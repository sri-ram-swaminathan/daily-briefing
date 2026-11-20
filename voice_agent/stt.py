"""Speech-to-text for the listener's voice (ElevenLabs Scribe).

The browser runs a voice-activity detector and only streams microphone audio
while the listener is talking (plus a short pre-roll), so we pay for speech,
not silence, and the narrator's own voice is never transcribed as a question.

Two backends:

* ``ScribeRealtimeSTT``: one websocket per session to
  ``/v1/speech-to-text/realtime``. Partial transcripts arrive while the
  listener is still speaking; the session uses the first real words to
  confirm a barge-in (so a cough doesn't stop the briefing).
* ``ScribeBatchSTT``: buffers the utterance and POSTs it to
  ``/v1/speech-to-text`` when the listener stops. No partials; the session
  falls back to a duration rule to confirm barge-ins.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import logging
import wave
from collections.abc import Callable
from typing import Protocol

import httpx

from .config import Settings

log = logging.getLogger(__name__)

PartialCallback = Callable[[str], None]


class STT(Protocol):
    supports_partials: bool

    async def begin(self, on_partial: PartialCallback) -> None: ...
    async def push(self, pcm16: bytes) -> None: ...
    async def finish(self) -> str: ...
    async def abort(self) -> None: ...
    async def aclose(self) -> None: ...


class ScribeRealtimeSTT:
    supports_partials = True

    def __init__(self, settings: Settings, language: str | None = None) -> None:
        self.s = settings
        self.language = language
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._on_partial: PartialCallback | None = None
        self._committed: asyncio.Queue[str] = asyncio.Queue()
        self._active = False
        self._lock = asyncio.Lock()

    async def _ensure_connected(self) -> None:
        if self._ws is not None:
            return
        import websockets  # pure-python, imported lazily

        params = [
            f"model_id={self.s.stt_model}",
            f"audio_format=pcm_{self.s.stt_sample_rate}",
            "commit_strategy=manual",
        ]
        if self.language:
            params.append(f"language_code={self.language}")
        url = f"{self.s.elevenlabs_ws_url}/v1/speech-to-text/realtime?{'&'.join(params)}"
        self._ws = await websockets.connect(
            url, additional_headers={"xi-api-key": self.s.elevenlabs_api_key}, max_size=2**22
        )
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        ws = self._ws
        try:
            async for raw in ws:
                msg = json.loads(raw)
                kind = msg.get("message_type")
                if kind == "partial_transcript":
                    if self._active and self._on_partial and msg.get("text"):
                        self._on_partial(msg["text"])
                elif kind in ("committed_transcript", "committed_transcript_with_timestamps"):
                    await self._committed.put(msg.get("text", ""))
                elif kind == "insufficient_audio_activity":
                    await self._committed.put("")
                elif kind in ("session_started", "warning"):
                    log.debug("scribe %s: %s", kind, msg)
                elif kind and ("error" in kind or kind in {"quota_exceeded", "rate_limited", "commit_throttled"}):
                    log.warning("scribe error %s: %s", kind, msg.get("error"))
                    await self._committed.put("")
        except Exception as exc:  # connection dropped; reconnect on next utterance
            log.info("scribe connection closed: %s", exc)
        finally:
            self._ws = None

    async def _send(self, pcm16: bytes, commit: bool) -> None:
        await self._ws.send(
            json.dumps(
                {
                    "message_type": "input_audio_chunk",
                    "audio_base_64": base64.b64encode(pcm16).decode(),
                    "commit": commit,
                    "sample_rate": self.s.stt_sample_rate,
                }
            )
        )

    async def begin(self, on_partial: PartialCallback) -> None:
        async with self._lock:
            await self._ensure_connected()
            while not self._committed.empty():
                self._committed.get_nowait()
            self._on_partial = on_partial
            self._active = True

    async def push(self, pcm16: bytes) -> None:
        if not self._active or self._ws is None:
            return
        with contextlib.suppress(Exception):
            await self._send(pcm16, commit=False)

    async def finish(self) -> str:
        if not self._active:
            return ""
        self._active = False
        if self._ws is None:
            return ""
        try:
            # 100 ms of silence carries the commit flag
            await self._send(bytes(self.s.stt_sample_rate // 10 * 2), commit=True)
            return (await asyncio.wait_for(self._committed.get(), timeout=4.0)).strip()
        except (asyncio.TimeoutError, Exception) as exc:
            log.warning("scribe commit failed: %s", exc)
            return ""

    async def abort(self) -> None:
        self._active = False
        self._on_partial = None

    async def aclose(self) -> None:
        self._active = False
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._reader:
            self._reader.cancel()


def pcm_to_wav(pcm16: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16)
    return buf.getvalue()


class ScribeBatchSTT:
    supports_partials = False

    def __init__(self, settings: Settings, language: str | None = None) -> None:
        self.s = settings
        self.language = language
        self._buf = bytearray()
        self._active = False
        self._client = httpx.AsyncClient(
            base_url=settings.elevenlabs_base_url,
            headers={"xi-api-key": settings.elevenlabs_api_key},
            timeout=20.0,
        )

    async def begin(self, on_partial: PartialCallback) -> None:
        self._buf.clear()
        self._active = True

    async def push(self, pcm16: bytes) -> None:
        if self._active:
            self._buf += pcm16

    async def finish(self) -> str:
        if not self._active:
            return ""
        self._active = False
        if len(self._buf) < self.s.stt_sample_rate // 5 * 2:  # < 200 ms: noise
            return ""
        data = {"model_id": self.s.stt_batch_model}
        if self.language:
            data["language_code"] = self.language
        files = {"file": ("utterance.wav", pcm_to_wav(bytes(self._buf), self.s.stt_sample_rate), "audio/wav")}
        try:
            resp = await self._client.post("/v1/speech-to-text", data=data, files=files)
            resp.raise_for_status()
            return (resp.json().get("text") or "").strip()
        except Exception as exc:
            log.warning("scribe batch failed: %s", exc)
            return ""

    async def abort(self) -> None:
        self._active = False
        self._buf.clear()

    async def aclose(self) -> None:
        await self._client.aclose()


class FallbackSTT:
    """Realtime first; if the websocket can't be opened, switch to batch for the session."""

    def __init__(self, settings: Settings, language: str | None = None) -> None:
        self._rt = ScribeRealtimeSTT(settings, language)
        self._batch = ScribeBatchSTT(settings, language)
        self._impl: STT = self._rt

    @property
    def supports_partials(self) -> bool:
        return self._impl.supports_partials

    async def begin(self, on_partial: PartialCallback) -> None:
        if self._impl is self._rt:
            try:
                await self._rt.begin(on_partial)
                return
            except Exception as exc:
                log.warning("realtime STT unavailable (%s); falling back to batch", exc)
                self._impl = self._batch
        await self._impl.begin(on_partial)

    async def push(self, pcm16: bytes) -> None:
        await self._impl.push(pcm16)

    async def finish(self) -> str:
        return await self._impl.finish()

    async def abort(self) -> None:
        await self._impl.abort()

    async def aclose(self) -> None:
        await self._rt.aclose()
        await self._batch.aclose()


class NullSTT:
    """No speech recognition (no API key). Typed questions and push-to-talk text still work."""

    supports_partials = False

    async def begin(self, on_partial: PartialCallback) -> None: ...
    async def push(self, pcm16: bytes) -> None: ...
    async def finish(self) -> str:
        return ""
    async def abort(self) -> None: ...
    async def aclose(self) -> None: ...


def make_stt(settings: Settings, language: str | None = None) -> STT:
    if not settings.elevenlabs_api_key:
        return NullSTT()
    if settings.stt_mode == "batch":
        return ScribeBatchSTT(settings, language)
    return FallbackSTT(settings, language)
