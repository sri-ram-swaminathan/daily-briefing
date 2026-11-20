"""Fetch the private data from Hugging Face at startup. The repository never contains it.

The code is public; the articles are licensed (Newstex) or copyrighted (the extra feeds), so they
live in a private Hugging Face **Storage Bucket** (or a private dataset repo) and are synced into
the working directory the first time something needs them.

Layout in the bucket (same as the local folders):

    liquid-briefing-data/*.jsonl      the hackathon dataset (Newstex), plus its README.txt
    news-data/*.jsonl                 optional: extra topic feeds, read only with INCLUDE_EXTRA_FEEDS=1
    state/council.sqlite3             saved council reviews (optional: skips re-running council.py)
    state/preferences.sqlite3         saved reader profiles and picks (optional)

Configuration (environment or .env):

    HF_DATA_URL   where the data lives, any of
                    hf://buckets/<owner>/<bucket>[/prefix]
                    https://huggingface.co/buckets/<owner>/<bucket>
                    hf://datasets/<owner>/<repo>   or   https://huggingface.co/datasets/<owner>/<repo>
    HF_TOKEN      a read token for it (a Space secret on Hugging Face Spaces)
    NEWS_DATA_DIRS  skip all this and read local folders instead (os.pathsep-separated)

    uv run data_store.py          # sync now and print what is available
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

try:  # read HF_DATA_URL / HF_TOKEN from .env when present
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DATA_DIRS = ("liquid-briefing-data", "news-data")
STATE_FILES = ("council.sqlite3", "preferences.sqlite3")


class DataUnavailable(RuntimeError):
    pass


def parse_source(url: str) -> tuple[str, str, str]:
    """-> (kind, repo_id, prefix) with kind 'bucket' or 'dataset'."""
    url = url.strip().rstrip("/")
    m = re.match(r"^(?:hf://|https?://(?:www\.)?huggingface\.co/)(buckets|datasets)/([^/]+/[^/?#]+)(?:/(?:tree/[^/]+/)?(.*))?$", url)
    if not m:
        raise ValueError(f"HF_DATA_URL must look like hf://buckets/<owner>/<name> or "
                         f"https://huggingface.co/datasets/<owner>/<name>, got {url!r}")
    kind = "bucket" if m.group(1) == "buckets" else "dataset"
    return kind, m.group(2), (m.group(3) or "").strip("/")


def _has_data(root: Path) -> bool:
    return any(any((root / d).glob("*.jsonl")) for d in DATA_DIRS)


def sync(dest: Path = ROOT, *, url: str | None = None, token: str | None = None, force: bool = False) -> Path:
    """Download the data into dest (default: the repository folder) unless it is already there."""
    url = url or os.getenv("HF_DATA_URL", "")
    if _has_data(dest) and not force:
        return dest
    if not url:
        raise DataUnavailable(
            "No news data found. Set HF_DATA_URL (and HF_TOKEN) to the private Hugging Face bucket or dataset, "
            "or NEWS_DATA_DIRS to local folders. See README, 'Data'.")
    token = token or os.getenv("HF_TOKEN") or None
    kind, repo_id, prefix = parse_source(url)
    log.info("Syncing news data from %s %s/%s", kind, repo_id, prefix)
    from huggingface_hub import HfApi, snapshot_download  # lazy: only needed on first run

    patterns = [f"{d}/*" for d in DATA_DIRS] + [f"state/{f}" for f in STATE_FILES]
    if kind == "bucket":
        source = f"hf://buckets/{repo_id}" + (f"/{prefix}" if prefix else "")
        HfApi().sync_bucket(source, str(dest), include=patterns, quiet=True, token=token)
    else:
        snapshot_download(repo_id, repo_type="dataset", local_dir=dest, token=token,
                          allow_patterns=[f"{prefix}/{p}" if prefix else p for p in patterns])
        if prefix:  # move <prefix>/... up to the repository root
            for item in (dest / prefix).iterdir():
                target = dest / item.name
                if not target.exists():
                    shutil.move(str(item), target)
    state = dest / "state"
    for name in STATE_FILES:  # place saved databases where council.py / briefing.py expect them
        if (state / name).exists() and not (dest / name).exists():
            shutil.copy2(state / name, dest / name)
    if not _has_data(dest):
        raise DataUnavailable(f"Synced {url} but found no {' or '.join(DATA_DIRS)}/*.jsonl in it.")
    return dest


def data_dirs() -> list[Path]:
    """The feed folders to read: NEWS_DATA_DIRS if set, else the synced copies in the repository.

    Only liquid-briefing-data/ unless INCLUDE_EXTRA_FEEDS=1 (then news-data/ too, if the bucket has it).
    """
    raw = os.getenv("NEWS_DATA_DIRS")
    if raw:
        return [Path(p) for p in raw.split(os.pathsep) if p.strip()]
    sync()
    extra = os.getenv("INCLUDE_EXTRA_FEEDS", "").strip().lower() in {"1", "true", "yes", "on"}
    wanted = DATA_DIRS if extra else DATA_DIRS[:1]
    return [ROOT / d for d in wanted if (ROOT / d).is_dir()]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dirs = data_dirs()
    for d in dirs:
        files = sorted(d.glob("*.jsonl"))
        print(f"{d}: {len(files)} feeds, {sum(1 for f in files for _ in f.open(encoding='utf-8'))} articles")
    for name in STATE_FILES:
        print(f"{name}: {'present' if (ROOT / name).exists() else 'missing (will be created)'}")
