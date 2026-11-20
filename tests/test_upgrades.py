"""The research desk's web search and memory, the extra feeds, and the website APIs."""

import asyncio
import json
from pathlib import Path

import pytest

from voice_agent.main_agent import LocalMainAgent, MainAgentRequest, _cap_words, _speakable, SpokenSegment
from voice_agent.retrieval import NewsIndex

ROOT = Path(__file__).resolve().parent.parent


def test_speakable_strips_markdown_and_citation_links_but_keeps_words():
    text = ("It peaks at the end of [2026](https://x.org/a). Its effects ([lemonde.fr](https://lemonde.fr/x)) "
            "are wide. Two questions: 1. **Leadership** and 2. China https://example.com/y")
    assert _speakable(text) == ("It peaks at the end of 2026. Its effects are wide. "
                                "Two questions: Leadership and China")


def test_cap_words_cuts_at_a_sentence():
    segs = [SpokenSegment(text="One two three. Four five six."), SpokenSegment(text="Seven eight nine.")]
    _cap_words(segs, 5)
    assert [s.text for s in segs] == ["One two three."]


class FakeLLM:
    """Stands in for OpenAIJSON: records the call, returns a web-backed answer."""

    def __init__(self):
        self.calls = []

    async def complete(self, *, user, **kwargs):
        self.calls.append({"user": user, **kwargs})
        return {"status": "ok", "segments": [{"text": "Democrats won the House ([apnews.com](https://apnews.com/a)).",
                                              "tone": "", "citation_ids": ["web"]}],
                "follow_up": "Want more?", "_web_citations": [{"url": "https://apnews.com/a", "title": "AP"}]}


def test_desk_searches_the_web_when_feeds_have_nothing_and_cites_it():
    llm = FakeLLM()
    agent = LocalMainAgent(NewsIndex([]), llm, web_search=True)
    resp = asyncio.run(agent.handle(MainAgentRequest(request_id="r", session_id="s", briefing_id="b",
                                                     question="Who won the House?", query="Who won the House?")))
    assert resp.status == "ok"
    assert llm.calls[0]["web_search"] == "required"  # no articles: the web is the only source
    assert resp.segments[0].text == "Democrats won the House."
    assert resp.citations == [{"id": "web-1", "publication": "apnews.com", "headline": "AP",
                               "url": "https://apnews.com/a", "published_at": "", "web": True}]
    assert resp.segments[0].citation_ids == ["web-1"]


def test_go_deeper_is_anchored_to_the_previous_question():
    llm = FakeLLM()
    agent = LocalMainAgent(NewsIndex([]), llm, web_search=True)
    req = MainAgentRequest(request_id="r", session_id="s", briefing_id="b", question="go deeper", query="go deeper",
                           needs=["deep_dive"],
                           history=[{"question": "What happened in the midterms?", "answer": "Democrats won."}])
    asyncio.run(agent.handle(req))
    prompt = llm.calls[0]["user"]
    assert "already told; do not repeat" in prompt and "Democrats won." in prompt


def test_without_web_search_an_empty_index_is_not_found():
    agent = LocalMainAgent(NewsIndex([]), FakeLLM(), web_search=False)
    resp = asyncio.run(agent.handle(MainAgentRequest(request_id="r", session_id="s", briefing_id="b",
                                                     question="anything", query="anything")))
    assert resp.status == "not_found"


def _data_root() -> Path | None:
    import os

    for d in [ROOT] + [Path(p).parent for p in os.getenv("NEWS_DATA_DIRS", "").split(os.pathsep) if p]:
        if (d / "news-data").is_dir() and (d / "liquid-briefing-data").is_dir():
            return d
    return None


@pytest.mark.skipif(_data_root() is None, reason="news data not synced (private HF bucket)")
def test_extra_feeds_match_the_liquid_briefing_schema():
    data = _data_root()
    original = json.loads((data / "liquid-briefing-data" / "actionforex.jsonl").open(encoding="utf-8").readline())
    required = {"headline", "date", "language", "source", "excerpt", "text_content", "html_content", "categories",
                "provider_categories", "company_tickers", "permalink", "publication", "$id", "category_scores"}
    assert required <= set(original)
    for path in sorted((data / "news-data").glob("*.jsonl")):
        for line in path.open(encoding="utf-8"):
            rec = json.loads(line)
            assert required <= set(rec), path.name
            assert rec["permalink"].startswith("https://") and rec["publication"]["name"]
            assert rec["topic"] in {"weather", "climate", "sports", "politics", "midterms", "finance", "energy"}


def test_ask_and_health_endpoints(monkeypatch):
    from fastapi.testclient import TestClient

    from voice_agent import server

    monkeypatch.setenv("FAKE_AUDIO", "1")
    with TestClient(server.app) as client:
        llm = FakeLLM()
        server.S.local_agent = LocalMainAgent(NewsIndex([]), llm, web_search=True)
        server.S.router.llm = None  # heuristic routing only, no network
        assert client.get("/api/health").json()["ok"]
        first = client.post("/api/ask", json={"question": "Who won the House?"}).json()
        assert first["answer"] == "Democrats won the House." and first["citations"][0]["web"]
        client.post("/api/ask", json={"question": "go deeper", "session_id": first["session_id"]})
        assert "Who won the House?" in llm.calls[1]["user"]  # the session remembered the first turn
        assert client.post("/api/ask", json={}).status_code == 422
