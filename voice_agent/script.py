"""Briefing scripts from the summary agent, and how they are cut up for speech.

The summary agent hands over a script that says *what* to say and *how* to say
it. Two input formats are accepted:

1. JSON (preferred)::

    {
      "id": "morning-2026-11-18",
      "title": "Tuesday markets briefing",
      "language": "en",
      "listener": "retail investor, ten minutes",
      "delivery": {"tone": "warm, measured", "style": "plain words, no jargon", "pace": 1.0},
      "intro": "Good morning...",
      "segments": [
        {"id": "nvda", "title": "Nvidia's leadership crisis", "tone": "serious",
         "text": "...", "citations": [{"id": "...", "publication": "Invezz", "url": "...", ...}]}
      ],
      "outro": "That's the briefing..."
    }

2. Plain text with light markup::

    # Tuesday markets briefing
    @tone: warm, measured
    @style: plain words, no jargon
    ## Nvidia's leadership crisis [tone: serious]
    Paragraph text...
    [cite: Invezz | https://invezz.com/... ]

Speech is generated sentence by sentence ("chunks"). Chunking is what makes
interruptions land cleanly: playback can stop anywhere, and the briefing
resumes from the start of the sentence the listener was hearing.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Citation:
    id: str
    publication: str = ""
    headline: str = ""
    url: str = ""
    published_at: str = ""

    @classmethod
    def from_any(cls, raw: Any) -> "Citation":
        if isinstance(raw, str):
            return cls(id=raw, url=raw if raw.startswith("http") else "")
        return cls(
            id=str(raw.get("id") or raw.get("$id") or raw.get("url") or ""),
            publication=raw.get("publication", ""),
            headline=raw.get("headline", ""),
            url=raw.get("url") or raw.get("permalink", ""),
            published_at=raw.get("published_at") or raw.get("date", ""),
        )


@dataclass
class Delivery:
    tone: str = "warm, conversational, unhurried"
    style: str = ""
    pace: float = 1.0
    voice_id: str | None = None


@dataclass
class Segment:
    id: str
    text: str
    title: str = ""
    tone: str = ""
    citations: list[Citation] = field(default_factory=list)


@dataclass
class Briefing:
    id: str
    title: str
    segments: list[Segment]
    delivery: Delivery = field(default_factory=Delivery)
    language: str = "en"
    listener: str = ""
    date: str = ""
    intro: str = ""
    outro: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def segment(self, segment_id: str) -> Segment | None:
        return next((s for s in self.segments if s.id == segment_id), None)

    def all_citations(self) -> list[Citation]:
        seen: dict[str, Citation] = {}
        for seg in self.segments:
            for c in seg.citations:
                seen.setdefault(c.id, c)
        return list(seen.values())


@dataclass
class Chunk:
    """One unit of speech: usually one sentence, sometimes two short ones."""

    id: str
    segment_id: str
    text: str
    tone: str
    kind: str = "script"  # script | intro | outro | answer | ack | bridge | hold
    pause_after_ms: int = 0
    previous_text: str = ""
    next_text: str = ""
    citations: list[Citation] = field(default_factory=list)

    def wire(self) -> dict[str, Any]:
        return {
            "chunk_id": self.id,
            "segment_id": self.segment_id,
            "kind": self.kind,
            "text": self.text,
            "tone": self.tone,
            "pause_after_ms": self.pause_after_ms,
            "citations": [asdict(c) for c in self.citations],
        }


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_briefing(payload: str | dict[str, Any]) -> Briefing:
    if isinstance(payload, str):
        stripped = payload.strip()
        if stripped.startswith("{"):
            return _from_json(json.loads(stripped))
        return _from_markup(stripped)
    return _from_json(payload)


def _slug(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40] or fallback


def _from_json(raw: dict[str, Any]) -> Briefing:
    delivery_raw = raw.get("delivery") or {}
    delivery = Delivery(
        tone=delivery_raw.get("tone") or raw.get("tone") or Delivery.tone,
        style=delivery_raw.get("style") or raw.get("style", ""),
        pace=float(delivery_raw.get("pace", 1.0)),
        voice_id=delivery_raw.get("voice_id"),
    )
    segments = []
    for i, seg in enumerate(raw.get("segments") or []):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        segments.append(
            Segment(
                id=str(seg.get("id") or _slug(seg.get("title", ""), f"s{i + 1}")),
                text=text,
                title=seg.get("title", ""),
                tone=seg.get("tone", ""),
                citations=[Citation.from_any(c) for c in seg.get("citations") or []],
            )
        )
    if not segments:
        raise ValueError("briefing script has no segments with text")
    title = raw.get("title") or "News briefing"
    return Briefing(
        id=str(raw.get("id") or _slug(title, "briefing")),
        title=title,
        segments=segments,
        delivery=delivery,
        language=raw.get("language", "en"),
        listener=raw.get("listener", ""),
        date=raw.get("date", ""),
        intro=raw.get("intro", ""),
        outro=raw.get("outro", ""),
    )


_SEG_HEADER = re.compile(r"^##\s+(?P<title>.*?)\s*(?:\[tone:\s*(?P<tone>[^\]]+)\])?\s*$")
_CITE = re.compile(r"^\[cite:\s*(?P<body>[^\]]+)\]\s*$", re.I)
_DIRECTIVE = re.compile(r"^@(?P<key>\w+):\s*(?P<value>.+)$")


def _from_markup(text: str) -> Briefing:
    title = "News briefing"
    delivery = Delivery()
    segments: list[Segment] = []
    current: Segment | None = None
    lines_buf: list[str] = []

    def close() -> None:
        nonlocal current, lines_buf
        if current is not None:
            current.text = " ".join(" ".join(lines_buf).split())
            if current.text:
                segments.append(current)
        current, lines_buf = None, []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("# ") and not segments and current is None:
            title = line[2:].strip()
        elif m := _DIRECTIVE.match(line):
            key, value = m["key"].lower(), m["value"].strip()
            if key == "tone":
                delivery.tone = value
            elif key == "style":
                delivery.style = value
            elif key == "pace":
                delivery.pace = float(value)
            elif key == "voice":
                delivery.voice_id = value
        elif m := _SEG_HEADER.match(line):
            close()
            current = Segment(
                id=_slug(m["title"], f"s{len(segments) + 1}"), text="", title=m["title"], tone=m["tone"] or ""
            )
        elif m := _CITE.match(line):
            if current is None:
                current = Segment(id=f"s{len(segments) + 1}", text="")
            parts = [p.strip() for p in m["body"].split("|")]
            url = next((p for p in parts if p.startswith("http")), "")
            pub = next((p for p in parts if not p.startswith("http")), "")
            current.citations.append(Citation(id=url or pub, publication=pub, url=url))
        else:
            if current is None:
                current = Segment(id=f"s{len(segments) + 1}", text="")
            lines_buf.append(line)
    close()
    if not segments:
        raise ValueError("briefing script has no segments with text")
    return Briefing(id=_slug(title, "briefing"), title=title, segments=segments, delivery=delivery)


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------

# Split after . ! ? (optionally followed by a closing quote/bracket) when the next
# sentence starts with an uppercase letter, digit or quote. Avoid common
# abbreviations and decimals ("0.3%", "U.S.", "Dr.").
_ABBREV = {"mr", "mrs", "ms", "dr", "st", "vs", "inc", "corp", "ltd", "co", "jr", "sr", "u.s", "u.k", "e.g", "i.e", "no"}
_SENT_END = re.compile(r"(?<=[.!?…])[\"'”’)\]]*\s+(?=[\"'“‘(\[]?[A-Z0-9])")


def split_sentences(text: str) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    out: list[str] = []
    start = 0
    for m in _SENT_END.finditer(text):
        candidate = text[start : m.start()].strip()
        last_word = candidate.rsplit(" ", 1)[-1].rstrip(".!?\"'”’)]").lower()
        if last_word in _ABBREV or re.fullmatch(r"[a-z]", last_word):
            continue
        out.append(text[start : m.end()].strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def group_sentences(sentences: list[str], min_chars: int = 70, max_chars: int = 320) -> list[str]:
    """Merge very short sentences so each TTS request has enough context to sound natural."""
    groups: list[str] = []
    buf = ""
    for s in sentences:
        if not buf:
            buf = s
        elif len(buf) < min_chars and len(buf) + len(s) + 1 <= max_chars:
            buf = f"{buf} {s}"
        else:
            groups.append(buf)
            buf = s
    if buf:
        if groups and len(buf) < min_chars // 2 and len(groups[-1]) + len(buf) + 1 <= max_chars:
            groups[-1] = f"{groups[-1]} {buf}"
        else:
            groups.append(buf)
    return groups


def chunk_text(
    text: str,
    *,
    segment_id: str,
    tone: str,
    kind: str,
    id_prefix: str,
    citations: list[Citation] | None = None,
    pause_after_ms: int = 0,
) -> list[Chunk]:
    pieces = group_sentences(split_sentences(text))
    chunks = []
    for i, piece in enumerate(pieces):
        chunks.append(
            Chunk(
                id=f"{id_prefix}.{i}",
                segment_id=segment_id,
                text=piece,
                tone=tone,
                kind=kind,
                previous_text=pieces[i - 1] if i > 0 else "",
                next_text=pieces[i + 1] if i + 1 < len(pieces) else "",
                citations=list(citations or []),
                # breathe a little between sentences; longer at the end of a block
                pause_after_ms=pause_after_ms if i == len(pieces) - 1 else 120,
            )
        )
    return chunks


def plan_briefing(briefing: Briefing) -> list[Chunk]:
    """Flatten a briefing into the ordered list of chunks the narrator will speak."""
    base_tone = briefing.delivery.tone
    plan: list[Chunk] = []
    if briefing.intro:
        plan += chunk_text(briefing.intro, segment_id="_intro", tone=base_tone, kind="intro",
                           id_prefix="intro", pause_after_ms=500)
    for seg in briefing.segments:
        plan += chunk_text(
            seg.text,
            segment_id=seg.id,
            tone=seg.tone or base_tone,
            kind="script",
            id_prefix=seg.id,
            citations=seg.citations,
            pause_after_ms=700,  # a beat between stories
        )
    if briefing.outro:
        plan += chunk_text(briefing.outro, segment_id="_outro", tone=base_tone, kind="outro",
                           id_prefix="outro")
    return plan
