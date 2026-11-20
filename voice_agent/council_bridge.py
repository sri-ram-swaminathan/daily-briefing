"""Connect the News Council pipeline to the voice agent.

    council.py  --observations-->  council.sqlite3
    briefing.py --spoken stories-->  preferences.sqlite3   (the summary agent)
                         |
                         v
    build_reader_briefing()  -> Briefing script -> VoiceSession (narrated, interruptible)
    council_perspectives()   -> extra evidence for the research desk ("what do people think?")

``briefing.py`` is imported lazily (it needs the OpenAI SDK); everything else
here is stdlib so it can be tested offline.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import ExitStack, closing
from datetime import date
from pathlib import Path
from typing import Any

from .retrieval import NewsIndex
from .script import Briefing, Citation, Delivery, Segment

# A morning briefing reads well at five or six stories.
MAX_STORIES = 6


def _readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def council_perspectives(db_path: Path, article_ids: Iterable[str]) -> dict[str, list[dict[str, str]]]:
    """Latest relevant observation per reviewer for each article, as briefing.py reads them."""
    ids = sorted({i for i in article_ids if i})
    if not ids or not db_path.is_file():
        return {}
    marks = ",".join("?" * len(ids))
    try:
        with closing(_readonly(db_path)) as db:
            rows = db.execute(f"""
                SELECT article_id, kind, persona_id, observation, supporting_quote FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY article_id, kind, persona_id ORDER BY rowid DESC
                    ) AS revision FROM observations WHERE article_id IN ({marks})
                ) WHERE revision = 1 AND relevant = 1
                ORDER BY article_id, kind, persona_id""", ids).fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, list[dict[str, str]]] = {}
    for article_id, kind, persona, observation, quote in rows:
        out.setdefault(article_id, []).append(
            {"reviewer": persona, "kind": kind, "observation": observation, "quote": quote})
    return out


def _title(headline: str, n: int) -> str:
    words = headline.split()
    return " ".join(words[:8]) + ("…" if len(words) > 8 else "") if words else f"Story {n}"


def briefing_from_items(
    items: Iterable[tuple[str, str, str]],
    *,
    user: str,
    delivery: Delivery,
    index: NewsIndex | None = None,
    on: date | None = None,
) -> Briefing:
    """Turn briefing.py output, (article_id, url, spoken_text) tuples, into a voice script."""
    on = on or date.today()
    segments: list[Segment] = []
    for article_id, url, spoken in items:
        text = " ".join(spoken.split())
        if not text:
            continue
        doc = index.get(article_id) if index else None
        citation = Citation.from_any(doc.citation()) if doc else Citation(id=article_id, url=url)
        n = len(segments) + 1
        segments.append(Segment(id=f"s{n}-{article_id}", text=text,
                                title=_title(doc.headline if doc else "", n), citations=[citation]))
    return Briefing(
        id=f"{user}-{on.isoformat()}",
        title=f"{user.title()}'s briefing",
        segments=segments,
        delivery=delivery,
        listener=user,
        date=on.isoformat(),
        intro=f"Morning, {user.title()}. Here's what's worth knowing today." if segments else "",
        outro="That's the briefing. Ask me about any of it, any time.",
    )


def save_preferences(preferences_db: Path, user: str, interests: str) -> None:
    from briefing import init_preferences  # lazy: needs the OpenAI SDK

    with closing(sqlite3.connect(preferences_db)) as db:
        init_preferences(db)
        with db:
            db.execute("""INSERT INTO preferences VALUES (?, ?) ON CONFLICT(user_id)
                          DO UPDATE SET interests=excluded.interests""", (user, interests))


def load_preferences(preferences_db: Path, user: str) -> str:
    if not preferences_db.is_file():
        return ""
    try:
        with closing(_readonly(preferences_db)) as db:
            row = db.execute("SELECT interests FROM preferences WHERE user_id=?", (user,)).fetchone()
    except sqlite3.Error:
        return ""
    return row[0] if row else ""


def reader_stories(
    *, user: str, council_db: Path, preferences_db: Path, model: str, sweep_new: bool,
    index: NewsIndex | None = None, feeds: set[str] | None = None,
) -> tuple[list[tuple[str, str, str]], int]:
    """Run briefing.py's sweep for new stories, then fall back to the reader's latest saved ones.

    Returns (stories, number_of_new_stories).
    """
    import briefing  # lazy: needs the OpenAI SDK

    with ExitStack() as stack:
        db = stack.enter_context(closing(sqlite3.connect(preferences_db)))
        briefing.init_preferences(db)
        new: list[tuple[str, str, str]] = []
        if sweep_new:
            if not council_db.is_file():
                raise ValueError("No council reviews yet. Run `uv run council.py --limit 5` first.")
            council = stack.enter_context(closing(_readonly(council_db)))
            client = None

            def get_client():
                nonlocal client
                if client is None:
                    from openai import OpenAI

                    client = stack.enter_context(OpenAI())
                return client

            # Judge every candidate, then keep the freshest: stopping at the first matches would
            # favour whichever feed sorts first by article id.
            new = list(briefing.sweep(council, db, user, model, get_client, voice=True))
            if feeds is not None and index is not None:  # e.g. only the hackathon dataset
                new = [item for item in new if (doc := index.get(item[0])) and doc.feed in feeds]
            if index is not None:
                def published(item):
                    doc = index.get(item[0])
                    return doc.published_at.isoformat() if doc else ""

                new.sort(key=published, reverse=True)
            new = new[:MAX_STORIES]
        if new:
            return new, len(new)
        rows = db.execute("""SELECT article_id, url, summary, spoken_text FROM decisions
                             WHERE user_id=? AND summary != '' ORDER BY rowid DESC LIMIT ?""",
                          (user, MAX_STORIES)).fetchall()
        saved = [(a, u, spoken or briefing.legacy_spoken_text(summary)) for a, u, summary, spoken in reversed(rows)]
        return saved, 0


def build_reader_briefing(
    *, user: str, delivery: Delivery, index: NewsIndex | None, council_db: Path, preferences_db: Path,
    model: str, sweep_new: bool = True,
) -> tuple[Briefing, int]:
    stories, new = reader_stories(user=user, council_db=council_db, preferences_db=preferences_db,
                                  model=model, sweep_new=sweep_new, index=index)
    return briefing_from_items(stories, user=user, delivery=delivery, index=index), new


def describe(perspectives: dict[str, list[dict[str, Any]]]) -> str:
    """Prompt text for the research desk."""
    lines = []
    for article_id, views in perspectives.items():
        for v in views:
            who = "Moderator summary" if v["kind"] == "editor" else v["reviewer"].title()
            lines.append(f"[{article_id}] {who}: {v['observation']} (quote: \"{v['quote']}\")")
    return "\n".join(lines)
