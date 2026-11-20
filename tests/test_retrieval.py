import asyncio
from datetime import datetime, timezone

import pytest

from voice_agent.config import get_settings
from voice_agent.main_agent import LocalMainAgent, MainAgentRequest
from voice_agent.retrieval import NewsIndex, resolve_time_hint


@pytest.fixture(scope="module")
def index():
    try:
        return NewsIndex.from_dir(get_settings().data_dir)
    except (RuntimeError, FileNotFoundError) as exc:  # data lives in a private HF bucket, not in git
        pytest.skip(f"news data not available: {exc}")


def test_index_loads_dataset(index):
    original = {"actionforex", "ibtimes_uk", "invezz_de", "invezz_en", "invezz_nl", "zacks"}
    assert sum(d.feed in original for d in index.docs) == 1740
    # news-data/ (INCLUDE_EXTRA_FEEDS=1) adds topic feeds: weather, climate, sports, politics, midterms, ...
    extra = {d.topic for d in index.docs if d.feed not in original}
    assert not extra or {"weather", "climate", "sports", "politics", "midterms", "finance", "energy"} <= extra


def test_search_finds_intel_lawsuit(index):
    hits = index.search("Intel shareholder lawsuit share offering", k=5)
    heads = " ".join(d.headline.lower() for _, d in hits)
    assert "intel" in heads and "lawsuit" in heads


def test_resolve_time_hint():
    now = datetime(2026, 11, 18, 9, tzinfo=timezone.utc)
    since, until, future = resolve_time_hint("last week", now)
    assert since.day == 10 and not future
    assert resolve_time_hint("next week", now)[2] is True
    since, until, _ = resolve_time_hint("on monday", now)
    assert since.day == 16 and until.day == 17


def test_local_agent_timeline_without_llm(index):
    agent = LocalMainAgent(index, llm=None)
    req = MainAgentRequest(request_id="r1", session_id="s", briefing_id="b", question="how did this develop?",
                           query="Jensen Huang Nvidia resignation", needs=["timeline"], as_of="2026-11-18T07:00:00+00:00")
    resp = asyncio.run(agent.handle(req))
    assert resp.status == "ok"
    assert resp.citations and all(c["url"].startswith("http") for c in resp.citations)
    dates = [c["published_at"] for c in resp.citations]
    assert dates == sorted(dates)  # timelines read in date order


def test_local_agent_future_request(index):
    agent = LocalMainAgent(index, llm=None)
    req = MainAgentRequest(request_id="r2", session_id="s", briefing_id="b", question="what happens next week",
                           query="Nvidia", needs=["timeline"], time_hint="next week")
    resp = asyncio.run(agent.handle(req))
    assert resp.status == "not_found" and "next week" in resp.segments[0].text
