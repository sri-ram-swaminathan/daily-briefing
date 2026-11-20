import asyncio
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone

from voice_agent.council_bridge import briefing_from_items, council_perspectives, describe
from voice_agent.main_agent import LocalMainAgent, MainAgentRequest
from voice_agent.presets import PRESETS, delivery_for
from voice_agent.retrieval import Doc, NewsIndex
from voice_agent.router import heuristic_route
from voice_agent.script import plan_briefing


def _council(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""CREATE TABLE observations (
            article_id TEXT NOT NULL, kind TEXT NOT NULL, persona_id TEXT NOT NULL,
            persona_version TEXT NOT NULL, model TEXT NOT NULL, url TEXT NOT NULL,
            relevant INTEGER NOT NULL, observation TEXT NOT NULL, supporting_quote TEXT NOT NULL,
            PRIMARY KEY (article_id, kind, persona_id, persona_version, model))""")
        rows = [
            ("A1", "persona", "sami", "v1", "m", "u", 1, "Old take.", "old"),
            ("A1", "persona", "sami", "v2", "m", "u", 1, "Rent is the real story.", "higher rent"),
            ("A1", "persona", "tom", "v1", "m", "u", 0, "", ""),
            ("A1", "editor", "moderator", "v1", "m", "u", 1, "They agree on costs.", "costs"),
            ("B2", "persona", "vera", "v1", "m", "u", 1, "Check the source.", "sources say"),
        ]
        db.executemany("INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def _doc(i, headline):
    return Doc(id=i, feed="f", publication="Invezz", author="", published_at=datetime(2026, 11, 17, tzinfo=timezone.utc),
               language="en", headline=headline, excerpt="Energy prices rose.", url=f"https://x/{i}",
               text="Energy prices rose across Europe this week.", categories=[])


def test_perspectives_latest_relevant_only(tmp_path):
    db = tmp_path / "council.sqlite3"
    _council(db)
    views = council_perspectives(db, ["A1"])
    assert [(v["reviewer"], v["observation"]) for v in views["A1"]] == [
        ("moderator", "They agree on costs."), ("sami", "Rent is the real story.")]
    assert "B2" not in views
    text = describe(views)
    assert "Sami: Rent is the real story." in text and "Moderator summary" in text
    assert council_perspectives(tmp_path / "missing.sqlite3", ["A1"]) == {}


def test_briefing_from_briefing_py_items():
    index = NewsIndex([_doc("A1", "European energy prices climb again as winter approaches early")])
    items = [("A1", "https://x/A1", "Energy prices rose.  Households will feel it."),
             ("Z9", "https://y/Z9", "A second story."), ("Q", "", "  ")]
    b = briefing_from_items(items, user="alex", delivery=delivery_for("Late-night radio"), index=index,
                            on=date(2026, 11, 18))
    assert b.id == "alex-2026-11-18" and len(b.segments) == 2
    assert b.segments[0].text == "Energy prices rose. Households will feel it."
    assert b.segments[0].citations[0].publication == "Invezz"
    assert b.segments[0].title.endswith("…")
    assert b.segments[1].citations[0].url == "https://y/Z9"
    assert b.delivery.tone == PRESETS["Late-night radio"]["tone"]
    assert plan_briefing(b)  # narratable


def test_router_sends_perspective_questions_to_desk():
    route = heuristic_route("What does the council think about this?")
    assert route.intent == "escalate" and "perspectives" in route.needs


class _CaptureLLM:
    def __init__(self):
        self.user = ""

    async def complete(self, *, user, **kwargs):
        self.user = user
        return {"status": "ok", "follow_up": "More?",
                "segments": [{"text": "Sami, on the council, says rent is the real story.", "tone": "warm",
                              "citation_ids": ["A1", "bogus"]}]}


def test_research_desk_sees_council_perspectives(tmp_path):
    db = tmp_path / "council.sqlite3"
    _council(db)
    llm = _CaptureLLM()
    agent = LocalMainAgent(NewsIndex([_doc("A1", "Energy prices climb")]), llm, council_db=db)
    req = MainAgentRequest(request_id="r1", session_id="s", briefing_id="b", question="what do people think?",
                           query="energy prices", needs=["perspectives"])
    resp = asyncio.run(agent.handle(req))
    assert "Sami: Rent is the real story." in llm.user
    assert resp.segments[0].citation_ids == ["A1"]
    assert [c["id"] for c in resp.citations] == ["A1"]
