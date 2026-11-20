"""Check that every article's permalink still resolves.

    uv run tools/check_links.py                      # the original Liquid Briefing feeds
    uv run tools/check_links.py --data-dir news-data # the extra feeds

Writes data_quality/<name>.json (one row per article) and data_quality/<name>.md
(a readable report grouped by feed). Only headlines, IDs and links are written,
never article text, so the report is safe to publish.

Two passes:
1. httpx, concurrent, follows redirects.
2. curl, for every link: some publishers (Invezz, behind Cloudflare) refuse Python's
   TLS fingerprint but serve curl, and a 200 can still be a soft 404. curl reads the
   page <title> / og:title and compares it with the headline.

Statuses
    ok           2xx and the page title matches the headline
    redirected   2xx, but the final URL is a different page (e.g. a section front
                 or the home page), so the citation no longer points at the story
    broken       404 / 410 / other 4xx-5xx, or DNS / TLS / connection failure
    soft_404     2xx, but the page is an error page or a different story
    blocked      401 / 403 / 429 / 999, or a bot-wall challenge page (Imperva, Cloudflare),
                 even for slow sequential curl: the publisher refuses every automated client,
                 so the link can't be verified by script and needs a check in a browser

    uv run tools/check_links.py --recheck   # re-try blocked/soft-404 rows one at a time, slowly
"""

from __future__ import annotations

import argparse
import asyncio
import html as htmllib
import json
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0 Safari/537.36")
HEADERS = {"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.8"}
BLOCKED = {401, 403, 429, 999}


def load(data_dir: Path) -> list[dict]:
    rows = []
    for path in sorted(data_dir.glob("*.jsonl")):
        for line in path.open(encoding="utf-8"):
            if not line.strip():
                continue
            raw = json.loads(line)
            rows.append({"id": raw["$id"], "feed": path.stem, "publication": raw["publication"]["name"],
                         "date": raw["date"], "headline": raw["headline"], "url": raw.get("permalink") or ""})
    return rows


def _path(url: str) -> str:
    p = urlsplit(url)
    return (p.netloc.removeprefix("www.") + p.path).rstrip("/").lower()


async def check(client: httpx.AsyncClient, row: dict, sem: asyncio.Semaphore) -> dict:
    url = row["url"]
    out = dict(row, status="broken", http=None, final_url="", error="")
    if not url.startswith(("http://", "https://")):
        out["error"] = "missing or non-http permalink"
        return out
    async with sem:
        for attempt in range(3):
            try:
                r = await client.get(url)
                out["http"] = r.status_code
                out["final_url"] = str(r.url)
                if r.status_code in BLOCKED or r.status_code >= 500:
                    if attempt < 2:
                        await asyncio.sleep(2 + 3 * attempt)
                        continue
                break
            except httpx.HTTPError as exc:
                out["error"] = f"{type(exc).__name__}: {exc}"[:200]
                if attempt < 2:
                    await asyncio.sleep(1 + attempt)
                    continue
    code = out["http"]
    if code is None:
        out["status"] = "broken"
    elif code in BLOCKED:
        out["status"] = "blocked"
    elif 200 <= code < 300:
        moved = out["final_url"] and _path(out["final_url"]) != _path(url)
        # Same article under a canonical URL (tracking params stripped, trailing slash) is fine.
        landing = moved and (urlsplit(out["final_url"]).path.strip("/").count("/") < 1)
        out["status"] = "redirected" if landing else "ok"
    else:
        out["status"] = "broken"
    return out


_TITLE = re.compile(r'<title[^>]*>(.*?)</title>|<meta[^>]+property="og:title"[^>]+content="([^"]*)"', re.I | re.S)
_ERROR_PAGE = re.compile(r"\b(page not found|404|not found|no longer available|seite nicht gefunden|"
                         r"pagina niet gevonden|error)\b", re.I)
# Bot walls answer 200 or 429 with a challenge page: Imperva ("Pardon Our Interruption"),
# Cloudflare ("Just a moment...", "Attention Required!"), Akamai ("Access Denied").
_CHALLENGE = re.compile(r"pardon our interruption|just a moment|attention required|access denied|"
                        r"are you a robot|captcha|security check", re.I)
_WORD = re.compile(r"[^\W_]{3,}", re.U)


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(htmllib.unescape(text))}


def curl_check(row: dict) -> dict:
    """Second opinion with curl: status, final URL and whether the page title matches the headline."""
    cmd = ["curl", "-s", "-L", "--max-time", "30", "--compressed", "-A", BROWSER_UA,
           "-H", "Accept: text/html,application/xhtml+xml", "-H", "Accept-Language: en-US,en;q=0.8",
           "-w", "\n@@%{http_code} %{url_effective}", row["url"]]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=45).stdout.decode("utf-8", "replace")
    except (subprocess.TimeoutExpired, OSError) as exc:
        return dict(row, curl_http=None, page_title="", curl_error=str(exc)[:120])
    body, _, tail = out.rpartition("\n@@")
    code, _, final = tail.partition(" ")
    titles = [a or b for a, b in _TITLE.findall(body[:200_000])]
    title = " ".join(htmllib.unescape(titles[0]).split())[:200] if titles else ""
    return dict(row, curl_http=int(code) if code.isdigit() else None, curl_final=final.strip(), page_title=title)


def classify(row: dict) -> dict:
    code = row.get("curl_http") or row.get("http")
    if _CHALLENGE.search(row.get("page_title", "")):
        row["status"] = "blocked"
        row["error"] = f"bot wall: {row['page_title'][:60]}"
        return row
    if code is None:
        row["status"] = "broken"
    elif code in BLOCKED:
        row["status"] = "blocked"
    elif not (200 <= code < 300):
        row["status"] = "broken"
        row["http"] = code
    else:
        row["http"] = code
        head, title = _words(row["headline"]), _words(row.get("page_title", ""))
        overlap = len(head & title) / max(1, min(len(head), 8))
        if row.get("page_title") and (overlap < 0.25 or (_ERROR_PAGE.search(row["page_title"]) and overlap < 0.5)):
            row["status"] = "soft_404"
            row["error"] = f"page title: {row['page_title'][:100]}"
        elif row["status"] != "redirected":
            row["status"] = "ok"
    return row


def report(rows: list[dict], title: str, checked_at: str) -> str:
    by_feed: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_feed[r["feed"]].append(r)
    total = Counter(r["status"] for r in rows)
    lines = [f"# {title}", "",
             f"Checked {len(rows)} permalinks on {checked_at} (UTC) with a browser user agent, "
             "following redirects, up to 3 attempts each.", "",
             "| Feed | Articles | Working | Redirected away | Soft 404 / wrong page | Broken | Blocked |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for feed, items in sorted(by_feed.items()):
        c = Counter(r["status"] for r in items)
        lines.append(f"| {feed} | {len(items)} | {c['ok']} | {c['redirected']} | {c['soft_404']} | {c['broken']} "
                     f"| {c['blocked']} |")
    lines.append(f"| **All** | **{len(rows)}** | **{total['ok']}** | **{total['redirected']}** | "
                 f"**{total['soft_404']}** | **{total['broken']}** | **{total['blocked']}** |")
    lines += ["", "*Working* means HTTP 2xx and the page title matches the article headline "
              "(or a person confirmed the page; see data_quality/manual_review.json). "
              "*Soft 404* means HTTP 2xx but the page is an error page or a different story. "
              "*Blocked* means the publisher refused every automated client (HTTP 401/403/429), "
              "so the link needs a manual check in a browser.", ""]
    for label, status in (("Broken links", "broken"), ("Soft 404 or wrong page", "soft_404"),
                          ("Redirected away from the article", "redirected"),
                          ("Blocked by the publisher (needs a manual check)", "blocked")):
        bad = [r for r in rows if r["status"] == status]
        if not bad:
            continue
        lines += [f"## {label} ({len(bad)})", "", "| Feed | Date | Headline | HTTP | Detail |", "|---|---|---|---:|---|"]
        for r in sorted(bad, key=lambda r: (r["feed"], r["date"])):
            head = r["headline"].replace("|", "\\|")[:110]
            detail = (r["error"] or (f"→ {r['final_url']}" if r["final_url"] and r["final_url"] != r["url"] else ""))
            detail = detail.replace("|", "\\|")[:120]
            lines.append(f"| {r['feed']} | {r['date'][:10]} | [{head}]({r['url']}) | {r['http'] or '–'} | {detail} |")
        lines.append("")
    return "\n".join(lines)


REVIEWS = ROOT / "data_quality" / "manual_review.json"


def apply_reviews(results: list[dict]) -> int:
    """Human verdicts (data_quality/manual_review.json) override the script, by URL or by host."""
    if not REVIEWS.exists():
        return 0
    data = json.loads(REVIEWS.read_text(encoding="utf-8"))
    by_url, by_host = data.get("reviews", {}), data.get("hosts", {})
    n = 0
    for row in results:
        verdict = by_url.get(row["url"])
        if verdict is None and row["status"] != "ok":
            verdict = by_host.get(urlsplit(row["url"]).netloc)
        if verdict:
            row["status"] = verdict["status"]
            row["error"] = verdict.get("note", row.get("error", ""))
            row["reviewed"] = True
            n += 1
    return n


def write_outputs(name: str, data_dir: Path, results: list[dict], checked_at: str, extra: dict | None = None) -> dict:
    out_dir = ROOT / "data_quality"
    out_dir.mkdir(exist_ok=True)
    reviewed = apply_reviews(results)
    extra = {**(extra or {}), "manually_reviewed": reviewed}
    summary = {"checked_at": checked_at, "data_dir": data_dir.name, "total": len(results),
               "by_status": Counter(r["status"] for r in results),
               "by_feed": {f: Counter(r["status"] for r in results if r["feed"] == f)
                           for f in sorted({r["feed"] for r in results})}, **extra}
    (out_dir / f"{name}.json").write_text(json.dumps({"summary": summary, "articles": results}, indent=1,
                                                     ensure_ascii=False), encoding="utf-8")
    (out_dir / f"{name}.md").write_text(report(results, "Permalink check: " + data_dir.name, checked_at),
                                        encoding="utf-8")
    return summary


def recheck(name: str, data_dir: Path, delay: float) -> None:
    path = ROOT / "data_quality" / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data["articles"]
    todo = [r for r in rows if r["status"] in ("blocked", "soft_404", "broken")]
    hosts = Counter(urlsplit(r["url"]).netloc for r in todo)
    print(f"Re-checking {len(todo)} links one at a time ({dict(hosts)}) …", flush=True)
    walled: set[str] = set()
    for i, row in enumerate(todo, 1):
        host = urlsplit(row["url"]).netloc
        if host in walled:  # every page on this host is a challenge; don't hammer it
            continue
        fresh = classify(curl_check(row))
        row.update(fresh)
        if row["status"] == "blocked" and "Pardon Our Interruption" in row.get("page_title", ""):
            # Imperva walls the whole site, including made-up URLs: no amount of retrying will tell.
            walled.add(host)
            for other in todo:
                if urlsplit(other["url"]).netloc == host and other is not row:
                    other["status"], other["error"] = "blocked", row["error"]
        if i % 25 == 0:
            print(f"  {i}/{len(todo)} {Counter(r['status'] for r in todo)}", flush=True)
        time.sleep(delay)
    summary = write_outputs(name, data_dir, rows, data["summary"]["checked_at"],
                            {"rechecked_at": time.strftime("%Y-%m-%d %H:%M", time.gmtime())})
    print(json.dumps(summary, indent=1))


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=ROOT / "liquid-briefing-data")
    ap.add_argument("--name", help="Output name (default: link_check_<data dir name>)")
    ap.add_argument("--concurrency", type=int, default=12)
    ap.add_argument("--per-host", type=int, default=4, help="Max parallel requests to one host")
    ap.add_argument("--recheck", action="store_true",
                    help="Reload the last report and re-try blocked/soft-404 links sequentially (rate limits)")
    ap.add_argument("--delay", type=float, default=2.5, help="Seconds between requests with --recheck")
    ap.add_argument("--apply-reviews", action="store_true",
                    help="Re-apply data_quality/manual_review.json to the last report without new requests")
    args = ap.parse_args()
    name = args.name or f"link_check_{args.data_dir.name.replace('-', '_')}"
    if args.apply_reviews:
        data = json.loads((ROOT / "data_quality" / f"{name}.json").read_text(encoding="utf-8"))
        keep = {k: v for k, v in data["summary"].items() if k not in ("checked_at", "data_dir", "total", "by_status", "by_feed")}
        print(json.dumps(write_outputs(name, args.data_dir, data["articles"], data["summary"]["checked_at"], keep), indent=1))
        return
    if args.recheck:
        recheck(name, args.data_dir, args.delay)
        return
    rows = load(args.data_dir)
    print(f"Checking {len(rows)} links from {args.data_dir} …", flush=True)
    sem_by_host: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(args.per_host))
    global_sem = asyncio.Semaphore(args.concurrency)
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25.0, limits=limits,
                                 http2=False) as client:
        async def one(row):
            async with global_sem:
                return await check(client, row, sem_by_host[urlsplit(row["url"]).netloc])
        results = []
        done = 0
        for fut in asyncio.as_completed([one(r) for r in rows]):
            results.append(await fut)
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(rows)}", flush=True)
    print("Second pass with curl (title check) …", flush=True)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = [classify(r) for r in pool.map(curl_check, results)]
    results.sort(key=lambda r: (r["feed"], r["date"]))
    checked_at = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
    print(json.dumps(write_outputs(name, args.data_dir, results, checked_at), indent=1))


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
