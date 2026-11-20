"""Build extra topic feeds in the exact Liquid Briefing JSONL format.

    uv run tools/fetch_feeds.py                 # all feeds in tools/feeds.yaml -> news-data/
    uv run tools/fetch_feeds.py --only bbc_sport --per-feed 10

Each feed is read from its public RSS, every item's article page is fetched, and the
main text is extracted. One line per article, with the same fields as the files in
liquid-briefing-data/ (headline, date, language, source, excerpt, text_content,
html_content, categories, provider_categories, company_tickers, permalink,
publication, $id, ...), so news_loader.py, council.py, briefing.py and the research
desk read them without changes.

Rerunning merges: articles already on disk keep their $id and are not refetched.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0 Safari/537.36")
NS = {"dc": "http://purl.org/dc/elements/1.1/", "media": "http://search.yahoo.com/mrss/",
      "content": "http://purl.org/rss/1.0/modules/content/", "atom": "http://www.w3.org/2005/Atom"}

# Newstex-style category codes per topic. Existing Newstex codes are reused where they exist
# (POLITICS, ELECTION, ENERGY01, FINANCE1, SCIENCE0, ...); WEATHER0/CLIMATE0/SPORTS00 are ours.
TOPIC_CODES = {
    "weather": ["WEATHER0", "SCIENCE0", "NONCOMPANY"],
    "climate": ["CLIMATE0", "SCIENCE0", "ENVIRONM", "NONCOMPANY"],
    "sports": ["SPORTS00", "ENTRTAIN", "NONCOMPANY"],
    "politics": ["POLITICS", "POLSC", "NATIONAL", "NONCOMPANY"],
    "midterms": ["ELECTION", "POLITICS", "POLSC", "NATIONAL", "NONCOMPANY"],
    "finance": ["FINANCE1", "FINANCIL", "MARKECON", "BANKFINA"],
    "energy": ["ENERGY01", "UTILITIE", "COMMODIT"],
}
KEYWORDS = {
    "WEATHER0": "weather storm rain snow flood heat heatwave hurricane typhoon cyclone wind forecast drought wildfire frost cold temperature",
    "CLIMATE0": "climate emissions carbon warming cop net-zero fossil greenhouse methane decarbon",
    "SPORTS00": "match league cup goal win season coach player team championship nfl nba premier final",
    "POLITICS": "government minister parliament senate congress president policy party vote bill",
    "ELECTION": "election midterm ballot vote voters polls candidate campaign district recount seat",
    "FINANCE1": "rate rates inflation bank market stocks bond central fed ecb economy gdp investors",
    "ENERGY01": "energy oil gas power electricity grid solar wind nuclear battery coal lng renewables",
}


class TextExtractor(HTMLParser):
    """Collect <p>/<h2>/<li> text from the article body, skipping page chrome."""

    SKIP = {"script", "style", "nav", "footer", "aside", "form", "noscript", "figure", "figcaption", "header",
            "button", "svg", "iframe"}
    BLOCK = {"p", "h2", "h3", "li", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.in_block = 0
        self.buf: list[str] = []
        self.blocks: list[tuple[str, str]] = []
        self.tag_stack: list[str] = []
        self.article_depth = 0
        self.saw_article = False
        self.article_blocks: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag in ("article", "main"):
            self.article_depth += 1
            self.saw_article = True
        if tag in self.BLOCK and not self.skip:
            self.in_block += 1
            self.tag_stack.append(tag)
            self.buf = []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag in self.BLOCK and self.in_block and self.tag_stack:
            self.in_block -= 1
            t = self.tag_stack.pop()
            text = " ".join("".join(self.buf).split())
            if len(text) > 30:
                self.blocks.append((t, text))
                if self.article_depth:
                    self.article_blocks.append((t, text))
            self.buf = []
        if tag in ("article", "main") and self.article_depth:
            self.article_depth -= 1

    def handle_data(self, data):
        if self.in_block and not self.skip:
            self.buf.append(data)


BOILERPLATE = re.compile(r"(sign up|subscribe|newsletter|cookies?|all rights reserved|follow us|read more:|"
                         r"advertisement|privacy policy|terms of (use|service)|share this|copyright ©)", re.I)


def extract(page: str) -> list[tuple[str, str]]:
    p = TextExtractor()
    try:
        p.feed(page)
    except Exception:
        pass
    blocks = p.article_blocks if sum(len(t) for _, t in p.article_blocks) > 600 else p.blocks
    out, seen = [], set()
    for tag, text in blocks:
        if BOILERPLATE.search(text) and len(text) < 220:
            continue
        if text in seen:
            continue
        seen.add(text)
        out.append((tag, text))
    return out


def strip_tags(s: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


def ulid_like(seed: str) -> str:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    n = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return "".join(alphabet[(n >> (5 * i)) & 31] for i in range(26))


def parse_rss(xml: bytes) -> list[dict]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:  # some feeds ship bare "&" or stray control characters
        text = xml.decode("utf-8", errors="replace")
        text = re.sub(r"&(?!(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        root = ET.fromstring(text.encode("utf-8"))
    items = root.findall(".//item") or root.findall(".//atom:entry", NS)
    out = []
    for it in items:
        def t(path):
            el = it.find(path, NS)
            return (el.text or "").strip() if el is not None and el.text else ""
        link = t("link") or (it.find("atom:link", NS).get("href") if it.find("atom:link", NS) is not None else "")
        thumb = ""
        for path in ("media:content", "media:thumbnail", "enclosure"):
            els = it.findall(path, NS)
            if els:
                el = max(els, key=lambda e: int(e.get("width") or 0))
                if (el.get("type") or "image").startswith("image") or path != "enclosure":
                    thumb = el.get("url") or ""
                    break
        date = t("pubDate") or t("dc:date") or t("atom:published") or t("atom:updated")
        try:
            dt = parsedate_to_datetime(date) if "," in date or date[:3].isalpha() else datetime.fromisoformat(date)
        except (TypeError, ValueError):
            dt = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        out.append({
            "title": strip_tags(t("title")),
            "link": link.strip(),
            "description": strip_tags(t("description") or t("atom:summary") or t("content:encoded")),
            "author": strip_tags(t("dc:creator") or t("author") or t("atom:author/atom:name")),
            "date": dt.astimezone(timezone.utc),
            "thumb": thumb,
            "rss_categories": [strip_tags(c.text or "") for c in it.findall("category") if (c.text or "").strip()],
        })
    return out


def scores(text: str, topic_codes: list[str]) -> dict[str, int]:
    low = text.lower()
    out = {}
    for code, words in KEYWORDS.items():
        n = sum(len(re.findall(rf"\b{re.escape(w)}", low)) for w in words.split())
        bonus = 10 if code in topic_codes else 0
        if n or bonus:
            out[code] = min(60, n * 2 + bonus)
    return out


def build_record(feed: dict, item: dict, blocks: list[tuple[str, str]], received: datetime) -> dict:
    pub = feed["publication"]
    paragraphs = [t for _, t in blocks]
    text = "\n\n".join(paragraphs) if sum(map(len, paragraphs)) > 400 else item["description"]
    html_content = "".join(f"<{tag}>{html.escape(t)}</{tag}>" for tag, t in blocks) if text != item["description"] \
        else f"<p>{html.escape(item['description'])}</p>"
    excerpt = item["description"] or (text[:400].rsplit(" ", 1)[0] + "…" if len(text) > 400 else text)
    if len(excerpt) > 600:
        excerpt = excerpt[:600].rsplit(" ", 1)[0] + "…"
    code = pub["code"]
    sid = f"{code.split('-')[0]}-{pub['newstex_id']}-01{ulid_like(item['link'])[:24]}"
    topic_codes = TOPIC_CODES[feed["topic"]]
    cats = list(dict.fromkeys(topic_codes + ([("INTL" if feed.get("region") != "US" else "NATIONAL")])))
    return {
        "categories": cats,
        "category_scores": scores(item["title"] + " " + text, topic_codes),
        "company_tickers": [],
        "date": item["date"].isoformat(timespec="seconds"),
        "excerpt": excerpt,
        "external_id": item["link"],
        "feed": base64.b64encode(feed["url"].encode()).decode(),
        "headline": item["title"],
        "html_content": html_content,
        "language": feed.get("language", "en"),
        "path": f"stories/{item['date']:%Y/%m}/{sid}",
        "permalink": item["link"],
        "provider_categories": list(dict.fromkeys([feed["section"]] + item["rss_categories"][:4])),
        "provider_tickers": [],
        "pub_type": feed.get("pub_type", "News"),
        "publication": {"$type": "Publication", "$id": hashlib.md5(pub["name"].encode()).hexdigest()[:19],
                        "name": pub["name"], "newstex_id": pub["newstex_id"], "url": pub["url"], "code": code,
                        "status": "Active", "country": pub.get("country", ""), "content_type": feed.get("pub_type", "News")},
        "publisher": {"$type": "Publisher", "$id": hashlib.md5(pub["publisher"].encode()).hexdigest()[:18],
                      "name": pub["publisher"], "newstex_id": code.split("-")[0], "url": pub["url"],
                      "status": "Active", "country": pub.get("country", "")},
        "received_at": received.isoformat(timespec="seconds"),
        "source": item["author"] or pub["name"],
        "stats": {"sentiment_score": 0, "word_count": len(text.split()), "line_count": text.count("\n") + 1,
                  "character_count": len(text)},
        "status": "Active",
        "text_content": text,
        "thumb_url": item["thumb"],
        "topic": feed["topic"],
        "version": 1,
        "$type": "Story",
        "$id": sid,
    }


async def run_feed(client: httpx.AsyncClient, feed: dict, out_dir: Path, per_feed: int, sem: asyncio.Semaphore) -> tuple[str, int, int]:
    path = out_dir / f"{feed['name']}.jsonl"
    existing: dict[str, dict] = {}
    if path.exists():
        for line in path.open(encoding="utf-8"):
            if line.strip():
                r = json.loads(line)
                existing[r["permalink"]] = r
    try:
        r = await client.get(feed["url"], headers={"User-Agent": feed["user_agent"]} if feed.get("user_agent") else None,
                             timeout=60.0)
        r.raise_for_status()
        items = parse_rss(r.content)
    except Exception as exc:
        print(f"! {feed['name']}: {exc}", file=sys.stderr)
        return feed["name"], 0, len(existing)
    if kw := feed.get("must_match"):
        pat = re.compile(kw, re.I)
        items = [i for i in items if pat.search(i["title"] + " " + i["description"])]
    skip = re.compile(feed["skip"], re.I) if feed.get("skip") else None
    items = [i for i in items if i["link"] and i["title"] and not (skip and skip.search(i["link"] + " " + i["title"]))]
    received = datetime.now(timezone.utc)
    # RSS "most popular" blocks sometimes resurface years-old pieces; keep the news current.
    items = [i for i in items if (received - i["date"]).days <= feed.get("max_age_days", 120)][:per_feed]
    new = 0

    async def one(item):
        nonlocal new
        if item["link"] in existing:
            return
        async with sem:
            try:
                page = await client.get(item["link"])
                body = page.text
                if "�" in body:  # mislabelled charset (common on .gov pages)
                    body = page.content.decode("cp1252", errors="replace")
                blocks = extract(body) if page.status_code == 200 else []
            except httpx.HTTPError:
                blocks = []
            await asyncio.sleep(0.3)
        existing[item["link"]] = build_record(feed, item, blocks, received)
        new += 1

    await asyncio.gather(*(one(i) for i in items))
    rows = sorted(existing.values(), key=lambda r: r["date"])
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return feed["name"], new, len(rows)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=Path(__file__).with_name("feeds.yaml"))
    ap.add_argument("--out", type=Path, default=ROOT / "news-data")
    ap.add_argument("--per-feed", type=int, default=25)
    ap.add_argument("--only", nargs="*", help="Feed names to fetch")
    args = ap.parse_args()
    feeds = yaml.safe_load(args.config.read_text(encoding="utf-8"))["feeds"]
    if args.only:
        feeds = [f for f in feeds if f["name"] in args.only]
    args.out.mkdir(exist_ok=True)
    sem = asyncio.Semaphore(6)
    async with httpx.AsyncClient(headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.8"},
                                 follow_redirects=True, timeout=25.0) as client:
        results = await asyncio.gather(*(run_feed(client, f, args.out, args.per_feed, sem) for f in feeds))
    total = 0
    for name, new, n in sorted(results):
        total += n
        print(f"{name:32s} +{new:3d}  ({n} on disk)")
    print(f"{total} articles in {args.out} at {time.strftime('%Y-%m-%d %H:%M')} UTC")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
