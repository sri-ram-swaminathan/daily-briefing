# Data quality: do the source links work?

A briefing that cites its sources is only as trustworthy as those links. We checked every permalink in
the hackathon dataset and in the feeds we added, on 20 November 2026.

## Results

| Dataset | Links | Working | Broken | Can't be checked by script |
|---|---:|---:|---:|---:|
| Liquid Briefing (Newstex), 6 feeds | 1,740 | **744** | **0** | 996 (all Zacks) |
| Our topic feeds, 23 feeds | 416 | **415** | **1** | 0 |

**Original dataset.** No link is confirmed broken. All ActionForex (36), IBTimes UK (298) and Invezz
(406, in English, German and Dutch) permalinks open the right article. All 1,000 Zacks permalinks hit
an Imperva bot wall ("Pardon Our Interruption", served with HTTP 200), and so does a made-up Zacks URL.
A script therefore can't tell a live Zacks link from a dead one; 4 got through before the wall closed,
and all 4 were the right article. Those links need a spot-check in a browser.

**Our feeds.** One real broken link: EIA's RSS feed published "Oil flows through Middle East chokepoints
increased in the third quarter" with an empty article id (`detail.php?id=`), so the link lands on a
generic page. Three more were flagged and turned out fine: the BBC (twice) and Electrek rewrote their
headlines after publishing.

## What we learned along the way

* **A 403 is not a dead link.** Every Invezz link answered Python's HTTP client with 403 (Cloudflare
  fingerprinting), and so did a made-up Invezz URL. curl got through but was rate-limited (429) when run in
  parallel. Run slowly, one request every 2.5 seconds, all 406 Invezz links opened the right article.
* **A 200 is not a working link.** Zacks serves its bot wall with HTTP 200, so we compare every page's
  title with the article's headline instead of trusting the status code.
* **Headlines change after publication.** A title mismatch needs a human look before it's called broken.
  Those verdicts live in `manual_review.json`, so reruns keep them.

## Files

| File | What it is |
|---|---|
| `link_check_liquid_briefing_data.md` / `.json` | Per-feed table and every link that didn't check out; JSON has one row per article |
| `link_check_news_data.md` / `.json` | The same for `news-data/` |
| `manual_review.json` | Human verdicts applied on top of the script |

Rerun:

```sh
uv run tools/check_links.py                      # concurrent pass (httpx, then curl with title check)
uv run tools/check_links.py --recheck            # slow sequential retry for rate-limited/blocked links
uv run tools/check_links.py --data-dir news-data
uv run tools/check_links.py --apply-reviews      # re-apply manual_review.json without new requests
```

The website's **Data desk** section shows these numbers and a searchable list of every link that didn't
check out. Only headlines, dates and URLs are stored here, never article text.
