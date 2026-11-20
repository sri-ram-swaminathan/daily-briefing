"""Stream citation-ready articles from the Liquid Briefing JSONL feeds.

By default only liquid-briefing-data/ is read: the Newstex dataset the hackathon
provided. news-data/ (extra topic feeds from tools/fetch_feeds.py: weather, climate,
sports, politics, the US midterms, finance and energy) has the same format and is
switched on with INCLUDE_EXTRA_FEEDS=1.
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import NotRequired, TypedDict


_ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = _ROOT / "liquid-briefing-data"
EXTRA_DATA_DIR = _ROOT / "news-data"
# NEWS_DATA_DIRS (os.pathsep-separated) overrides both.
INCLUDE_EXTRA = os.getenv("INCLUDE_EXTRA_FEEDS", "").strip().lower() in {"1", "true", "yes", "on"}
DEFAULT_DATA_DIRS = [DEFAULT_DATA_DIR] + ([EXTRA_DATA_DIR] if INCLUDE_EXTRA else [])

# Topic for the original feeds, which predate the "topic" field.
FEED_TOPICS = {"actionforex": "forex", "invezz_en": "markets", "invezz_de": "markets", "invezz_nl": "markets",
               "ibtimes_uk": "world", "zacks": "equities"}


def default_data_dirs() -> list[Path]:
    """NEWS_DATA_DIRS, else the local folders, synced from Hugging Face on first use (data_store.py)."""
    raw = os.getenv("NEWS_DATA_DIRS")
    if raw:
        return [Path(p) for p in raw.split(os.pathsep) if p.strip()]
    if not any(any(d.glob("*.jsonl")) for d in DEFAULT_DATA_DIRS if d.is_dir()):
        from data_store import data_dirs

        return data_dirs()
    return [d for d in DEFAULT_DATA_DIRS if d.is_dir()]


class Article(TypedDict):
    id: str
    feed: str
    publication: str
    author: str
    published_at: str
    language: str
    headline: str
    excerpt: str
    url: str
    provider_categories: list[str]
    categories: list[str]
    company_tickers: list[str]
    topic: str
    text: NotRequired[str]


def iter_articles(
    data_dir: "str | Path | list[str | Path] | None" = None,
    *,
    feed: str | None = None,
    language: str | None = None,
    include_text: bool = True,
) -> Iterator[Article]:
    """Yield JSON-serializable records in filename order, then source line order.

    data_dir is one directory, a list of them, or None for liquid-briefing-data/
    plus news-data/. Use a filename stem such as "invezz_en" or "bbc_sport" for
    feed. Set include_text=False to browse headlines and excerpts without passing
    full bodies to an agent.
    """
    if data_dir is None:
        directories = default_data_dirs()
    elif isinstance(data_dir, (list, tuple)):
        directories = [Path(d) for d in data_dir]
    else:
        directories = [Path(data_dir)]
    paths = [p for directory in directories for p in sorted(Path(directory).glob("*.jsonl"))]
    if not paths:
        raise FileNotFoundError(f"No JSONL feeds found in {', '.join(map(str, directories))}")
    if feed is not None:
        available = [path.stem for path in paths]
        if feed not in available:
            raise ValueError(f"Unknown feed {feed!r}. Available feeds: {', '.join(available)}")
        paths = [p for p in paths if p.stem == feed]

    for path in paths:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                    if language is not None and raw["language"] != language:
                        continue
                    article: Article = {
                        "id": raw["$id"],
                        "feed": path.stem,
                        "publication": raw["publication"]["name"],
                        "author": raw["source"],
                        "published_at": raw["date"],
                        "language": raw["language"],
                        "headline": raw["headline"],
                        "excerpt": raw["excerpt"],
                        "url": raw["permalink"],
                        "provider_categories": raw.get("provider_categories", []),
                        "categories": raw.get("categories", []),
                        "company_tickers": raw.get("company_tickers", []),
                        "topic": raw.get("topic") or FEED_TOPICS.get(path.stem, "general"),
                    }
                    if include_text:
                        article["text"] = raw["text_content"]
                except (ValueError, KeyError, TypeError) as exc:
                    raise ValueError(f"{path}:{line_number}: invalid article: {exc}") from exc
                yield article
