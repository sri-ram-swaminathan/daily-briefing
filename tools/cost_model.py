"""What does News Council cost for the full dataset, and at ten times the volume?

    uv run tools/cost_model.py        # writes docs/COSTS.md and site/data/costs.json

Token counts come from the real files (characters / 4 per token for English;
prompts measured from council.py and briefing.py). Prices are list prices per
million tokens / per unit, set in PRICES below; change them and rerun.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHARS_PER_TOKEN = 4.0

# USD. Edit to match your contract. Assumptions, not quotes.
PRICES = {
    "gpt-4.1-mini": {"in": 0.40, "cached_in": 0.10, "out": 1.60},
    "gpt-4.1-nano": {"in": 0.10, "cached_in": 0.025, "out": 0.40},
    "web_search_call": 0.010,          # per research-desk web search
    "elevenlabs_per_1k_chars": 0.10,   # narration, API business tier, approximate
    "openai_tts_per_1k_chars": 0.015,  # gpt-4o-mini-tts, approximate
    "sora2_per_second": 0.10,          # 720p
}

PERSONAS = 7
PERSONA_PROMPT_TOKENS = 330     # REVIEW + one persona file (~1,000 characters + wrapper)
OBS_OUT_TOKENS = 110            # one observation + quote (JSON)
MODERATOR_OUT_TOKENS = 220
BRIEF_PROMPT_TOKENS = 260       # briefing.py INSTRUCTIONS + preferences
BRIEF_EVIDENCE_TOKENS = 650     # 7 observations + quotes for one article
BRIEF_OUT_TOKENS = 90
STORIES_PER_BRIEFING = 6
WORDS_PER_STORY = 70
QUESTIONS_PER_LISTENER = 3      # interruptions answered by the research desk
DESK_IN_TOKENS = 5200           # 6 articles x ~800 tokens + context
DESK_OUT_TOKENS = 260


def dataset_stats() -> dict:
    out = {}
    for name in ("liquid-briefing-data", "news-data"):
        n = chars = 0
        for path in (ROOT / name).glob("*.jsonl"):
            for line in path.open(encoding="utf-8"):
                if line.strip():
                    r = json.loads(line)
                    n += 1
                    chars += len(r.get("text_content", "")) + len(r.get("headline", "")) + len(r.get("excerpt", ""))
        out[name] = {"articles": n, "chars": chars}
    return out


def llm(model: str, tin: float, tout: float, cached: float = 0.0) -> float:
    p = PRICES[model]
    return ((tin - cached) * p["in"] + cached * p["cached_in"] + tout * p["out"]) / 1e6


def scenario(articles: int, chars: int, readers: int) -> dict:
    art_tokens = chars / CHARS_PER_TOKEN / max(articles, 1)
    # Council, as built: 7 personas + moderator per article, gpt-4.1-mini.
    persona_in = PERSONAS * (art_tokens + PERSONA_PROMPT_TOKENS)
    moderator_in = art_tokens + PERSONAS * OBS_OUT_TOKENS + 300
    council_in = articles * (persona_in + moderator_in)
    council_out = articles * (PERSONAS * OBS_OUT_TOKENS + MODERATOR_OUT_TOKENS)
    council = llm("gpt-4.1-mini", council_in, council_out)
    # Optimised: nano triage drops ~45% of articles no persona would react to (price lists,
    # duplicate translations), and the article goes first in the prompt so 6 of 7 persona calls
    # hit the prompt cache.
    triage = llm("gpt-4.1-nano", articles * (min(art_tokens, 600) + 150), articles * 15)
    kept = articles * 0.55
    opt_in = kept * (persona_in + moderator_in)
    opt_cached = kept * (PERSONAS - 1) * art_tokens
    council_opt = triage + llm("gpt-4.1-mini", opt_in, kept * (PERSONAS * OBS_OUT_TOKENS + MODERATOR_OUT_TOKENS),
                               cached=opt_cached)
    # Personal briefing per reader: topic prefilter (free, BM25/categories) to 40 candidates, then the sweep.
    candidates = min(40, articles)
    per_reader_brief = llm("gpt-4.1-mini", candidates * (BRIEF_PROMPT_TOKENS + BRIEF_EVIDENCE_TOKENS),
                           candidates * BRIEF_OUT_TOKENS)
    narration_chars = STORIES_PER_BRIEFING * WORDS_PER_STORY * 6 + 300
    per_reader_voice = narration_chars / 1000 * PRICES["elevenlabs_per_1k_chars"]
    per_reader_voice_budget = narration_chars / 1000 * PRICES["openai_tts_per_1k_chars"]
    per_reader_desk = QUESTIONS_PER_LISTENER * (llm("gpt-4.1-mini", DESK_IN_TOKENS, DESK_OUT_TOKENS)
                                               + PRICES["web_search_call"]
                                               + 450 / 1000 * PRICES["elevenlabs_per_1k_chars"])
    per_reader = per_reader_brief + per_reader_voice + per_reader_desk
    return {
        "articles": articles, "readers": readers, "tokens_per_article": round(art_tokens),
        "council_as_built": round(council, 2), "council_optimised": round(council_opt, 2),
        "per_reader": {"briefing_selection": round(per_reader_brief, 4), "voice_elevenlabs": round(per_reader_voice, 4),
                       "voice_openai_tts": round(per_reader_voice_budget, 4), "research_desk_3_questions": round(per_reader_desk, 4),
                       "total": round(per_reader, 4)},
        "readers_total": round(per_reader * readers, 2),
        "total_as_built": round(council + per_reader * readers, 2),
        "total_optimised": round(council_opt + per_reader * readers, 2),
    }


def main() -> None:
    stats = dataset_stats()
    base = stats["liquid-briefing-data"]
    full = scenario(base["articles"], base["chars"], readers=2)
    ten = scenario(base["articles"] * 10, base["chars"] * 10, readers=20)
    daily = scenario(185, int(base["chars"] / base["articles"] * 185), readers=1000)
    daily10 = scenario(1850, int(base["chars"] / base["articles"] * 1850), readers=10000)
    reel = {"sora_seconds": 24, "video": 24 * PRICES["sora2_per_second"], "script_voice": 0.02,
            "total": round(24 * PRICES["sora2_per_second"] + 0.02, 2)}
    data = {"prices": PRICES, "dataset": stats, "full_dataset": full, "ten_x": ten, "newsroom_day": daily,
            "newsroom_day_10x": daily10, "reel": reel,
            "notes": ["Token counts from the files at 4 characters per token.",
                      "Prices are list-price assumptions in tools/cost_model.py; edit and rerun.",
                      "Council cost scales with articles; reader cost scales with readers."]}
    (ROOT / "site" / "data").mkdir(parents=True, exist_ok=True)
    (ROOT / "site" / "data" / "costs.json").write_text(json.dumps(data, indent=1), encoding="utf-8")

    def row(label, s):
        return (f"| {label} | {s['articles']:,} | ${s['council_as_built']:,.2f} | ${s['council_optimised']:,.2f} | "
                f"{s['readers']:,} | ${s['per_reader']['total']:.3f} | ${s['total_as_built']:,.2f} | ${s['total_optimised']:,.2f} |")

    md = f"""# What News Council costs

Generated by `tools/cost_model.py` from the real files. Prices are assumptions listed at the bottom; edit
`PRICES` and rerun.

## Headline numbers

| Scenario | Articles | Council (as built) | Council (optimised) | Readers | Per reader | Total (as built) | Total (optimised) |
|---|---:|---:|---:|---:|---:|---:|---:|
{row("Full dataset, once", full)}
{row("10x the dataset", ten)}
{row("One newsroom day (185 articles), 1,000 readers", daily)}
{row("10x newsroom day, 10,000 readers", daily10)}

* **Council** reviews each article once, with seven personas and a moderator on `gpt-4.1-mini`
  (~{full['tokens_per_article']:,} tokens of article text each). It's shared by every reader.
* **Optimised** adds a `gpt-4.1-nano` triage that skips articles no persona would react to (about 45%:
  price tickers, duplicated translations), and puts the article first in the prompt so six of seven
  persona calls hit OpenAI's prompt cache.
* **Per reader**: picking 6 stories from 40 prefiltered candidates (${full['per_reader']['briefing_selection']:.3f}),
  narrating about 2,800 characters with ElevenLabs (${full['per_reader']['voice_elevenlabs']:.3f}, or
  ${full['per_reader']['voice_openai_tts']:.3f} with OpenAI TTS), and three spoken follow-up questions with web search
  (${full['per_reader']['research_desk_3_questions']:.3f}).
* **Short video** (optional): one 24-second reel costs about ${reel['total']:.2f} (Sora 2 at
  ${PRICES['sora2_per_second']:.2f}/second). Make a few per day for the top stories, not one per reader.

Cost grows linearly: 10x the articles is 10x the council cost; 10x the readers is 10x the reader cost. Voice is
the largest per-reader line, so the budget tier swaps ElevenLabs for OpenAI TTS on free accounts.

## Price assumptions (USD)

```json
{json.dumps(PRICES, indent=2)}
```
"""
    (ROOT / "docs" / "COSTS.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
