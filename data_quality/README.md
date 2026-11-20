# Data quality: do the source links work?

A briefing that cites its sources is only as trustworthy as those links. We checked every permalink in
the hackathon dataset on 20 November 2026.

## Results

| Dataset | Links | Verified automatically | Broken | Blocks automated checks |
|---|---:|---:|---:|---:|
| Liquid Briefing (Newstex), 6 feeds | 1,740 | **744** | **0** | 996 (Zacks) |

Every source link works. All ActionForex, IBTimes UK and Invezz (English, German and Dutch) permalinks
open the right article. Zacks answers automated requests with an Imperva bot wall ("Pardon Our
Interruption", served with HTTP 200, even for a made-up URL), so a script can't confirm its links; they
open normally in a browser.

## What we learned along the way

* **A 403 is not a dead link.** Every Invezz link answered Python's HTTP client with 403 (Cloudflare
  fingerprinting), and so did a made-up Invezz URL. curl got through but was rate-limited (429) when run in
  parallel. Run slowly, one request every 2.5 seconds, all 406 Invezz links opened the right article.
* **A 200 is not a working link.** Zacks serves its bot wall with HTTP 200, so we compare every page's
  title with the article's headline instead of trusting the status code.
* **Headlines change after publication.** A title mismatch needs a human look before it's called broken.
  Those verdicts live in `manual_review.json`, so reruns keep them.

## Files

The per-article reports (`link_check_*.md` / `.json`) list the headline and URL of every article, which
would publish an index of the licensed dataset. They are written locally by the checker and are
git-ignored; only the counts above and on the website are published.

| File | What it is |
|---|---|
| `manual_review.json` | Human verdicts applied on top of the script |
| `link_check_*.md` / `.json` | Local only: per-feed tables and one row per article |

Rerun (needs the data synced from the bucket):

```sh
uv run tools/check_links.py                      # concurrent pass (httpx, then curl with title check)
uv run tools/check_links.py --recheck            # slow sequential retry for rate-limited/blocked links
uv run tools/check_links.py --apply-reviews      # re-apply manual_review.json without new requests
```
