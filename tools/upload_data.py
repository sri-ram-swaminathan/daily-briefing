"""Upload the private data to a Hugging Face Storage Bucket (or dataset repo), once.

    HF_WRITE_TOKEN=<write token> uv run tools/upload_data.py hf://buckets/<owner>/daily-briefing-data \
        --from ../dass-news

Uploads, keeping this layout (data_store.py downloads the same paths):

    liquid-briefing-data/*.jsonl, README.txt, IDEAS_AND_TIPS.txt
    news-data/*.jsonl, README.txt                       (only with --include-extra)
    state/council.sqlite3, state/preferences.sqlite3     (if present)

The bucket is created private if it doesn't exist. Nothing here is ever committed to git.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data_store import DATA_DIRS, STATE_FILES, parse_source  # noqa: E402


def stage(src: Path, tmp: Path, include_extra: bool = False) -> list[str]:
    files = []
    for d in (DATA_DIRS if include_extra else DATA_DIRS[:1]):
        for f in sorted((src / d).glob("*")):
            if f.suffix in {".jsonl", ".txt"}:
                (tmp / d).mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, tmp / d / f.name)
                files.append(f"{d}/{f.name}")
    for name in STATE_FILES:
        if (src / name).exists():
            (tmp / "state").mkdir(exist_ok=True)
            shutil.copy2(src / name, tmp / "state" / name)
            files.append(f"state/{name}")
    return files


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dest", help="hf://buckets/<owner>/<name> or https://huggingface.co/datasets/<owner>/<name>")
    ap.add_argument("--from", dest="src", type=Path, default=Path(__file__).resolve().parent.parent,
                    help="Folder holding liquid-briefing-data/, news-data/ and the .sqlite3 files")
    ap.add_argument("--dry-run", action="store_true", help="List what would be uploaded")
    ap.add_argument("--include-extra", action="store_true", help="Also upload news-data/ (our extra feeds)")
    args = ap.parse_args()
    # HF_WRITE_TOKEN first: .env usually holds the app's *read* HF_TOKEN, and data_store loads .env.
    token = os.getenv("HF_WRITE_TOKEN") or os.getenv("HF_TOKEN")
    source = "HF_WRITE_TOKEN" if os.getenv("HF_WRITE_TOKEN") else "HF_TOKEN" if token else "saved login (hf auth login)"
    kind, repo_id, prefix = parse_source(args.dest)
    from huggingface_hub import HfApi

    api = HfApi(token=token)
    if not args.dry_run:
        me = api.whoami()
        auth = me.get("auth", {}).get("accessToken", {})
        role = auth.get("role", "?")
        print(f"Token from {source}: '{auth.get('displayName', '?')}', role {role}, Hugging Face user '{me['name']}'")
        if role == "read":
            raise SystemExit("That is a read-only token. Set HF_WRITE_TOKEN to a token with write access.")
        owners = {me["name"]} | {o["name"] for o in me.get("orgs", [])}
        owner = repo_id.split("/")[0]
        if owner not in owners:
            raise SystemExit(f"This token can't write to '{owner}'. Your Hugging Face account is '{me['name']}'"
                             f"{' (orgs: ' + ', '.join(sorted(owners - {me['name']})) + ')' if len(owners) > 1 else ''}."
                             f"\nUse: hf://buckets/{me['name']}/{repo_id.split('/')[1]}")
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        files = stage(args.src, tmp, args.include_extra)
        if not files:
            raise SystemExit(f"Nothing to upload in {args.src}")
        print(f"{len(files)} files from {args.src} -> {args.dest}")
        if args.dry_run:
            print("\n".join(files))
            return
        if kind == "bucket":
            api.create_bucket(repo_id, private=True, exist_ok=True)
            api.sync_bucket(str(tmp), f"hf://buckets/{repo_id}" + (f"/{prefix}" if prefix else ""), verbose=True)
        else:
            api.create_repo(repo_id, repo_type="dataset", private=True, exist_ok=True)
            api.upload_folder(repo_id=repo_id, repo_type="dataset", folder_path=tmp, path_in_repo=prefix or None,
                              commit_message="Upload News Council data")
    print("Done. Set HF_DATA_URL to", args.dest)


if __name__ == "__main__":
    main()
