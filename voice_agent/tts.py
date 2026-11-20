"""Text-to-speech: ElevenLabs streaming TTS with tone-aware voice settings.

Design notes
------------
* Output is raw 16-bit mono PCM at 24 kHz. The browser schedules it with the
  Web Audio API, which gives sample-accurate gapless playback and lets us fade
  out in ~120 ms when the listener interrupts (no clicks, no mid-word chop).
* Every request carries ``previous_text`` / ``next_text`` so prosody flows
  across sentence boundaries even though each sentence is its own request.
* Audio is cached by (text, voice, model, settings). Resuming after an
  interruption replays the interrupted sentence from cache: zero latency and
  zero extra characters billed.
* The tone string from the script ("serious", "upbeat but measured", ...) is
  mapped onto ElevenLabs voice settings. With ``eleven_v3`` the tone is also
  passed as an inline audio tag, which that model understands natively.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

import httpx

from .config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoiceSettings:
    stability: float = 0.5
    similarity_boost: float = 0.75
    style: float = 0.15
    speed: float = 1.0
    use_speaker_boost: bool = True

    def to_json(self) -> dict:
        return {
            "stability": round(self.stability, 2),
            "similarity_boost": round(self.similarity_boost, 2),
            "style": round(self.style, 2),
            "speed": round(min(1.2, max(0.7, self.speed)), 2),
            "use_speaker_boost": self.use_speaker_boost,
        }


# keyword -> (stability delta, style delta, speed delta)
# Deltas are deliberately small. High stability is what makes ElevenLabs sound
# flat and robotic, so "serious"/"calm" tones slow down a touch instead of
# locking the voice into a monotone.
_TONE_TABLE: dict[str, tuple[float, float, float]] = {
    "serious": (+0.06, -0.03, -0.03),
    "sombre": (+0.08, -0.04, -0.06),
    "somber": (+0.08, -0.04, -0.06),
    "grave": (+0.08, -0.04, -0.06),
    "calm": (+0.04, 0.0, -0.04),
    "soothing": (+0.04, +0.02, -0.06),
    "relaxed": (0.0, +0.04, -0.05),
    "gentle": (+0.03, +0.02, -0.05),
    "measured": (+0.03, 0.0, -0.02),
    "authoritative": (+0.06, +0.04, -0.02),
    "neutral": (+0.04, -0.03, 0.0),
    "warm": (-0.03, +0.08, -0.03),
    "conversational": (-0.06, +0.08, 0.0),
    "friendly": (-0.05, +0.1, +0.01),
    "upbeat": (-0.08, +0.15, +0.04),
    "energetic": (-0.1, +0.2, +0.06),
    "excited": (-0.1, +0.22, +0.06),
    "urgent": (-0.04, +0.12, +0.05),
    "brisk": (0.0, +0.04, +0.06),
    "playful": (-0.1, +0.22, +0.02),
    "curious": (-0.05, +0.12, 0.0),
    "reassuring": (+0.03, +0.05, -0.04),
}

# Baseline: a relaxed, unhurried read. Lower stability = more natural
# intonation; a little style = expressive without being theatrical.
_BASE_STABILITY = 0.38
_BASE_STYLE = 0.22
_BASE_SPEED = 0.95


def voice_settings_for(tone: str, pace: float = 1.0) -> VoiceSettings:
    tone_l = (tone or "").lower()
    stab, style, speed = _BASE_STABILITY, _BASE_STYLE, _BASE_SPEED * pace
    for word, (ds, dst, dsp) in _TONE_TABLE.items():
        if word in tone_l:
            stab += ds
            style += dst
            speed += dsp
    return VoiceSettings(
        stability=min(0.6, max(0.25, stab)),
        similarity_boost=0.8,
        style=min(0.5, max(0.05, style)),
        speed=min(1.1, max(0.82, speed)),
    )


def v3_tag_for(tone: str) -> str:
    """Inline audio tag for eleven_v3 (e.g. "[serious]")."""
    first = (tone or "").split(",")[0].strip().lower()
    return f"[{first}] " if first and len(first) < 24 else ""


class TTSEngine(Protocol):
    sample_rate: int

    def stream(
        self,
        text: str,
        *,
        tone: str,
        pace: float = 1.0,
        voice_id: str | None = None,
        fast: bool = False,
        previous_text: str = "",
        next_text: str = "",
        language: str | None = None,
    ) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


class _LRU:
    def __init__(self, max_bytes: int = 64 * 1024 * 1024) -> None:
        self._data: OrderedDict[str, bytes] = OrderedDict()
        self._size = 0
        self._max = max_bytes

    def get(self, key: str) -> bytes | None:
        val = self._data.get(key)
        if val is not None:
            self._data.move_to_end(key)
        return val

    def put(self, key: str, val: bytes) -> None:
        if key in self._data:
            return
        self._data[key] = val
        self._size += len(val)
        while self._size > self._max and self._data:
            _, old = self._data.popitem(last=False)
            self._size -= len(old)


PIECE_BYTES = 4800  # 100 ms of 24 kHz 16-bit mono


async def _replay(blob: bytes) -> AsyncIterator[bytes]:
    for i in range(0, len(blob), PIECE_BYTES):
        yield blob[i : i + PIECE_BYTES]
        await asyncio.sleep(0)


class ElevenLabsTTS:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.sample_rate = settings.tts_sample_rate
        self._client = httpx.AsyncClient(
            base_url=settings.elevenlabs_base_url,
            headers={"xi-api-key": settings.elevenlabs_api_key},
            timeout=httpx.Timeout(30.0, connect=5.0),
        )
        self._cache = _LRU()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _body(self, text, tone, pace, model, previous_text, next_text, language) -> dict:
        is_v3 = model.startswith("eleven_v3")
        body: dict = {
            "text": (v3_tag_for(tone) + text) if is_v3 else text,
            "model_id": model,
            "voice_settings": voice_settings_for(tone, pace).to_json(),
        }
        if not is_v3:
            if previous_text:
                body["previous_text"] = previous_text[-600:]
            if next_text:
                body["next_text"] = next_text[:600]
        if language and model.startswith(("eleven_flash", "eleven_turbo")):
            body["language_code"] = language
        return body

    async def stream(
        self,
        text: str,
        *,
        tone: str,
        pace: float = 1.0,
        voice_id: str | None = None,
        fast: bool = False,
        previous_text: str = "",
        next_text: str = "",
        language: str | None = None,
    ) -> AsyncIterator[bytes]:
        voice = voice_id or self.s.voice_id
        model = self.s.tts_fast_model if fast else self.s.tts_model
        body = self._body(text, tone, pace, model, previous_text, next_text, language)
        key = hashlib.sha256(json.dumps([voice, body], sort_keys=True).encode()).hexdigest()
        cached = self._cache.get(key)
        if cached is not None:
            async for piece in _replay(cached):
                yield piece
            return

        collected = bytearray()
        carry = b""
        complete = False
        try:
            async with self._client.stream(
                "POST",
                f"/v1/text-to-speech/{voice}/stream",
                params={"output_format": f"pcm_{self.sample_rate}"},
                json=body,
            ) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread())[:300]
                    raise RuntimeError(f"ElevenLabs TTS {resp.status_code}: {detail!r}")
                async for raw in resp.aiter_bytes():
                    data = carry + raw
                    cut = len(data) - (len(data) % 2)  # keep 16-bit samples whole
                    carry = data[cut:]
                    if cut:
                        collected += data[:cut]
                        yield data[:cut]
            complete = True
        finally:
            if complete and collected:
                self._cache.put(key, bytes(collected))


class FakeTTS:
    """Silent audio with realistic duration (~15 characters per second). For dev and tests."""

    def __init__(self, sample_rate: int = 24000, realtime: bool = False) -> None:
        self.sample_rate = sample_rate
        self.realtime = realtime
        self.requests: list[dict] = []

    async def aclose(self) -> None:
        return None

    async def stream(self, text: str, **kw) -> AsyncIterator[bytes]:
        self.requests.append({"text": text, **kw})
        seconds = max(0.4, len(text) / 15 / (kw.get("pace") or 1.0))
        total = int(seconds * self.sample_rate) * 2
        for i in range(0, total, PIECE_BYTES):
            if self.realtime:
                await asyncio.sleep(0.02)
            else:
                await asyncio.sleep(0)
            yield bytes(min(PIECE_BYTES, total - i))


def make_tts(settings: Settings) -> TTSEngine:
    if settings.fake_audio or not settings.elevenlabs_api_key:
        if not settings.fake_audio:
            log.warning("ELEVENLABS_API_KEY not set: using silent FakeTTS")
        return FakeTTS(settings.tts_sample_rate, realtime=True)
    return ElevenLabsTTS(settings)
