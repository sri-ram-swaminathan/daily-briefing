"""The main agent ("research desk"): what the voice agent calls when it doesn't know.

Contract (JSON over HTTP, so any agent can sit behind it; set MAIN_AGENT_URL):

POST <MAIN_AGENT_URL>
{
  "request_id": "r-3f2a", "session_id": "...", "briefing_id": "...",
  "question": "how did this develop?",            # what the listener said
  "query": "How did the Intel shareholder lawsuit develop?",   # router's rewrite
  "needs": ["timeline"], "time_hint": "last week", "language": "en",
  "delivery": {"tone": "warm, measured", "style": "..."},
  "context": {"segment_id": "...", "segment_title": "...", "segment_text": "...",
              "spoken_so_far": "...", "citations": [{"id": "...", "url": "...", ...}]},
  "history": [{"question": "...", "answer": "..."}]
}
-> 200
{
  "request_id": "r-3f2a",
  "status": "ok" | "not_found" | "error",
  "segments": [{"text": "spoken words", "tone": "serious", "citation_ids": ["INVZ-..."]}],
  "citations": [{"id": "...", "publication": "...", "headline": "...", "url": "...", "published_at": "..."}],
  "follow_up": "Want me to go through the court timeline, or head back to the briefing?"
}

``LocalMainAgent`` is the reference implementation: BM25 retrieval over the
dataset, plus the News Council's saved observations on those articles, then the
OpenAI model writes a short spoken answer grounded in them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from pathlib import Path

from .council_bridge import council_perspectives, describe
from .retrieval import Doc, NewsIndex, resolve_time_hint, tokenize

log = logging.getLogger(__name__)


@dataclass
class MainAgentRequest:
    request_id: str
    session_id: str
    briefing_id: str
    question: str
    query: str
    needs: list[str] = field(default_factory=list)
    time_hint: str = ""
    language: str = "en"
    delivery: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, str]] = field(default_factory=list)
    as_of: str = ""  # the briefing's "now", ISO date

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MainAgentRequest":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class SpokenSegment:
    text: str
    tone: str = ""
    citation_ids: list[str] = field(default_factory=list)


@dataclass
class MainAgentResponse:
    request_id: str
    status: str
    segments: list[SpokenSegment]
    citations: list[dict[str, Any]] = field(default_factory=list)
    follow_up: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MainAgentResponse":
        return cls(
            request_id=raw.get("request_id", ""),
            status=raw.get("status", "ok"),
            segments=[
                SpokenSegment(text=s.get("text", ""), tone=s.get("tone", ""), citation_ids=s.get("citation_ids", []))
                for s in raw.get("segments", [])
                if s.get("text")
            ],
            citations=raw.get("citations", []),
            follow_up=raw.get("follow_up", ""),
        )


class MainAgent(Protocol):
    async def handle(self, req: MainAgentRequest) -> MainAgentResponse: ...


class RemoteMainAgent:
    def __init__(self, url: str, timeout_s: float = 45.0) -> None:
        self.url = url
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def handle(self, req: MainAgentRequest) -> MainAgentResponse:
        resp = await self._client.post(self.url, json=req.to_dict())
        resp.raise_for_status()
        return MainAgentResponse.from_dict(resp.json())


MAIN_SYSTEM = """You are the research desk behind a live news briefing. The listener interrupted to ask
something the briefing didn't cover, or to dig deeper. You write what the narrator says next.

Sources, in order of preference:
1. ARTICLES: the newsroom's licensed feeds. Prefer them for anything they cover.
2. The web search tool. Use it when the articles don't fully answer, when the listener asks to dig
   deeper, wants background ("who is", "why"), context, what happened next, or anything more recent
   than the articles. Prefer primary sources and established outlets. Never use the web to contradict
   an article silently: if they disagree, say so and name both.
3. COUNCIL PERSPECTIVES (opinions, not facts; see below).

Rules:
- Do not rely on your own memory for who currently holds an office or for recent events: TODAY is given,
  and the sources decide. If unsure of someone's current title, use their name only.
- Spoken English for the ear: short sentences, no lists, no markdown, no URLs, no article IDs read aloud,
  no bracketed links. Attribute as you go ("Invezz reported on November 16 that ...", "According to
  Reuters ..."). Numbers must match the source exactly.
- citation_ids: the article ids a segment relies on, plus the string "web" if it uses web results.
  Whenever one of the ARTICLES states a fact you use, cite its id, even if a web page confirms it too.
  The CURRENT STORY's article comes first: the listener was hearing it when they asked.
- If neither the articles nor the web answer it, set status "not_found" and say briefly what you found.
- Licence: paraphrase. Never read out article text; a direct quote must be under 12 words and attributed.
- Timelines go in date order and say the dates. Source requests name each outlet, headline in brief, date.
- MEMORY: EARLIER IN THIS CONVERSATION is what the listener already heard. Never repeat it. When they say
  "go deeper", "more", "and then?", start directly with something new (a mechanism, a number, a named
  person's role, what happens next, who is affected), with no recap of the story.
- Length is a hard limit: 60 to 160 words in total, 1 to 4 segments of at most 3 sentences each. If NEEDS
  includes deep_dive, up to 280 words over up to 6 segments. Pick the two or three facts that matter most.
- Match the delivery tone given. A segment's tone is a few words such as "serious" or "warm, measured".
- COUNCIL PERSPECTIVES are reactions from the News Council, a panel of fictional personas (Mira,
  Tom, Amara, Walter, Sami, Vera, Elias). When the listener asks what people think, for other angles,
  or who is affected, relay one to three of them by name, say they are council members, and keep them
  clearly separate from reported facts. Cite the article id they were reacting to.
- follow_up is one short spoken question offering a concrete next step (a deeper angle you could
  research, a related story, or returning to the briefing)."""

MAIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok", "not_found"]},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "tone": {"type": "string"},
                    "citation_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "tone", "citation_ids"],
                "additionalProperties": False,
            },
        },
        "follow_up": {"type": "string"},
    },
    "required": ["status", "segments", "follow_up"],
    "additionalProperties": False,
}


def _fmt_date(dt: datetime) -> str:
    return f"{dt.strftime('%B')} {dt.day}"


_CITE_LINK = re.compile(r"\s*\(\[[^\]]*\]\(https?://[^)]+\)\)")  # "([apnews.com](url))": a citation, drop it
_MD_LINK = re.compile(r"\[([^\]]*)\]\(https?://[^)]+\)")  # "[the report](url)" in a sentence: keep the words
_BARE_URL = re.compile(r"\s*\(?https?://\S+\)?")


_MD_MARK = re.compile(r"(\*\*|__|`|^#+\s*)", re.M)
_LIST_MARK = re.compile(r"(^|\s)(\d{1,2}\.|[-*\u2022])\s+(?=[A-Z*])")


def _speakable(text: str) -> str:
    """Strip markdown and the inline links web search tends to add; links go on screen, not in the ear."""
    text = _BARE_URL.sub("", _MD_LINK.sub(r"\1", _CITE_LINK.sub("", text)))
    text = _LIST_MARK.sub(r"\1", _MD_MARK.sub("", text))
    return " ".join(text.split())


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _cap_words(segments: list["SpokenSegment"], limit: int) -> None:
    """Models overshoot length when they've searched; a listener can't skim, so cut at a sentence."""
    used = 0
    for i, seg in enumerate(segments):
        kept = []
        for sentence in _SENTENCE.split(seg.text):
            n = len(sentence.split())
            if used + n > limit and (kept or i):
                break
            kept.append(sentence)
            used += n
        seg.text = " ".join(kept)
        if used >= limit or len(kept) < len(_SENTENCE.split(seg.text)):
            del segments[i + 1:]
            break
    segments[:] = [s for s in segments if s.text]


def _domain(url: str) -> str:
    return urlsplit(url).netloc.removeprefix("www.")


class LocalMainAgent:
    def __init__(self, index: NewsIndex, llm: Any | None = None, council_db: Path | None = None,
                 web_search: bool = True) -> None:
        self.index = index
        self.llm = llm
        self.council_db = council_db
        # OpenAI's hosted web search: background, digging deeper, anything newer than the feeds.
        self.web_search = web_search

    def _now(self, req: MainAgentRequest) -> datetime:
        if req.as_of:
            try:
                dt = datetime.fromisoformat(req.as_of)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return self.index.latest

    def retrieve(self, req: MainAgentRequest) -> tuple[list[Doc], bool]:
        since, until, future = resolve_time_hint(req.time_hint, self._now(req))
        ctx = req.context or {}
        query = req.query or req.question
        # "go deeper on that" carries no subject: anchor it to the thread it continues.
        if req.history and len(tokenize(query)) < 4:
            query = f"{req.history[-1].get('question', '')} {query}"
        query = " ".join(filter(None, [query, ctx.get("segment_title", "")]))
        boost = {c.get("id") for c in ctx.get("citations", []) if c.get("id")}
        k = 10 if "timeline" in req.needs else 6
        hits = self.index.search(query, k=k, language=req.language or "en", since=since, until=until,
                                 boost_ids=boost)
        if not hits and since:  # window too narrow: widen
            hits = self.index.search(query, k=k, language=req.language or "en", boost_ids=boost)
        docs = [d for _, d in hits]
        if "timeline" in req.needs:
            docs.sort(key=lambda d: d.published_at)
        return docs, future

    async def handle(self, req: MainAgentRequest) -> MainAgentResponse:
        docs, future = self.retrieve(req)
        if future:
            return MainAgentResponse(
                request_id=req.request_id,
                status="not_found",
                segments=[SpokenSegment(
                    text="I can only see reporting up to today, so I can't tell you what happens next week yet. "
                         "What I can do is walk you through how the story got here.",
                    tone="warm")],
                follow_up="Want the story so far?",
            )
        if not docs and not (self.llm is not None and self.web_search):
            return MainAgentResponse(
                request_id=req.request_id,
                status="not_found",
                segments=[SpokenSegment(text="I looked, but I couldn't find any reporting on that in today's sources.",
                                        tone="warm")],
                follow_up="Shall we get back to the briefing?",
            )
        by_id = {d.id: d for d in docs}
        if self.llm is not None:
            try:
                return await self._compose_llm(req, docs, by_id)
            except Exception as exc:
                log.warning("main agent LLM failed (%s); using template answer", exc)
        if not docs:
            return MainAgentResponse(
                request_id=req.request_id, status="not_found",
                segments=[SpokenSegment(text="I couldn't reach my sources just now. Try me again in a moment.",
                                        tone="warm")],
                follow_up="Shall we get back to the briefing?")
        return self._compose_template(req, docs)

    async def _compose_llm(self, req: MainAgentRequest, docs: list[Doc], by_id: dict[str, Doc]) -> MainAgentResponse:
        ctx = req.context or {}
        articles = "\n\n".join(
            f"[{d.id}] {d.publication}, {d.published_at:%Y-%m-%d %H:%M} UTC\nHEADLINE: {d.headline}\n"
            f"SUMMARY: {d.excerpt}\nBODY (for your reading only, do not reproduce): {d.text[:1800]}"
            for d in docs
        )
        views = council_perspectives(self.council_db, by_id) if self.council_db else {}
        history = "\n".join(f"Q: {h.get('question')}\nA: {h.get('answer')}" for h in req.history[-6:]) or "(none)"
        user = (
            f"TODAY: {self._now(req):%Y-%m-%d}\n"
            f"DELIVERY TONE: {req.delivery.get('tone', '')}\nSTYLE NOTES: {req.delivery.get('style', '')}\n"
            f"CURRENT STORY: {ctx.get('segment_title', '')}\n"
            f"WHAT THE LISTENER HAS HEARD: {ctx.get('spoken_so_far') or ctx.get('segment_text', '')}\n"
            f"EARLIER IN THIS CONVERSATION (already told; do not repeat or re-introduce):\n{history}\n\n"
            f"LISTENER ASKED: {req.question}\nRESEARCH QUESTION: {req.query}\nNEEDS: {', '.join(req.needs) or 'answer'}\n\n"
            f"ARTICLES:\n{articles or '(no matching articles in the feeds: use web search)'}\n\n"
            f"COUNCIL PERSPECTIVES:\n{describe(views) or '(none saved for these articles)'}"
        )
        web = self.web_search
        must_search = bool(set(req.needs) & {"deep_dive", "background", "related"}) or not docs
        kwargs = {"web_search": "required" if must_search else True} if web else {}
        raw = await self.llm.complete(system=MAIN_SYSTEM, user=user, schema=MAIN_SCHEMA, max_tokens=2500,
                                      timeout=60.0 if web else 40.0, name="research_answer", **kwargs)
        web_hits = raw.pop("_web_citations", [])
        resp = MainAgentResponse.from_dict({"request_id": req.request_id, **raw})
        used: dict[str, dict] = {}
        web_ids = [f"web-{i}" for i in range(1, len(web_hits) + 1)]
        for seg in resp.segments:
            seg.text = _speakable(seg.text)
            uses_web = "web" in seg.citation_ids
            seg.citation_ids = [c for c in seg.citation_ids if c in by_id]
            for c in seg.citation_ids:
                used[c] = by_id[c].citation()
            if uses_web:
                seg.citation_ids += web_ids
        if web_ids and not any(set(web_ids) & set(seg.citation_ids) for seg in resp.segments):
            # The model searched but forgot to tag which segment used it: attach the hits to the
            # uncited segments so every web fact still has a visible source.
            for seg in resp.segments:
                if not seg.citation_ids:
                    seg.citation_ids = list(web_ids)
        _cap_words(resp.segments, 300 if "deep_dive" in req.needs else 190)
        resp.follow_up = _speakable(resp.follow_up)
        resp.citations = list(used.values()) + [
            {"id": wid, "publication": _domain(h["url"]), "headline": h["title"] or _domain(h["url"]),
             "url": h["url"], "published_at": "", "web": True}
            for wid, h in zip(web_ids, web_hits)]
        if not resp.segments:
            return self._compose_template(req, docs)
        return resp

    def _compose_template(self, req: MainAgentRequest, docs: list[Doc]) -> MainAgentResponse:
        """No-LLM fallback: names outlets, dates and headlines (short quotes are licence-safe)."""
        picks = docs[:4]
        if "timeline" in req.needs:
            lead = "Here's how it unfolded."
        elif "sources" in req.needs:
            lead = "Here's where that reporting comes from."
        else:
            lead = "Here's what I found."
        segments = [SpokenSegment(text=lead, tone="warm")]
        for d in picks:
            segments.append(SpokenSegment(
                text=f"On {_fmt_date(d.published_at)}, {d.publication} ran a piece headlined: {d.headline.rstrip('.')}.",
                tone="measured", citation_ids=[d.id]))
        return MainAgentResponse(
            request_id=req.request_id,
            status="ok",
            segments=segments,
            citations=[d.citation() for d in picks],
            follow_up="The links are on your screen. Want more on any of these, or shall we get back to the briefing?",
        )
