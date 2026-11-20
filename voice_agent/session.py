"""VoiceSession: the turn-taking brain of one listener's live briefing.

One session per websocket. It owns:

* the narration plan (the briefing cut into sentences) and a resume cursor
  that always points at the sentence the listener is hearing;
* audio flow control: at most ``max_chunks_ahead`` sentences are queued in
  the browser, so an interruption never has much to throw away;
* barge-in handling in two steps: *suspect* (listener's voice detected: the
  browser ducks the narrator, the server stops queueing more audio) then
  *confirm* (real words show up in the transcript: fade out and listen) or
  *release* (it was a cough or an "mm-hm": un-duck and carry on);
* the conversational turn: answer, hand off to the main agent, or obey a
  playback command, then bridge back into the briefing at the interrupted
  sentence.

Exactly one "turn" task runs at a time (narrating, answering, researching,
waiting for a follow-up). A confirmed interruption cancels it.

Wire protocol (JSON text frames; audio is binary):

  server -> client
    {"type":"state","state":...}
    {"type":"chunk_start","seq":n, ...Chunk.wire(), "sample_rate":24000}
    <binary: uint32 LE seq + PCM16 mono>      audio for chunk n
    {"type":"chunk_end","seq":n}
    {"type":"flush"}                          fade out and drop all queued audio
    {"type":"unduck"}                         false alarm, restore volume
    {"type":"transcript","final":bool,"text":...}
    {"type":"research","status":"pending|done|failed", ...}
    {"type":"ended"}
  client -> server
    {"type":"start"} | {"type":"playback","event":"started|ended","seq":n}
    {"type":"speech_start"} | {"type":"speech_end"} | <binary PCM16 16 kHz mic audio>
    {"type":"interrupt"} | {"type":"text","text":...} | {"type":"control","action":...}
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import struct
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from . import phrases
from .config import Settings
from .main_agent import MainAgent, MainAgentRequest, MainAgentResponse
from .router import Route, RouteContext, Router, heuristic_route
from .script import Briefing, Chunk, Citation, chunk_text, plan_briefing
from .stt import STT
from .tts import TTSEngine

log = logging.getLogger(__name__)

NARRATION_KINDS = {"script", "intro", "outro"}


class Transport(Protocol):
    async def send_json(self, data: dict[str, Any]) -> None: ...
    async def send_bytes(self, data: bytes) -> None: ...


class State:
    IDLE = "idle"
    NARRATING = "narrating"
    SUSPECT = "suspect"  # listener may be interrupting; narrator ducked
    LISTENING = "listening"
    THINKING = "thinking"
    RESEARCHING = "researching"
    ANSWERING = "answering"
    AWAITING = "awaiting_follow_up"
    PAUSED = "paused"
    ENDED = "ended"


@dataclass
class _Sent:
    chunk: Chunk
    plan_index: int | None


def _real_words(text: str) -> bool:
    """Is this partial transcript enough to stop the narrator? ("stop" yes, "mm" no, "so the" yes)."""
    quick = heuristic_route(text)
    if quick is not None and quick.intent == "control":
        return True
    if quick is not None and quick.intent == "backchannel":
        return False
    return len(re.findall(r"[A-Za-z']+", text)) >= 2


class VoiceSession:
    def __init__(
        self,
        session_id: str,
        briefing: Briefing,
        *,
        tts: TTSEngine,
        stt: STT,
        router: Router,
        main_agent: MainAgent,
        settings: Settings,
        transport: Transport,
        rng: random.Random | None = None,
    ) -> None:
        self.id = session_id
        self.briefing = briefing
        self.tts = tts
        self.stt = stt
        self.router = router
        self.main_agent = main_agent
        self.s = settings
        self.t = transport
        self.rng = rng or random.Random()

        self.plan: list[Chunk] = plan_briefing(briefing)
        self.resume_index = 0
        self.speed = briefing.delivery.pace or 1.0
        self.state = State.IDLE
        self.history: list[dict[str, str]] = []

        self._seq = 0
        self._sent: dict[int, _Sent] = {}  # queued on the client, not yet finished playing
        self._cond = asyncio.Condition()
        self._hold = asyncio.Event()
        self._hold.set()  # set = allowed to queue more audio
        self._turn: asyncio.Task | None = None
        self._state_before_suspect = State.IDLE
        self._utterance_open = False
        self._confirmed = False
        self._speech_started_at = 0.0
        self._confirm_timer: asyncio.Task | None = None
        self._finishing: asyncio.Task | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            await self.t.send_json({"type": "state", "state": state})

    def _segment_title(self, segment_id: str) -> str:
        seg = self.briefing.segment(segment_id)
        return seg.title if seg else ""

    def _current_segment_id(self) -> str:
        idx = min(self.resume_index, len(self.plan) - 1)
        return self.plan[idx].segment_id if self.plan else ""

    def _spoken_so_far(self) -> str:
        seg_id = self._current_segment_id()
        upto = min(self.resume_index, len(self.plan) - 1)
        return " ".join(c.text for c in self.plan[: upto + 1] if c.segment_id == seg_id)

    def _line(self, text: str, kind: str, tone: str | None = None, citations: list[Citation] | None = None) -> list[Chunk]:
        return chunk_text(
            text,
            segment_id=self._current_segment_id(),
            tone=tone or self.briefing.delivery.tone,
            kind=kind,
            id_prefix=f"{kind}-{uuid.uuid4().hex[:6]}",
            citations=citations,
            pause_after_ms=250,
        )

    def _start_turn(self, coro) -> None:
        self._cancel_turn()
        self._turn = asyncio.create_task(self._run_turn(coro))

    async def _run_turn(self, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("turn failed in session %s", self.id)
            with contextlib.suppress(Exception):
                await self.t.send_json({"type": "error", "message": "internal error in voice agent turn"})

    def _cancel_turn(self) -> None:
        if self._turn and not self._turn.done():
            self._turn.cancel()
        self._turn = None

    # ------------------------------------------------------------------
    # audio out
    # ------------------------------------------------------------------

    async def _wait_capacity(self) -> None:
        async with self._cond:
            await self._cond.wait_for(lambda: len(self._sent) < self.s.max_chunks_ahead)
        await self._hold.wait()

    async def _drain(self) -> None:
        """Wait until the listener has heard everything queued."""
        async with self._cond:
            await self._cond.wait_for(lambda: not self._sent)

    async def _speak(self, chunks: list[Chunk], plan_offset: int | None = None) -> None:
        """Send chunks with flow control; returns once the listener has heard them all."""
        for i, chunk in enumerate(chunks):
            await self._wait_capacity()
            self._seq += 1
            seq = self._seq
            self._sent[seq] = _Sent(chunk, None if plan_offset is None else plan_offset + i)
            await self.t.send_json({"type": "chunk_start", "seq": seq, "sample_rate": self.tts.sample_rate, **chunk.wire()})
            header = struct.pack("<I", seq)
            try:
                async for pcm in self.tts.stream(
                    chunk.text,
                    tone=chunk.tone,
                    pace=self.speed,
                    voice_id=self.briefing.delivery.voice_id,
                    fast=chunk.kind not in NARRATION_KINDS,
                    previous_text=chunk.previous_text,
                    next_text=chunk.next_text,
                    language=self.briefing.language,
                ):
                    if seq not in self._sent:  # flushed while synthesizing
                        break
                    await self.t.send_bytes(header + pcm)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("TTS failed for %r: %s", chunk.text[:60], exc)
                await self.t.send_json({"type": "error", "message": f"speech synthesis failed: {exc}"})
            await self.t.send_json({"type": "chunk_end", "seq": seq})
        await self._drain()

    async def _flush(self) -> None:
        async with self._cond:
            self._sent.clear()
            self._cond.notify_all()
        self._hold.set()
        await self.t.send_json({"type": "flush"})

    async def on_playback(self, event: str, seq: int) -> None:
        sent = self._sent.get(seq)
        if sent is None:
            return
        if event == "started":
            if sent.plan_index is not None:
                # the sentence being heard is where we resume if interrupted
                self.resume_index = sent.plan_index
        elif event == "ended":
            if sent.plan_index is not None:
                self.resume_index = sent.plan_index + 1
            async with self._cond:
                self._sent.pop(seq, None)
                self._cond.notify_all()

    # ------------------------------------------------------------------
    # narration
    # ------------------------------------------------------------------

    async def prewarm(self) -> None:
        """Synthesize the stock lines and the first sentences ahead of time (fills the TTS cache)."""
        async def warm(text: str, tone: str, fast: bool, prev: str = "", nxt: str = "") -> None:
            with contextlib.suppress(Exception):
                async for _ in self.tts.stream(text, tone=tone, pace=self.speed, voice_id=self.briefing.delivery.voice_id,
                                               fast=fast, previous_text=prev, next_text=nxt,
                                               language=self.briefing.language):
                    pass

        tone = self.briefing.delivery.tone
        jobs = [warm(c.text, c.tone, False, c.previous_text, c.next_text) for c in self.plan[:2]]
        jobs += [warm(line, tone, True) for line in phrases.all_prerender_lines()]
        sem = asyncio.Semaphore(3)

        async def limited(job):
            async with sem:
                await job

        await asyncio.gather(*(limited(j) for j in jobs))

    async def _narrate(self, bridge: bool = False, preface: str = "") -> None:
        if self.resume_index >= len(self.plan):
            await self._finish_briefing()
            return
        lead: list[Chunk] = []
        if preface:
            lead += self._line(preface, "bridge")
        elif bridge:
            title = self._segment_title(self.plan[self.resume_index].segment_id)
            lead += self._line(phrases.bridge(title, self.rng), "bridge")
        await self._set_state(State.NARRATING)
        if lead:
            await self._speak(lead)
        start = self.resume_index
        await self._speak(self.plan[start:], plan_offset=start)
        self.resume_index = len(self.plan)
        await self._finish_briefing()

    async def _finish_briefing(self) -> None:
        if self.state != State.ENDED:
            if not self.briefing.outro:
                await self._set_state(State.NARRATING)
                await self._speak(self._line(phrases.END, "bridge"))
            await self._set_state(State.ENDED)
            await self.t.send_json({"type": "ended"})

    # ------------------------------------------------------------------
    # inbound events
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self.resume_index = 0
        self._start_turn(self._narrate())

    async def on_speech_start(self) -> None:
        """Browser VAD heard the listener. Duck first, decide later."""
        if self._utterance_open:
            return
        if self._finishing and not self._finishing.done():
            # previous utterance still being transcribed; let it land first
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(self._finishing), timeout=4.5)
        self._utterance_open = True
        self._confirmed = False
        self._speech_started_at = time.monotonic()
        await self.stt.begin(self._on_partial)
        busy = self._turn is not None and not self._turn.done()
        if busy and self.state not in (State.AWAITING, State.PAUSED, State.ENDED, State.IDLE):
            self._state_before_suspect = self.state
            self._hold.clear()  # queue nothing new while we find out
            await self._set_state(State.SUSPECT)
            if not self.stt.supports_partials:
                self._confirm_timer = asyncio.create_task(self._confirm_after(0.9))
        else:
            if self.state != State.LISTENING:  # (push-to-talk already recorded it in on_interrupt)
                self._state_before_suspect = self.state
            if self.state == State.AWAITING:
                self._cancel_turn()  # stop the "back to the briefing" countdown
            self._confirmed = True
            await self._set_state(State.LISTENING)

    async def _confirm_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if self._utterance_open and not self._confirmed:
            await self._confirm_barge_in()

    def _on_partial(self, text: str) -> None:
        asyncio.get_running_loop().create_task(self._handle_partial(text))

    async def _handle_partial(self, text: str) -> None:
        await self.t.send_json({"type": "transcript", "final": False, "text": text})
        if self._utterance_open and not self._confirmed and _real_words(text):
            await self._confirm_barge_in()

    async def _confirm_barge_in(self) -> None:
        """The listener really is talking: fade out, stop the current turn, listen."""
        if self._confirmed:
            return
        self._confirmed = True
        if self._confirm_timer:
            self._confirm_timer.cancel()
        self._cancel_turn()
        await self._flush()
        await self._set_state(State.LISTENING)

    async def on_audio(self, pcm16: bytes) -> None:
        if self._utterance_open:
            await self.stt.push(pcm16)

    async def on_speech_end(self) -> None:
        """Returns immediately; the STT commit (up to a few seconds) runs in the background."""
        if not self._utterance_open:
            return
        self._utterance_open = False
        if self._confirm_timer:
            self._confirm_timer.cancel()
        self._finishing = asyncio.create_task(self._finish_utterance())

    async def _finish_utterance(self) -> None:
        text = await self.stt.finish()
        if text:
            await self.t.send_json({"type": "transcript", "final": True, "text": text})
        if not self._confirmed:
            quick = heuristic_route(text) if text else None
            if not text or (quick is not None and quick.intent == "backchannel"):
                await self._release_suspect()
                return
            await self._confirm_barge_in()
        if not text:
            await self._after_empty_utterance()
            return
        self._start_turn(self._handle_text(text))

    async def _release_suspect(self) -> None:
        """False alarm (cough, "mm-hm"): restore volume and keep going."""
        self._hold.set()
        await self.t.send_json({"type": "unduck"})
        await self._set_state(self._state_before_suspect)

    async def _after_empty_utterance(self) -> None:
        # We stopped to listen but heard nothing usable.
        before = self._state_before_suspect
        if before == State.ENDED or self.resume_index >= len(self.plan):
            await self._set_state(State.ENDED)
        elif before in (State.PAUSED, State.IDLE):
            await self._set_state(before)
        else:
            self._start_turn(self._narrate(bridge=True))

    async def on_interrupt(self) -> None:
        """Explicit interrupt (push-to-talk, typing): stop at once."""
        if self.state not in (State.LISTENING, State.SUSPECT):
            self._state_before_suspect = self.state
        self._confirmed = True
        self._cancel_turn()
        await self._flush()
        await self._set_state(State.LISTENING)

    async def on_text(self, text: str) -> None:
        await self.on_interrupt()
        await self.t.send_json({"type": "transcript", "final": True, "text": text})
        self._start_turn(self._handle_text(text))

    async def on_control(self, action: str) -> None:
        await self.on_interrupt()
        self._start_turn(self._do_control(action, spoken=False))

    async def close(self) -> None:
        self._closed = True
        self._cancel_turn()
        with contextlib.suppress(Exception):
            await self.stt.aclose()

    # ------------------------------------------------------------------
    # the conversational turn
    # ------------------------------------------------------------------

    def _route_context(self) -> RouteContext:
        seg_id = self._current_segment_id()
        seg = self.briefing.segment(seg_id)
        last = self.history[-1] if self.history else None
        return RouteContext(
            briefing_title=self.briefing.title,
            current_segment_title=seg.title if seg else "",
            current_segment_text=seg.text if seg else "",
            spoken_so_far=self._spoken_so_far(),
            previous_exchange=f"Q: {last['question']}\nA: {last['answer']}" if last else "",
            language=self.briefing.language,
        )

    async def _handle_text(self, text: str) -> None:
        await self._set_state(State.THINKING)
        route = await self.router.route(text, self._route_context())
        await self.t.send_json({"type": "route", "intent": route.intent, "action": route.action,
                                "needs": route.needs, "source": route.source})
        if route.intent == "backchannel":
            await self._resume_or_idle(bridge=False)
        elif route.intent == "control":
            await self._do_control(route.action, spoken=True)
        elif route.intent == "answer":
            await self._set_state(State.ANSWERING)
            await self._speak(self._line(route.answer, "answer"))
            self.history.append({"question": text, "answer": route.answer})
            await self._resume_or_idle(bridge=True)
        else:
            await self._research(text, route)

    async def _resume_or_idle(self, bridge: bool) -> None:
        if self.resume_index >= len(self.plan):
            await self._set_state(State.ENDED)
            return
        await self._narrate(bridge=bridge)

    async def _do_control(self, action: str, spoken: bool) -> None:
        if action == "pause":
            await self._set_state(State.PAUSED)
            if spoken:
                await self._speak(self._line(phrases.PAUSE, "bridge"))
            return
        if action == "stop":
            await self._speak(self._line(phrases.STOP, "bridge"))
            await self._set_state(State.ENDED)
            await self.t.send_json({"type": "ended"})
            return
        if action == "skip":
            current = self._current_segment_id()
            nxt = next((i for i in range(self.resume_index, len(self.plan)) if self.plan[i].segment_id != current),
                       len(self.plan))
            self.resume_index = nxt
            await self._narrate(preface=phrases.SKIP)
            return
        if action == "repeat":
            # back to the previous sentence of the same story, so the repeat has context
            idx = min(self.resume_index, len(self.plan) - 1)
            if idx > 0 and self.plan[idx - 1].segment_id == self.plan[idx].segment_id:
                idx -= 1
            self.resume_index = idx
            await self._narrate(preface=phrases.REPEAT)
            return
        if action in ("slower", "faster"):
            delta = -0.08 if action == "slower" else 0.08
            self.speed = round(min(1.2, max(0.75, self.speed + delta)), 2)
            await self._narrate(preface=phrases.SLOWER if action == "slower" else phrases.FASTER)
            return
        # resume (default)
        await self._resume_or_idle(bridge=True)

    def _build_request(self, question: str, route: Route) -> MainAgentRequest:
        seg = self.briefing.segment(self._current_segment_id())
        return MainAgentRequest(
            request_id=f"r-{uuid.uuid4().hex[:8]}",
            session_id=self.id,
            briefing_id=self.briefing.id,
            question=question,
            query=route.query or question,
            needs=route.needs,
            time_hint=route.time_hint,
            language=self.briefing.language,
            delivery={"tone": self.briefing.delivery.tone, "style": self.briefing.delivery.style},
            context={
                "segment_id": seg.id if seg else "",
                "segment_title": seg.title if seg else "",
                "segment_text": seg.text if seg else "",
                "spoken_so_far": self._spoken_so_far(),
                "citations": [c.__dict__ for c in seg.citations] if seg else [],
            },
            history=self.history[-3:],
            as_of=self.briefing.date,
        )

    async def _research(self, question: str, route: Route) -> None:
        """Say we don't know yet, ask the main agent, speak its answer, then offer to go back."""
        req = self._build_request(question, route)
        await self._set_state(State.RESEARCHING)
        await self.t.send_json({"type": "research", "status": "pending", "request_id": req.request_id,
                                "question": question, "query": req.query, "needs": req.needs})
        job = asyncio.create_task(asyncio.wait_for(self.main_agent.handle(req), self.s.main_agent_timeout_s))
        try:
            # 1. acknowledge immediately (pre-rendered, so it plays with no synthesis delay)
            await self._speak(self._line(phrases.ack_for(route.needs, self.rng), "ack"))
            # 2. hold lines if the desk is slow
            holds = list(phrases.HOLD_LINES)
            while not job.done():
                done, _ = await asyncio.wait({job}, timeout=self.s.hold_line_after_s)
                if not done and holds:
                    await self._speak(self._line(holds.pop(0), "hold"))
                elif not done:
                    await asyncio.wait({job})
            resp: MainAgentResponse = job.result()
        except asyncio.CancelledError:
            job.cancel()
            raise
        except Exception as exc:
            log.warning("main agent failed: %s", exc)
            await self.t.send_json({"type": "research", "status": "failed", "request_id": req.request_id,
                                    "error": str(exc)})
            await self._speak(self._line(phrases.FAIL, "bridge"))
            await self._resume_or_idle(bridge=False)
            return

        cites = {c.get("id"): c for c in resp.citations}
        await self.t.send_json({"type": "research", "status": "done", "request_id": req.request_id,
                                "answer_status": resp.status, "citations": resp.citations})
        await self._set_state(State.ANSWERING)
        chunks: list[Chunk] = []
        for seg in resp.segments:
            seg_cites = [Citation.from_any(cites[c]) for c in seg.citation_ids if c in cites]
            chunks += self._line(seg.text, "answer", tone=seg.tone or None, citations=seg_cites)
        ended = self.resume_index >= len(self.plan)
        follow_up = resp.follow_up or (phrases.AFTER_END_FOLLOW_UP if ended else phrases.FOLLOW_UP_DEFAULT)
        chunks += self._line(follow_up, "answer")
        await self._speak(chunks)
        self.history.append({"question": question, "answer": " ".join(s.text for s in resp.segments)})

        if ended:
            await self._set_state(State.ENDED)
            return
        # 3. give the listener a moment to follow up, then pick the briefing back up
        await self._set_state(State.AWAITING)
        await asyncio.sleep(self.s.followup_wait_s)
        await self._narrate(bridge=True)
