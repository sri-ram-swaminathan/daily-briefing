import json
from pathlib import Path

import pytest

from voice_agent.router import heuristic_route
from voice_agent.script import group_sentences, parse_briefing, plan_briefing, split_sentences
from voice_agent.tts import voice_settings_for

ROOT = Path(__file__).resolve().parent.parent


def test_split_sentences_keeps_numbers_and_abbreviations():
    text = "US producer prices rose 0.3 percent in October. Dr. Smith said the U.S. economy is fine! Is it? Yes."
    assert split_sentences(text) == [
        "US producer prices rose 0.3 percent in October.",
        "Dr. Smith said the U.S. economy is fine!",
        "Is it?",
        "Yes.",
    ]


def test_group_sentences_merges_short_ones():
    groups = group_sentences(["Is it?", "Yes.", "This is a much longer sentence that stands on its own quite happily."])
    assert groups[0] == "Is it? Yes. This is a much longer sentence that stands on its own quite happily." or len(groups) == 2
    assert all(len(g) <= 320 for g in groups)


def test_sample_briefing_parses_and_plans():
    raw = json.loads((ROOT / "briefings" / "sample-2026-11-18.json").read_text(encoding="utf-8"))
    b = parse_briefing(raw)
    assert [s.id for s in b.segments] == ["nvidia-leadership", "intel-lawsuit", "us-ppi"]
    plan = plan_briefing(b)
    assert plan[0].kind == "intro" and plan[-1].kind == "outro"
    script = [c for c in plan if c.kind == "script"]
    assert script and all(c.citations for c in script)
    # neighbours are passed for prosody continuity
    assert script[1].previous_text == script[0].text


def test_markup_briefing():
    b = parse_briefing(
        "# Evening wrap\n@tone: upbeat\n## Crypto [tone: playful]\nBitcoin rose. Ether fell.\n"
        "[cite: Invezz | https://invezz.com/x]\n## FX\nThe yen weakened."
    )
    assert b.title == "Evening wrap" and b.delivery.tone == "upbeat"
    assert b.segments[0].tone == "playful" and b.segments[0].citations[0].url == "https://invezz.com/x"
    assert b.segments[1].text == "The yen weakened."


@pytest.mark.parametrize(
    "utterance,intent,extra",
    [
        ("hey can you dig deeper into the story", "escalate", "deep_dive"),
        ("cite your sources", "escalate", "sources"),
        ("where did that come from?", "escalate", "sources"),
        ("how did this develop?", "escalate", "timeline"),
        ("take me back to last week", "escalate", "timeline"),
        ("what happens next week?", "escalate", "timeline"),
        ("stop", "control", "stop"),
        ("hold on a second", "control", "pause"),
        ("okay carry on", "control", "resume"),
        ("skip this", "control", "skip"),
        ("say that again", "control", "repeat"),
        ("mm-hm", "backchannel", None),
        ("yeah", "backchannel", None),
    ],
)
def test_heuristic_router(utterance, intent, extra):
    r = heuristic_route(utterance)
    assert r is not None and r.intent == intent
    if intent == "escalate":
        assert extra in r.needs
    elif intent == "control":
        assert r.action == extra


def test_heuristic_router_defers_ambiguous():
    assert heuristic_route("who is colette kress") is None
    assert heuristic_route("is that a lot") is None


def test_time_hint_extracted():
    r = heuristic_route("take me back to last week, how did this develop")
    assert r.time_hint == "last week"


def test_tone_maps_to_voice_settings():
    serious = voice_settings_for("serious, measured")
    upbeat = voice_settings_for("upbeat")
    assert serious.stability > upbeat.stability
    assert upbeat.style > serious.style
    assert upbeat.speed > serious.speed
