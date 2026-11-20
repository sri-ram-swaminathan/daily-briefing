"""Deploy the live app to a Hugging Face Space (Docker), with its secrets.

    $env:HF_WRITE_TOKEN = "hf_..."            # a WRITE token, only for this command
    uv run tools/deploy_space.py srilearns/daily-briefing
    uv run tools/deploy_space.py srilearns/daily-briefing --dry-run   # show what would happen

What it does:
1. Creates the Space (public, Docker SDK) if it doesn't exist.
2. Uploads exactly the files tracked by git: code and the website, never the data, the
   SQLite files or .env. The Space syncs the data from the private bucket when it starts.
3. Copies settings from .env into the Space: API keys as *secrets* (hidden), the rest as
   variables. Only names are printed, never values. HF_TOKEN in .env must be a READ token.

Rerun it to redeploy after changes.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SECRETS = ("OPENAI_API_KEY", "ELEVENLABS_API_KEY", "HF_TOKEN")
VARIABLES = ("HF_DATA_URL", "ELEVENLABS_VOICE_ID", "ELEVENLABS_TTS_MODEL", "ELEVENLABS_TTS_FAST_MODEL",
             "STT_MODE", "WEB_SEARCH", "RATE_LIMIT_PER_HOUR", "DAILY_LIMIT")
REQUIRED = ("OPENAI_API_KEY", "HF_TOKEN", "HF_DATA_URL")
SITE_ORIGINS = "https://sriramswaminathan.com,https://sri-ram-swaminathan.github.io"


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    files = [f for f in out.splitlines() if f]
    leaked = [f for f in files if f.endswith((".jsonl", ".sqlite3")) or Path(f).name == ".env"]
    if leaked:
        raise SystemExit(f"Refusing to deploy: data or secrets are tracked by git: {leaked}")
    return files


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("space", help="<owner>/<name>, e.g. srilearns/daily-briefing")
    ap.add_argument("--env", type=Path, default=ROOT / ".env", help="Where to read the settings from")
    ap.add_argument("--private", action="store_true", help="Make the Space private (only you can open it)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from dotenv import dotenv_values
    from huggingface_hub import HfApi

    env = {k: v for k, v in dotenv_values(args.env).items() if v} if args.env.exists() else {}
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        raise SystemExit(f"{args.env} is missing {', '.join(missing)}")
    write_token = os.getenv("HF_WRITE_TOKEN")
    if not write_token:
        raise SystemExit("Set HF_WRITE_TOKEN to a Hugging Face WRITE token for this command.")
    if env["HF_TOKEN"] == write_token:
        raise SystemExit("HF_TOKEN in .env is your WRITE token. Put a READ token there: the Space only needs to read.")

    files = tracked_files()
    owner, name = args.space.split("/")
    url = f"https://{owner}-{name}.hf.space".lower().replace("_", "-")
    variables = {k: env[k] for k in VARIABLES if env.get(k)}
    variables["CORS_ORIGINS"] = SITE_ORIGINS
    print(f"Space: https://huggingface.co/spaces/{args.space}  (app: {url})")
    print(f"Upload: {len(files)} tracked files (code + website, no data)")
    print(f"Secrets: {', '.join(k for k in SECRETS if env.get(k))}")
    print(f"Variables: {', '.join(variables)}")
    if args.dry_run:
        return

    api = HfApi(token=write_token)
    me = api.whoami()["name"]
    if owner != me:
        raise SystemExit(f"Your token belongs to '{me}'. Use {me}/{name}.")
    api.create_repo(args.space, repo_type="space", space_sdk="docker", private=args.private, exist_ok=True)
    for key in SECRETS:
        if env.get(key):
            api.add_space_secret(args.space, key, env[key])
    for key, value in variables.items():
        api.add_space_variable(args.space, key, value)
    api.upload_folder(repo_id=args.space, repo_type="space", folder_path=ROOT, allow_patterns=files,
                      commit_message="Deploy The Daily Briefing")
    print(f"\nDeployed. The Space builds for a few minutes, then: {url}")
    print(f"Build logs: https://huggingface.co/spaces/{args.space}?logs=build")


if __name__ == "__main__":
    sys.exit(main())
