"""End-to-end turn-taking tests with a simulated browser (no network)."""

import asyncio
import random
import struct
from dataclasses import replace

import pytest

from voice_agent.config import Settings
from voice_agent.main_agent import MainAgentResponse, SpokenSegment
from voice_agent.router import Router
from voice_agent.script import parse_briefing
from voice_agent.session import State, VoiceSession
from voice_agent.tts import FakeTTS

BRIEFING = {
    "id": "t",
    "title": "Test briefing",
    "delivery": {"tone": "warm"},
    "segments": [
        {"id": "a", "title": "Story A", "text": "Alpha one is a sentence of decent length here. Alpha two follows it with more words. Alpha three closes the story out nicely."},
        {"id": "b", "title": "Story B", "text": "Bravo one opens the second story with some words. Bravo two ends the second story with some words."},
    ],
}


class FakeClient:
    """Plays queued chunks in order, reporting started/ended like the browser does."""

    def __init__(self, play_s: float = 0.02):
        self.session: VoiceSession | None = None
        self.events: list[dict] = []
        self.audio_bytes = 0
        self.play_s = play_s
        self._ready: asyncio.Queue[int] = asyncio.Queue()
        self._flushed_upto = 0
        self._max_seq = 0
        self.task: asyncio.Task | None = None

    def start(self):
        self.task = asyncio.create_task(self._player())

    async def send_json(self, data):
        self.events.append(data)
        if data["type"] == "chunk_start":
            self._max_seq = data["seq"]
        elif data["type"] == "chunk_end":
            await self._ready.put(data["seq"])
        elif data["type"] == "flush":
            self._flushed_upto = self._max_seq

    async def send_bytes(self, data):
        (seq,) = struct.unpack("<I", data[:4])
        if seq > self._flushed_upto:
            self.audio_bytes += len(data) - 4

    async def _player(self):
        while True:
            seq = await self._ready.get()
            if seq <= self._flushed_upto:
                continue
            await self.session.on_playback("started", seq)
            await asyncio.sleep(self.play_s)
            if seq <= self._flushed_upto:
                continue
            await self.session.on_playback("ended", seq)

    def spoken(self, kind=None):
        return [e for e in self.events if e["type"] == "chunk_start" and (kind is None or e["kind"] == kind)]

    def states(self):
        return [e["state"] for e in self.events if e["type"] == "state"]


class FakeSTT:
    supports_partials = True

    def __init__(self):
        self.final = ""
        self.cb = None

    async def begin(self, on_partial):
        self.cb = on_partial

    async def push(self, pcm16):
        pass

    def say_partial(self, text):
        self.cb(text)

    async def finish(self):
        return self.final

    async def abort(self):
        pass

    async def aclose(self):
        pass


class FakeMainAgent:
    def __init__(self, delay=0.05):
        self.requests = []
        self.delay = delay

    async def handle(self, req):
        self.requests.append(req)
        await asyncio.sleep(self.delay)
        return MainAgentResponse(
            request_id=req.request_id,
            status="ok",
            segments=[SpokenSegment(text="Invezz reported the lawsuit on November 16. It names two executives.",
                                    tone="measured", citation_ids=["X1"])],
            citations=[{"id": "X1", "publication": "Invezz", "headline": "h", "url": "https://x", "published_at": ""}],
            follow_up="Want more, or back to the briefing?",
        )


def make(followup=0.05, play_s=0.02, delay=0.05):
    settings = replace(Settings(), followup_wait_s=followup, hold_line_after_s=1.0, fake_audio=True)
    client = FakeClient(play_s)
    stt = FakeSTT()
    agent = FakeMainAgent(delay)
    s = VoiceSession("s1", parse_briefing(BRIEFING), tts=FakeTTS(), stt=stt, router=Router(None),
                     main_agent=agent, settings=settings, transport=client, rng=random.Random(0))
    client.session = s
    client.start()
    return s, client, stt, agent


async def wait_state(s, state, timeout=5.0):
    async def poll():
        while s.state != state:
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), timeout)


@pytest.mark.asyncio
async def test_plays_whole_briefing_and_ends():
    s, client, *_ = make()
    await s.start()
    await wait_state(s, State.ENDED)
    texts = [e["text"] for e in client.spoken("script")]
    assert texts[0].startswith("Alpha one") and "Bravo two" in texts[-1]
    assert client.audio_bytes > 0


@pytest.mark.asyncio
async def test_flow_control_limits_audio_ahead():
    s, client, *_ = make(play_s=0.2)
    await s.start()
    await asyncio.sleep(0.1)
    assert len(s._sent) <= s.s.max_chunks_ahead
    await s.close()


@pytest.mark.asyncio
async def test_barge_in_escalates_then_resumes_at_interrupted_sentence():
    s, client, stt, agent = make(play_s=0.15)
    await s.start()
    # let the listener hear into the second sentence of story A
    while s.resume_index < 1:
        await asyncio.sleep(0.005)
    interrupted_at = s.resume_index
    interrupted_text = s.plan[interrupted_at].text

    await s.on_speech_start()
    assert s.state == State.SUSPECT
    assert any(e["type"] == "state" and e["state"] == "suspect" for e in client.events)
    stt.say_partial("can you dig")
    await wait_state(s, State.LISTENING)
    assert any(e["type"] == "flush" for e in client.events)

    stt.final = "can you dig deeper into this story"
    await s.on_speech_end()
    await wait_state(s, State.AWAITING)

    # said it doesn't know yet, asked the desk, spoke the answer with citations
    acks = client.spoken("ack")
    assert acks and "don't" in acks[0]["text"].lower()
    assert agent.requests and agent.requests[0].needs == ["deep_dive"]
    assert agent.requests[0].context["segment_id"] == "a"
    answers = client.spoken("answer")
    assert any(a["citations"] and a["citations"][0]["url"] == "https://x" for a in answers)

    # then bridges back and replays the interrupted sentence first
    await wait_state(s, State.NARRATING)
    after_bridge = None
    while after_bridge is None:
        starts = [e for e in client.events if e["type"] == "chunk_start"]
        kinds = [e["kind"] for e in starts]
        if "bridge" in kinds:
            idx = len(kinds) - 1 - kinds[::-1].index("bridge")
            if idx + 1 < len(starts):
                after_bridge = starts[idx + 1]
        await asyncio.sleep(0.005)
    assert after_bridge["text"] == interrupted_text
    await s.close()


@pytest.mark.asyncio
async def test_false_alarm_unducks_and_continues():
    s, client, stt, _ = make(play_s=0.05)
    await s.start()
    await asyncio.sleep(0.03)
    await s.on_speech_start()
    stt.say_partial("mm")
    stt.final = "mm-hm"
    await s.on_speech_end()
    await asyncio.sleep(0.02)
    assert any(e["type"] == "unduck" for e in client.events)
    assert not any(e["type"] == "flush" for e in client.events)
    await wait_state(s, State.ENDED)
    assert not client.spoken("ack")


@pytest.mark.asyncio
async def test_typed_pause_then_resume():
    s, client, *_ = make(play_s=0.05)
    await s.start()
    await asyncio.sleep(0.03)
    await s.on_text("hold on")
    await wait_state(s, State.PAUSED)
    at = s.resume_index
    await s.on_control("resume")
    await wait_state(s, State.ENDED)
    assert client.spoken("bridge")
    script_after = [e["text"] for e in client.spoken("script")]
    assert s.plan[at].text in script_after


@pytest.mark.asyncio
async def test_skip_moves_to_next_story():
    s, client, *_ = make(play_s=0.05)
    await s.start()
    await asyncio.sleep(0.03)
    await s.on_text("skip this")
    await wait_state(s, State.ENDED)
    starts = client.spoken()
    skip_idx = next(i for i, e in enumerate(starts) if e["text"] == "Okay, moving on.")
    assert starts[skip_idx + 1]["segment_id"] == "b"
