"""Small in-memory BM25 index over the Liquid Briefing dataset.

1,740 articles index in well under a second, which is plenty for a demo and
keeps the research desk dependency-free. Swap in a vector store behind the
same ``search`` signature if the corpus grows.
"""

from __future__ import annotations

import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TOKEN = re.compile(r"[a-z0-9][a-z0-9'.$%-]*", re.I)
_STOP = set(
    "a an the and or of to in on for at by with from is are was were be been it its this that these those as "
    "about into over after before than then so but not no do does did has have had will would can could "
    "you me my we our they their he she his her i what who how why when where which tell more story".split()
)


def tokenize(text: str) -> list[str]:
    return [t.strip(".'-").lower() for t in _TOKEN.findall(text) if t.lower() not in _STOP and len(t) > 1]


def clean(text: str) -> str:
    return text.replace("�", "'").replace("\xa0", " ").strip()


@dataclass
class Doc:
    id: str
    feed: str
    publication: str
    author: str
    published_at: datetime
    language: str
    headline: str
    excerpt: str
    url: str
    text: str
    categories: list[str]
    topic: str = "general"

    def citation(self) -> dict:
        return {
            "id": self.id,
            "publication": self.publication,
            "headline": self.headline,
            "url": self.url,
            "published_at": self.published_at.isoformat(),
        }


class NewsIndex:
    K1, B = 1.4, 0.75
    FIELD_WEIGHTS = (("headline", 3.0), ("excerpt", 2.0), ("body", 1.0))
    BODY_CHARS = 3000

    def __init__(self, docs: list[Doc]) -> None:
        self.docs = docs
        self._tf: list[Counter[str]] = []
        self._len: list[float] = []
        df: Counter[str] = Counter()
        for d in docs:
            tf: Counter[str] = Counter()
            for name, w in self.FIELD_WEIGHTS:
                src = d.headline if name == "headline" else d.excerpt if name == "excerpt" else d.text[: self.BODY_CHARS]
                for tok in tokenize(src):
                    tf[tok] += w
            self._tf.append(tf)
            self._len.append(sum(tf.values()))
            df.update(tf.keys())
        n = max(1, len(docs))
        self._avg = sum(self._len) / n if docs else 1.0
        self._idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    @classmethod
    def from_dir(cls, data_dir: Path | None = None) -> "NewsIndex":
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from news_loader import iter_articles  # the repo's loader

        docs = []
        for a in iter_articles(data_dir):
            docs.append(
                Doc(
                    id=a["id"],
                    feed=a["feed"],
                    publication=a["publication"],
                    author=a["author"],
                    published_at=datetime.fromisoformat(a["published_at"]),
                    language=a["language"],
                    headline=clean(a["headline"]),
                    excerpt=clean(a["excerpt"]),
                    url=a["url"],
                    text=clean(a.get("text", "")),
                    categories=a.get("provider_categories", []) or [],
                    topic=a.get("topic", "general"),
                )
            )
        return cls(docs)

    @property
    def latest(self) -> datetime:
        return max((d.published_at for d in self.docs), default=datetime.now(timezone.utc))

    def get(self, doc_id: str) -> Doc | None:
        return next((d for d in self.docs if d.id == doc_id), None)

    def search(
        self,
        query: str,
        *,
        k: int = 8,
        language: str | None = "en",
        since: datetime | None = None,
        until: datetime | None = None,
        boost_ids: set[str] | None = None,
    ) -> list[tuple[float, Doc]]:
        q = tokenize(query)
        if not q:
            return []
        scored: list[tuple[float, Doc]] = []
        for i, d in enumerate(self.docs):
            if language and d.language != language:
                continue
            if since and d.published_at < since:
                continue
            if until and d.published_at > until:
                continue
            tf = self._tf[i]
            norm = self.K1 * (1 - self.B + self.B * self._len[i] / self._avg)
            s = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    s += self._idf.get(t, 0.0) * f * (self.K1 + 1) / (f + norm)
            if boost_ids and d.id in boost_ids:
                s *= 1.5
            if s > 0:
                scored.append((s, d))
        scored.sort(key=lambda x: x[0], reverse=True)
        # drop near-duplicate headlines (syndicated copies)
        out: list[tuple[float, Doc]] = []
        seen: set[str] = set()
        for s, d in scored:
            key = re.sub(r"\W+", "", d.headline.lower())[:60]
            if key in seen:
                continue
            seen.add(key)
            out.append((s, d))
            if len(out) >= k:
                break
        return out


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def resolve_time_hint(hint: str, now: datetime) -> tuple[datetime | None, datetime | None, bool]:
    """Map "last week", "yesterday", "on monday" ... to a window. Returns (since, until, is_future)."""
    h = (hint or "").lower()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if not h:
        return None, None, False
    if "next" in h:
        return now, None, True
    if "yesterday" in h:
        return day - timedelta(days=1), day, False
    if "today" in h:
        return day, None, False
    if "last month" in h or "past month" in h or "previous month" in h:
        return day - timedelta(days=31), now, False
    if "week" in h or "few days" in h:
        return day - timedelta(days=8), now, False
    for i, name in enumerate(_WEEKDAYS):
        if name in h:
            delta = (day.weekday() - i) % 7 or 7
            start = day - timedelta(days=delta)
            return start, start + timedelta(days=1), False
    return None, None, False
