"""Export everything the static website (site/) shows, from the real pipeline.

    uv run tools/build_site_data.py                # all steps
    uv run tools/build_site_data.py --skip-audio --skip-qa

Steps
1. Readers   Saves the two demo readers' preferences (Max, finance; Lena, student) and runs
             briefing.py's sweep over the council database for each (voice_agent.council_bridge).
2. Edition   Every reviewed article with its council reactions and moderator summary.
3. Audio     Narrates each reader's stories with gpt-4o-mini-tts (site/audio/<reader>/<n>.mp3).
4. Q&A       Asks the research desk (feeds + web search) the demo follow-up questions, so the
             static site can replay a real conversation without a server.
5. Desk      Coverage per topic/feed, link-check results, costs, reels.

Licence: only headlines, links, our own summaries and the council's short quotes are exported,
never article text or publisher excerpts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import os  # noqa: E402

from model_config import load_models  # noqa: E402
from voice_agent.council_bridge import council_perspectives, reader_stories, save_preferences  # noqa: E402
from voice_agent.retrieval import NewsIndex  # noqa: E402

SITE = ROOT / "site"
DATA = SITE / "data"
COUNCIL_DB = Path(os.getenv("COUNCIL_DB", ROOT / "council.sqlite3"))
PREFS_DB = Path(os.getenv("PREFERENCES_DB", ROOT / "preferences.sqlite3"))
# The hand-in asks for briefings "from the dataset": the demo readers draw only on the Newstex feeds.
# The extra topic feeds still power the council explorer and the research desk.
DATASET_FEEDS = {"actionforex", "ibtimes_uk", "invezz_de", "invezz_en", "invezz_nl", "zacks"}
INCLUDE_EXTRA = os.getenv("INCLUDE_EXTRA_FEEDS", "").strip().lower() in {"1", "true", "yes", "on"}

READERS = {
    "max": {
        "name": "Max", "age": 31, "avatar": "assets/people/max.png",
        "role": "Rates trader in Frankfurt",
        "tagline": "Up at 5:40, two screens before coffee. Wants the trade, not the think-piece.",
        "voice": "ash",
        "voice_style": "Confident, crisp and quick, like a sharp morning markets desk. Numbers land clearly.",
        "interests": ("I trade rates and macro. I follow the Fed and the ECB, US inflation prints like PPI and CPI, "
                      "gold, oil and natural gas, big tech earnings such as Nvidia and Intel, crypto, and anything "
                      "that moves the dollar or European bonds. Give me the market-relevant angle and what to watch. "
                      "Skip celebrity gossip, sports, weather, lifestyle, local UK housing policy and "
                      "human-interest stories."),
        "about": "INVZ-10009737-01M7QNB4C561ZWTB5S14DXZCKM",  # the story playing when Max interrupts
        "questions": [
            "Go deeper on the Nvidia earnings: what are the two questions investors need answered?",
            "And what happened when they actually reported?",
        ],
    },
    "lena": {
        "name": "Lena", "age": 21, "avatar": "assets/people/lena.png",
        "role": "Second-year geography student",
        "tagline": "Bike, then the 7:52 train. Rent, exams, a part-time job and a planet to worry about.",
        "voice": "coral",
        "voice_style": "Warm, friendly and a little wry, like a smart older friend on the walk to class.",
        "interests": ("I'm a university student. I care about public transport, trains and travel disruption, storms "
                      "and weather on my commute, rent, housing and energy bills, jobs and pay for young people, "
                      "universities, schools and exams, climate change and politics that affects young people. "
                      "Skip stock tips, earnings previews, crypto trading, celebrity gossip, and market data such as "
                      "producer prices, gas inventories or central-bank speeches unless the story says how it "
                      "changes bills, rent or jobs for ordinary people."),
        "about": "IBTU-10010345-01M7QZNEZQG7NB3TRBHJ2Z63TP",
        "questions": [
            "Where does that figure on young people out of work come from?",
            "Go deeper: is anyone actually doing something about it?",
        ],
    },
}

COUNCIL = [
    {"id": "mira", "name": "Mira", "age": 41, "role": "Epidemiologist", "home": "Big city, one child",
     "lens": "What's the evidence, compared to what?", "values": "Accuracy, the common good",
     "blind_spot": "Can sound like a lecturer and underweight lived experience", "color": "sky"},
    {"id": "tom", "name": "Tom", "age": 54, "role": "Runs a family bakery", "home": "Small town, 8 staff",
     "lens": "What does it cost, and who pays?", "values": "Hard work, responsibility, security",
     "blind_spot": "Sees every new rule as a burden", "color": "butter"},
    {"id": "amara", "name": "Amara", "age": 29, "role": "Tenants' & climate organizer", "home": "Mid-sized city",
     "lens": "Who benefits, who is harmed, who is missing?", "values": "Fairness, dignity, practical wins",
     "blind_spot": "Can assume bad intent where there is just complexity", "color": "blush"},
    {"id": "walter", "name": "Walter", "age": 67, "role": "Retired history teacher", "home": "Village, church, sports club",
     "lens": "Is this change necessary, and what might we lose?", "values": "Tradition, community, duty",
     "blind_spot": "Nostalgia; can dismiss young people's problems", "color": "sage"},
    {"id": "sami", "name": "Sami", "age": 23, "role": "Food-delivery rider, design student", "home": "Shared flat, big city",
     "lens": "What does this mean for people my age?", "values": "Freedom, loyalty, security they don't have",
     "blind_spot": "Cynicism; trusts viral takes too fast", "color": "peach", "pronouns": "they/them"},
    {"id": "vera", "name": "Vera", "age": 48, "role": "Ex-newspaper journalist", "home": "Suburb, freelance",
     "lens": "Why this story, why now, why framed this way?", "values": "Independence, truth",
     "blind_spot": "Can treat everything as spin", "color": "lavender"},
    {"id": "elias", "name": "Elias", "age": 58, "role": "Philosophy teacher", "home": "Runs evening philosophy cafés",
     "lens": "Which values collide underneath?", "values": "Wisdom, meaning, independent thought",
     "blind_spot": "Drifts into abstraction", "color": "mint"},
]


_NAMES = re.compile(r"(" + "|".join(p["id"] for p in COUNCIL) + r")")


def capitalize_names(text: str) -> str:
    """briefing.py's perspective sentence can name a reviewer by file id ("vera"); print it as a name."""
    return _NAMES.sub(lambda m: m.group(1).title(), text)


def doc_card(doc) -> dict:
    return {"id": doc.id, "headline": doc.headline, "publication": doc.publication, "url": doc.url,
            "published_at": doc.published_at.isoformat(), "topic": doc.topic, "feed": doc.feed}


def reviewed_ids() -> list[str]:
    with closing(sqlite3.connect(COUNCIL_DB)) as db:
        return [r[0] for r in db.execute("SELECT DISTINCT article_id FROM observations")]


def build_edition(index: NewsIndex) -> dict:
    ids = reviewed_ids()
    views = council_perspectives(COUNCIL_DB, ids)
    stories = []
    for aid in ids:
        doc = index.get(aid)
        if not doc or aid not in views:
            continue
        if doc.feed not in DATASET_FEEDS and not INCLUDE_EXTRA:
            continue
        reactions = [v for v in views[aid] if v["kind"] == "persona"]
        editor = next((v for v in views[aid] if v["kind"] == "editor"), None)
        if len(reactions) < 2:
            continue
        stories.append({**doc_card(doc), "reactions": [{"reviewer": v["reviewer"], "observation": v["observation"],
                                                         "quote": v["quote"]} for v in reactions],
                        "moderator": editor["observation"] if editor else ""})
    stories.sort(key=lambda s: s["published_at"], reverse=True)
    return {"stories": stories}


def build_readers(index: NewsIndex) -> dict:
    model = load_models().main_agent
    out = {}
    for rid, r in READERS.items():
        save_preferences(PREFS_DB, rid, r["interests"])
        with closing(sqlite3.connect(PREFS_DB)) as db:  # fresh demo run: forget earlier picks
            with db:
                for table in ("decisions", "perspective_results"):
                    db.execute(f"DELETE FROM {table} WHERE user_id=?", (rid,))
                db.execute("DELETE FROM perspective_notes WHERE user_id=?", (rid,))
        items, new = reader_stories(user=rid, council_db=COUNCIL_DB, preferences_db=PREFS_DB, model=model,
                                    sweep_new=True, index=index, feeds=DATASET_FEEDS)
        print(f"{rid}: {len(items)} stories ({new} new)", flush=True)
        stories = []
        for aid, url, spoken in items:
            doc = index.get(aid)
            stories.append({**(doc_card(doc) if doc else {"id": aid, "url": url, "headline": ""}),
                            "summary": capitalize_names(" ".join(spoken.split()))})
        out[rid] = {k: v for k, v in r.items() if k not in ("voice", "about")} | {"stories": stories}
    return out


def add_claims(readers: dict, index: NewsIndex) -> None:
    """Every sentence of every story -> its supporting quote in the source article (voice_agent/claims.py)."""
    from openai import OpenAI

    from voice_agent.claims import link_claims

    client = OpenAI()
    model = load_models().main_agent
    for rid, r in readers.items():
        for s in r["stories"]:
            if s.get("claims") and s.get("claims_for") == s["summary"]:
                continue
            doc = index.get(s["id"])
            if not doc:
                continue
            views = [v for v in council_perspectives(COUNCIL_DB, [s["id"]]).get(s["id"], []) if v["kind"] == "persona"]
            try:
                s["claims"] = link_claims(client, model, s["summary"],
                                          {"headline": doc.headline, "excerpt": doc.excerpt, "text": doc.text}, views)
                s["claims_for"] = s["summary"]
            except Exception as exc:  # keep the story; it still links to its source as a whole
                print(f"  claims failed for {s['id']}: {exc}")
                continue
            ok = sum(c["verified"] for c in s["claims"])
            print(f"  {rid}: {ok}/{len(s['claims'])} sentences quote-verified · {s['headline'][:50]}", flush=True)


def narrate(readers: dict) -> None:
    from openai import OpenAI

    client = OpenAI()
    for rid, r in readers.items():
        folder = SITE / "audio" / rid
        folder.mkdir(parents=True, exist_ok=True)
        intro = f"Morning, {r['name']}. Here's your News Council briefing."
        parts = [intro] + [s["summary"] for s in r["stories"]]
        for n, text in enumerate(parts):
            dest = folder / f"{n}.mp3"
            if dest.exists():
                continue
            with client.audio.speech.with_streaming_response.create(
                    model="gpt-4o-mini-tts", voice=READERS[rid]["voice"], input=text,
                    instructions=READERS[rid]["voice_style"], response_format="mp3") as resp:
                resp.stream_to_file(dest)
            print(f"  audio {dest.relative_to(SITE)}", flush=True)
        r["audio"] = [f"audio/{rid}/{n}.mp3" for n in range(len(parts))]


async def ask_desk(index: NewsIndex, readers: dict) -> dict:
    from voice_agent.config import get_settings
    from voice_agent.llm import OpenAIJSON
    from voice_agent.main_agent import LocalMainAgent, MainAgentRequest
    from voice_agent.router import RouteContext, Router

    settings = get_settings()
    agent = LocalMainAgent(index, OpenAIJSON(settings.main_agent_model), council_db=COUNCIL_DB, web_search=True)
    router = Router(OpenAIJSON(settings.router_model))
    out = {}
    for rid, r in readers.items():
        history, turns = [], []
        # Like the voice app: the question interrupts a story, so the desk knows what "that" is.
        about = index.get(READERS[rid].get("about", ""))
        context = {"segment_title": about.headline, "segment_text": about.excerpt,
                   "citations": [about.citation()]} if about else {}
        for q in READERS[rid]["questions"]:
            prev = f"Q: {history[-1]['question']} A: {history[-1]['answer']}" if history else ""
            route = await router.route(q, RouteContext("", context.get("segment_title", ""),
                                                       context.get("segment_text", ""), "", previous_exchange=prev))
            req = MainAgentRequest(request_id=f"{rid}-{len(turns)}", session_id=rid, briefing_id=rid, question=q,
                                   query=route.query or q, needs=route.needs, time_hint=route.time_hint,
                                   history=list(history), delivery={"tone": "clear, friendly"}, context=context)
            resp = await agent.handle(req)
            answer = " ".join(s.text for s in resp.segments)
            history.append({"question": q, "answer": answer})
            turns.append({"question": q, "answer": answer, "follow_up": resp.follow_up, "needs": route.needs,
                          "citations": resp.citations, "during": about.headline if about else ""})
            print(f"  {rid}: {q[:60]} -> {len(answer.split())} words, {len(resp.citations)} sources", flush=True)
        out[rid] = turns
    return out


def coverage(index: NewsIndex) -> dict:
    by_topic = Counter(d.topic for d in index.docs)
    feeds: dict[str, dict] = {}
    for d in index.docs:
        f = feeds.setdefault(d.feed, {"feed": d.feed, "publication": d.publication, "topic": d.topic, "articles": 0,
                                      "original": d.feed in {"actionforex", "ibtimes_uk", "invezz_de", "invezz_en",
                                                             "invezz_nl", "zacks"}})
        f["articles"] += 1
    return {"total": len(index.docs), "by_topic": dict(by_topic.most_common()), "feeds": sorted(
        feeds.values(), key=lambda f: (not f["original"], f["topic"], f["feed"]))}


def links() -> dict:
    out = {}
    names = ["link_check_liquid_briefing_data"] + (["link_check_news_data"] if INCLUDE_EXTRA else [])
    for name in names:
        path = ROOT / "data_quality" / f"{name}.json"
        if not path.exists():
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        bad = [{k: a.get(k) for k in ("feed", "date", "headline", "url", "status", "http", "error", "page_title")}
               for a in raw["articles"] if a["status"] != "ok"]
        out[name] = {"summary": raw["summary"], "not_working": bad}
    return out


def reels() -> list[dict]:
    out = []
    dest_root = SITE / "reels"
    for meta_path in sorted((ROOT / "reels").glob("*/reel.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta["article"]["id"].split("-")[0] not in {"ACTI", "IBTU", "INVZ", "ZACK"} and not INCLUDE_EXTRA:
            continue  # reel made from an extra feed
        video = meta_path.parent / "reel.mp4"
        dest = dest_root / meta_path.parent.name
        if video.exists():  # freshly rendered: export it
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(video, dest / "reel.mp4")
            shutil.copy2(meta_path.parent / "poster.jpg", dest / "poster.jpg")
        elif not (dest / "reel.mp4").exists():  # rendered elsewhere and never exported
            continue
        out.append({"slug": meta_path.parent.name, "audience": meta["audience"], "article": meta["article"],
                    "hook": meta["plan"]["hook"], "caption": meta["plan"]["post_caption"],
                    "hashtags": meta["plan"]["hashtags"], "video_model": meta.get("video_model"),
                    "duration_s": meta.get("duration_s"), "video": f"reels/{dest.name}/reel.mp4",
                    "poster": f"reels/{dest.name}/poster.jpg"})
    return out


def write(name: str, data) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / name).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"wrote site/data/{name}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-readers", action="store_true", help="Reuse site/data/readers.json")
    ap.add_argument("--skip-audio", action="store_true")
    ap.add_argument("--skip-qa", action="store_true")
    ap.add_argument("--skip-claims", action="store_true")
    args = ap.parse_args()
    index = NewsIndex.from_dir(None)
    write("council.json", COUNCIL)
    write("edition.json", build_edition(index))
    if args.skip_readers and (DATA / "readers.json").exists():
        readers = json.loads((DATA / "readers.json").read_text(encoding="utf-8"))
    else:
        readers = build_readers(index)
    if not args.skip_claims:
        add_claims(readers, index)
    if not args.skip_audio:
        narrate(readers)
    write("readers.json", readers)
    if not args.skip_qa:
        write("qa.json", asyncio.run(ask_desk(index, readers)))
    write("coverage.json", coverage(index))
    write("links.json", links())
    write("reels.json", reels())


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    main()
