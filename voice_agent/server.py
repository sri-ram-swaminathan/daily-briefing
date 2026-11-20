"""Voice agent HTTP + websocket routes.

``app.py`` mounts the Gradio app on top of this FastAPI app and serves both on
one port (the Hugging Face Spaces layout):

    uv run app.py

Endpoints
    GET  /voice?briefing=id        the listening page (embedded in the Gradio "Listen" tab)
    GET  /api/briefings            list loaded briefing scripts
    GET  /api/briefings/{id}       one script
    POST /api/briefings            the summary agent pushes a new script (JSON or markup text)
    POST /api/main-agent           the built-in research desk, same contract as MAIN_AGENT_URL
    POST /api/ask                  text Q&A with the research desk (feeds + web search), with memory
    POST /api/council              live council reactions to a pasted story or an article id
    POST /api/reader-briefing      build a reader's voice briefing from their interests (website "Talk to it live")
    WS   /ws/session?briefing=id   one live, interruptible briefing
    GET  /                         the News Council website (site/), when app.py serves it
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .main_agent import LocalMainAgent, MainAgent, MainAgentRequest, RemoteMainAgent
from .retrieval import NewsIndex
from .router import Router
from .script import Briefing, parse_briefing
from .session import VoiceSession
from .stt import make_stt
from .tts import make_tts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("voice_agent")

settings = get_settings()


class AppState:
    briefings: dict[str, Briefing] = {}
    index: NewsIndex | None = None
    local_agent: LocalMainAgent | None = None
    main_agent: MainAgent | None = None
    router: Router | None = None
    tts = None


S = AppState()


def _make_llm(model: str):
    if not settings.openai_enabled:
        return None
    try:
        from .llm import OpenAIJSON

        return OpenAIJSON(model)
    except Exception as exc:  # SDK missing or blocked: degrade to heuristics/templates
        log.warning("OpenAI unavailable (%s); running without an LLM", exc)
        return None


def register_briefing(b: Briefing, *, persist: bool = True) -> Briefing:
    """Make a script playable. Called by POST /api/briefings and by the Gradio app."""
    if not b.segments:
        raise ValueError("The script has no stories to read.")
    S.briefings[b.id] = b
    if persist:
        settings.briefings_dir.mkdir(exist_ok=True)
        (settings.briefings_dir / f"{b.id}.json").write_text(json.dumps(b.to_dict(), indent=2), encoding="utf-8")
    return b


def _load_briefings() -> None:
    settings.briefings_dir.mkdir(exist_ok=True)
    for path in sorted(settings.briefings_dir.glob("*")):
        if path.suffix not in {".json", ".txt", ".md"}:
            continue
        try:
            b = parse_briefing(path.read_text(encoding="utf-8"))
            S.briefings[b.id] = b
        except Exception as exc:
            log.warning("skipping briefing %s: %s", path.name, exc)
    log.info("loaded %d briefing(s)", len(S.briefings))


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_briefings()
    try:
        S.index = await asyncio.to_thread(NewsIndex.from_dir, settings.data_dir)
    except (FileNotFoundError, OSError, RuntimeError) as exc:  # no data synced (HF_DATA_URL unset or unreachable)
        log.warning("news dataset unavailable (%s); research desk has no articles", exc)
        S.index = NewsIndex([])
    log.info("indexed %d articles", len(S.index.docs))
    S.local_agent = LocalMainAgent(S.index, _make_llm(settings.main_agent_model), council_db=settings.council_db,
                                   web_search=settings.web_search)
    S.main_agent = RemoteMainAgent(settings.main_agent_url, settings.main_agent_timeout_s) if settings.main_agent_url else S.local_agent
    S.router = Router(_make_llm(settings.router_model))
    S.tts = make_tts(settings)  # shared so its audio cache is shared across listeners
    yield
    await S.tts.aclose()


app = FastAPI(title="News Council", lifespan=lifespan)


# ---------------------------------------------------------------- rate limits (public deployments)
class RateLimiter:
    """Caps the endpoints that spend API credits, per visitor and in total.

    RATE_LIMIT_PER_HOUR (default 40) per client IP, DAILY_LIMIT (default 1500) across everyone.
    0 disables a limit. In memory: a restart resets the counts, which is fine for a demo Space.
    """

    COSTLY = ("/api/ask", "/api/council", "/api/reader-briefing", "/api/main-agent", "/ws/session")

    def __init__(self) -> None:
        import os
        from collections import deque

        self.per_hour = int(os.getenv("RATE_LIMIT_PER_HOUR", "40"))
        self.per_day = int(os.getenv("DAILY_LIMIT", "1500"))
        self._by_ip: dict[str, deque] = {}
        self._day: deque = deque()
        self._deque = deque

    @staticmethod
    def client_ip(headers, client) -> str:
        fwd = headers.get("x-forwarded-for", "")  # Spaces and most hosts sit behind a proxy
        return fwd.split(",")[0].strip() if fwd else (client.host if client else "?")

    def allow(self, ip: str) -> str:
        """'' if allowed, else the reason."""
        import time

        now = time.time()
        while self._day and now - self._day[0] > 86400:
            self._day.popleft()
        if self.per_day and len(self._day) >= self.per_day:
            return "This demo has reached its limit for today. Try again tomorrow."
        hits = self._by_ip.setdefault(ip, self._deque())
        while hits and now - hits[0] > 3600:
            hits.popleft()
        if self.per_hour and len(hits) >= self.per_hour:
            return "You've asked a lot this hour. Give it a little while and try again."
        hits.append(now)
        self._day.append(now)
        if len(self._by_ip) > 5000:  # forget idle visitors
            for k in [k for k, v in self._by_ip.items() if not v][:2500]:
                del self._by_ip[k]
        return ""


limiter = RateLimiter()


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if request.method == "POST" and request.url.path in RateLimiter.COSTLY:
        reason = limiter.allow(RateLimiter.client_ip(request.headers, request.client))
        if reason:
            return JSONResponse({"detail": reason}, status_code=429)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "briefings": len(S.briefings),
        "articles": len(S.index.docs) if S.index else 0,
        "tts": type(S.tts).__name__,
        "llm": S.router.llm is not None if S.router else False,
        "main_agent": "remote" if settings.main_agent_url else "local",
        "web_search": bool(S.local_agent and S.local_agent.web_search and S.local_agent.llm),
    }


@app.get("/api/briefings")
async def list_briefings() -> list[dict[str, Any]]:
    return [{"id": b.id, "title": b.title, "date": b.date, "listener": b.listener, "language": b.language,
             "segments": len(b.segments)} for b in S.briefings.values()]


@app.get("/api/briefings/{briefing_id}")
async def get_briefing(briefing_id: str) -> dict[str, Any]:
    b = S.briefings.get(briefing_id)
    if not b:
        raise HTTPException(404, "unknown briefing")
    return b.to_dict()


@app.post("/api/briefings")
async def push_briefing(request: Request) -> dict[str, Any]:
    """Entry point for the summary agent."""
    raw = (await request.body()).decode("utf-8")
    try:
        b = parse_briefing(raw)
        register_briefing(b)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(422, f"invalid script: {exc}") from exc
    return {"id": b.id, "segments": len(b.segments)}


@app.post("/api/main-agent")
async def main_agent_endpoint(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    req = MainAgentRequest.from_dict(payload)
    resp = await S.local_agent.handle(req)
    return JSONResponse(resp.to_dict())


# ---------------------------------------------------------------- text Q&A (website)

_ASK_MEMORY: dict[str, list[dict[str, str]]] = {}
_ASK_MEMORY_MAX = 500


@app.post("/api/ask")
async def ask(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """{question, session_id?, article_id?, depth?: "brief"|"deep"} -> answer with citations.

    The desk remembers each session's earlier questions and answers, so "go deeper" and
    "and then what?" continue the thread instead of repeating it.
    """
    question = str(payload.get("question", "")).strip()[:600]
    if not question:
        raise HTTPException(422, "question is required")
    session_id = str(payload.get("session_id") or uuid.uuid4().hex[:12])[:40]
    history = _ASK_MEMORY.setdefault(session_id, [])
    if len(_ASK_MEMORY) > _ASK_MEMORY_MAX:
        _ASK_MEMORY.pop(next(iter(_ASK_MEMORY)))
    context: dict[str, Any] = {}
    doc = S.index.get(str(payload.get("article_id", ""))) if S.index and payload.get("article_id") else None
    if doc:
        context = {"segment_title": doc.headline, "segment_text": doc.excerpt, "citations": [doc.citation()]}
    route = S.router.route if S.router else None
    needs: list[str] = []
    if route is not None:
        from .router import RouteContext

        r = await route(question, RouteContext(briefing_title="", current_segment_title=context.get("segment_title", ""),
                                               current_segment_text=context.get("segment_text", ""),
                                               spoken_so_far="", previous_exchange=(
                                                   f"Q: {history[-1]['question']} A: {history[-1]['answer']}"
                                                   if history else "")))
        needs = list(r.needs)
        query = r.query or question
        time_hint = r.time_hint
    else:
        query, time_hint = question, ""
    if payload.get("depth") == "deep" and "deep_dive" not in needs:
        needs.append("deep_dive")
    req = MainAgentRequest(request_id=uuid.uuid4().hex[:8], session_id=session_id, briefing_id="web",
                           question=question, query=query, needs=needs, time_hint=time_hint, context=context, history=history[-6:],
                           delivery={"tone": "clear, friendly", "style": "Written for reading on screen."})
    resp = await S.local_agent.handle(req)
    answer = " ".join(seg.text for seg in resp.segments)
    history.append({"question": question, "answer": answer})
    return JSONResponse({"session_id": session_id, "answer": answer, **resp.to_dict()})


@app.post("/api/council")
async def council_live(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """{text?: str, article_id?: str} -> each persona's reaction and the moderator's summary.

    Saved reviews are returned for free when the article was already reviewed by council.py.
    """
    from .council_bridge import council_perspectives

    article_id = str(payload.get("article_id", "")).strip()
    if article_id:
        saved = council_perspectives(settings.council_db, [article_id]).get(article_id)
        if saved:
            return JSONResponse({"article_id": article_id, "saved": True, "reactions": saved})
    doc = S.index.get(article_id) if (article_id and S.index) else None
    text = str(payload.get("text", "")).strip()[:12000]
    if doc is None and not text:
        raise HTTPException(422, "text or a known article_id is required")
    if not settings.openai_enabled:
        raise HTTPException(503, "OPENAI_API_KEY is not set on the server")
    article = ({"id": doc.id, "headline": doc.headline, "excerpt": doc.excerpt, "text": doc.text, "url": doc.url}
               if doc else {"id": "pasted", "headline": "", "excerpt": "", "text": text, "url": ""})
    reactions = await asyncio.to_thread(_run_council, article)
    return JSONResponse({"article_id": article["id"], "saved": False, **reactions})


@app.post("/api/reader-briefing")
async def reader_briefing(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """{user, interests, preset?} -> {id}: the same sweep as the Listen tab, ready for /voice?briefing=id."""
    import os
    import re as _re

    from model_config import load_models

    from .council_bridge import build_reader_briefing, save_preferences
    from .presets import DEFAULT_PRESET, delivery_for

    user = _re.sub(r"[^a-z0-9_-]", "", str(payload.get("user", "")).lower())[:32]
    interests = str(payload.get("interests", "")).strip()[:1500]
    if not user or not interests:
        raise HTTPException(422, "user and interests are required")

    def build():
        save_preferences(settings.preferences_db, user, interests)
        return build_reader_briefing(
            user=user, delivery=delivery_for(str(payload.get("preset") or DEFAULT_PRESET)), index=S.index,
            council_db=settings.council_db, preferences_db=settings.preferences_db,
            model=os.getenv("OPENAI_MODEL") or load_models().main_agent, sweep_new=settings.openai_enabled)

    try:
        b, new = await asyncio.to_thread(build)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not b.segments:
        raise HTTPException(404, "No matching stories yet. Run more council reviews or widen the interests.")
    register_briefing(b)
    stories = []
    for seg in b.segments:
        cite = seg.citations[0] if seg.citations else None
        doc = S.index.get(cite.id) if (cite and S.index) else None
        stories.append({"id": cite.id if cite else seg.id, "headline": doc.headline if doc else seg.title,
                        "url": cite.url if cite else "", "publication": doc.publication if doc else "",
                        "published_at": doc.published_at.isoformat() if doc else "",
                        "topic": doc.topic if doc else "", "summary": seg.text})
    if payload.get("claims") and settings.openai_enabled:
        stories = await asyncio.to_thread(_link_story_claims, stories)
    return {"id": b.id, "segments": len(b.segments), "new": new, "stories": stories}


def _link_story_claims(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sentence-level sources for a live briefing (same as the demo readers'; see voice_agent/claims.py)."""
    from concurrent.futures import ThreadPoolExecutor

    from openai import OpenAI

    from model_config import load_models

    from .claims import link_claims
    from .council_bridge import council_perspectives

    model = load_models().main_agent
    with OpenAI() as client:
        def one(s):
            doc = S.index.get(s["id"]) if S.index else None
            if not doc:
                return s
            views = [v for v in council_perspectives(settings.council_db, [doc.id]).get(doc.id, []) if v["kind"] == "persona"]
            try:
                s["claims"] = link_claims(client, model, s["summary"],
                                          {"headline": doc.headline, "excerpt": doc.excerpt, "text": doc.text}, views)
            except Exception as exc:  # the story still links to its article as a whole
                log.warning("claim linking failed for %s: %s", doc.id, exc)
            return s

        with ThreadPoolExecutor(max_workers=6) as pool:
            return list(pool.map(one, stories))


def _run_council(article: dict[str, Any]) -> dict[str, Any]:
    import os
    from concurrent.futures import ThreadPoolExecutor

    from openai import OpenAI

    from council import EDITOR, InvalidReview, generate_observation, load_personas
    from model_config import load_models

    models = load_models()
    override = os.getenv("OPENAI_MODEL")
    members = load_personas(settings.personas_dir)
    moderator = members.pop("moderator", EDITOR)
    with OpenAI() as client:
        def one(item):
            name, instructions = item
            try:
                r = generate_observation(client, override or models.persona(name), name, instructions,
                                         {"article": article})
                return {"reviewer": name, "kind": "persona", "relevant": r.relevant,
                        "observation": r.observation, "quote": r.supporting_quote}
            except (InvalidReview, Exception) as exc:  # one failed persona must not sink the panel
                log.warning("council %s failed: %s", name, exc)
                return {"reviewer": name, "kind": "persona", "relevant": False, "observation": "", "quote": ""}

        with ThreadPoolExecutor(max_workers=7) as pool:
            reactions = list(pool.map(one, members.items()))
        heard = [r for r in reactions if r["relevant"]]
        summary = ""
        if heard:
            reply = client.responses.create(
                model=override or models.persona("moderator"),
                instructions=moderator + "\nGive your final summary based on the supplied reactions.",
                input=json.dumps({"article": article["headline"] or article["text"][:4000],
                                  "reactions": [{"name": r["reviewer"], "reaction": r["observation"]} for r in heard]},
                                 ensure_ascii=False),
                store=False)
            summary = reply.output_text.strip()
    return {"reactions": reactions, "summary": summary}


class WSTransport:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self._lock = asyncio.Lock()

    async def send_json(self, data: dict[str, Any]) -> None:
        async with self._lock:
            await self.ws.send_text(json.dumps(data))

    async def send_bytes(self, data: bytes) -> None:
        async with self._lock:
            await self.ws.send_bytes(data)


@app.websocket("/ws/session")
async def session_ws(ws: WebSocket) -> None:
    await ws.accept()
    reason = limiter.allow(RateLimiter.client_ip(ws.headers, ws.client))
    if reason:
        await ws.send_text(json.dumps({"type": "error", "message": reason}))
        await ws.close()
        return
    briefing_id = ws.query_params.get("briefing") or next(iter(S.briefings), None)
    briefing = S.briefings.get(briefing_id or "")
    if briefing is None:
        await ws.send_text(json.dumps({"type": "error", "message": "unknown briefing"}))
        await ws.close()
        return
    transport = WSTransport(ws)
    session = VoiceSession(
        uuid.uuid4().hex[:12],
        briefing,
        tts=S.tts,
        stt=make_stt(settings, briefing.language),
        router=S.router,
        main_agent=S.main_agent,
        settings=settings,
        transport=transport,
    )
    await transport.send_json({"type": "ready", "session_id": session.id, "briefing": briefing.to_dict(),
                               "stt": type(session.stt).__name__})
    prewarm = asyncio.create_task(session.prewarm())
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if (data := msg.get("bytes")) is not None:
                await session.on_audio(data)
                continue
            event = json.loads(msg.get("text") or "{}")
            kind = event.get("type")
            if kind == "start":
                await session.start()
            elif kind == "playback":
                await session.on_playback(event.get("event", ""), int(event.get("seq", -1)))
            elif kind == "speech_start":
                await session.on_speech_start()
            elif kind == "speech_end":
                await session.on_speech_end()
            elif kind == "interrupt":
                await session.on_interrupt()
            elif kind == "text" and event.get("text", "").strip():
                await session.on_text(event["text"].strip())
            elif kind == "control":
                await session.on_control(event.get("action", "resume"))
    except WebSocketDisconnect:
        pass
    finally:
        prewarm.cancel()
        await session.close()
        with contextlib.suppress(Exception):
            await ws.close()


@app.get("/voice")
async def voice_page() -> FileResponse:
    return FileResponse(settings.web_dir / "index.html")


app.mount("/voice/static", StaticFiles(directory=settings.web_dir), name="voice-static")
