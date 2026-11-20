"""Turn a news story into a vertical short video (TikTok / Reels / Shorts).

    uv run reels.py --article ARTICLE_ID --audience student
    uv run reels.py --query "UK storm travel disruption" --audience student
    uv run reels.py --article ID --audience finance --dry-run      # script only, no video spend

Pipeline (all OpenAI, plus a bundled ffmpeg from imageio-ffmpeg):

1. Script   The model writes a ~20 second reel for one audience: a hook, a
            voice-over that paraphrases the reporting (licence: no article text),
            two visual shots, a post caption and hashtags, grounded in the article
            and the News Council's saved reactions.
2. Video    Sora 2 renders each shot as a 12 second 720x1280 clip. Prompts ask for
            illustrative b-roll: no real people, no logos, no on-screen text.
            If the account has no Sora access, gpt-image-2 stills with a slow zoom
            stand in (about $0.10 per reel instead of $2.40).
3. Voice    gpt-4o-mini-tts reads the voice-over.
4. Edit     ffmpeg joins the shots, ducks the ambient audio under the voice, and
            burns in captions, the hook and the source credit.

Review step: --dry-run saves the script to reels/<audience>-<headline>/reel.json. Check the
numbers against the source, edit if needed, then rerun without --dry-run to render it.

Output: reels/<slug>/reel.mp4, poster.jpg, captions.ass and reel.json (script,
source link, cost estimate). Cost: about $2.40 of Sora per reel (24 s at $0.10/s),
under $0.02 for the script and voice.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, Field

from model_config import load_models

try:  # read OPENAI_API_KEY etc. from .env when present
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "reels"

AUDIENCES = {
    "student": "A university student who rides public transport, watches every euro, and cares about exams, "
               "rent, jobs, the weather on the way to campus, and whether the future is livable. Casual, "
               "quick, no jargon, a little wry. Tell them what it means for their week.",
    "finance": "A confident young finance professional who watches markets, rates, the dollar, oil and "
               "earnings before the open. Crisp, numbers-first, a bit swaggering but never sloppy. Tell them "
               "the trade-relevant takeaway and what to watch next.",
    "general": "A busy adult who wants to know what happened and why it matters, in plain words.",
}


class Shot(BaseModel):
    prompt: str = Field(description="Sora prompt for a 12 second vertical b-roll shot")


class ReelPlan(BaseModel):
    title: str
    hook: str = Field(description="On-screen hook for the first 3 seconds, max 7 words")
    voiceover: str = Field(description="Spoken script, 45 to 60 words")
    shots: list[Shot]
    post_caption: str
    hashtags: list[str]


SCRIPT = """You write vertical short-form news videos (TikTok, Reels, Shorts) for one audience.
Input: an ARTICLE (licensed: paraphrase it, never copy sentences; a quote must be under 8 words and
attributed) and optional COUNCIL reactions from fictional personas.

Write:
- hook: the scroll-stopper shown on screen for 3 seconds. Max 7 words. No clickbait, no false urgency.
- voiceover: 45 to 60 words, spoken at a brisk pace (about 20 seconds). Open with the news itself, say
  which outlet reported it, give one concrete number or fact from the article exactly as written, then
  what it means for THIS audience, then one line on what to watch. Plain spoken English, no emoji,
  no hashtags, no URLs. Facts only from the article.
- shots: exactly 2 Sora prompts, one per half of the voiceover. Each describes a 12 second, vertical 9:16,
  photorealistic or cinematic b-roll scene that illustrates the story: setting, subject, camera move,
  lighting, mood and ambient sound. Rules for every prompt: no real or identifiable people or public
  figures (use anonymous people from behind, hands, crowds out of focus, or objects and places), no
  logos or brands, no on-screen text, captions, numbers, charts or signs with words, no dialogue.
- post_caption: one or two sentences for the post, ending with the outlet name as the source.
- hashtags: 3 to 5, lowercase, without the # sign."""


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as exc:
        raise SystemExit("ffmpeg not found. Install it or run: uv add imageio-ffmpeg") from exc


def run_ffmpeg(args: list[str], cwd: Path) -> str:
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-y", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-1500:]}")
    return proc.stderr


def media_info(path: Path) -> tuple[float, bool]:
    """(duration seconds, has audio)."""
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", proc.stderr)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    return dur, "Audio:" in proc.stderr


def find_article(article_id: str | None, query: str | None) -> dict:
    sys.path.insert(0, str(ROOT))
    from voice_agent.retrieval import NewsIndex

    index = NewsIndex.from_dir(None)
    doc = index.get(article_id) if article_id else None
    if doc is None and query:
        hits = index.search(query, k=1)
        doc = hits[0][1] if hits else None
    if doc is None:
        raise SystemExit(f"No article found for {article_id or query!r}")
    return {"id": doc.id, "publication": doc.publication, "headline": doc.headline, "url": doc.url,
            "published_at": doc.published_at.isoformat(), "excerpt": doc.excerpt, "text": doc.text[:6000],
            "topic": doc.topic}


def council_views(article_id: str) -> list[dict]:
    from voice_agent.council_bridge import council_perspectives

    return council_perspectives(ROOT / "council.sqlite3", [article_id]).get(article_id, [])


def write_plan(client: OpenAI, model: str, article: dict, audience: str) -> ReelPlan:
    payload = {"audience": AUDIENCES.get(audience, audience), "article": article,
               "council": [{"name": v["reviewer"], "view": v["observation"]} for v in council_views(article["id"])][:4]}
    result = client.responses.parse(model=model, instructions=SCRIPT, input=json.dumps(payload, ensure_ascii=False),
                                    text_format=ReelPlan, store=False)
    plan = result.output_parsed
    if result.status != "completed" or plan is None or len(plan.shots) < 1:
        raise SystemExit("The model did not return a usable reel script. Rerun.")
    plan.shots = plan.shots[:2]
    return plan


SAFETY_SUFFIX = (" Vertical 9:16 framing. No identifiable people, no faces in close-up, no logos, no text, "
                 "no captions, no signage with words. Ambient sound only, no speech, no music with lyrics.")


def sora_shot(client: OpenAI, prompt: str, dest: Path, model: str, seconds: str) -> None:
    video = client.videos.create(model=model, prompt=prompt + SAFETY_SUFFIX, size="720x1280", seconds=seconds)
    started = time.time()
    while video.status in ("queued", "in_progress"):
        if time.time() - started > 900:
            raise RuntimeError(f"Sora timed out on {video.id}")
        time.sleep(8)
        video = client.videos.retrieve(video.id)
        print(f"    sora {video.id}: {video.status} {getattr(video, 'progress', '') or ''}", flush=True)
    if video.status != "completed":
        err = getattr(video, "error", None)
        raise RuntimeError(f"Sora {video.status}: {getattr(err, 'message', err)}")
    client.videos.download_content(video.id, variant="video").write_to_file(dest)


IMAGE_MODELS = ("gpt-image-2", "gpt-image-1.5", "gpt-image-1")


def still_shot(client: OpenAI, prompt: str, dest: Path, seconds: float, workdir: Path) -> str:
    """Fallback: one generated still with a slow Ken Burns zoom. Returns the image model used."""
    img, used, last = None, "", None
    for model in IMAGE_MODELS:
        try:
            img = client.images.generate(model=model, prompt=prompt + SAFETY_SUFFIX, size="1024x1536", n=1)
            used = model
            break
        except OpenAIError as exc:  # model not on this account: try the next one
            last = exc
    if img is None:
        raise RuntimeError(f"No image model available: {last}")
    png = dest.with_suffix(".png")
    png.write_bytes(base64.b64decode(img.data[0].b64_json))
    frames = int(seconds * 30)
    run_ffmpeg(["-loop", "1", "-i", png.name, "-vf",
                f"scale=1440:2560,zoompan=z='min(zoom+0.0009,1.18)':d={frames}:s=720x1280:fps=30,format=yuv420p",
                "-t", f"{seconds}", "-an", dest.name], cwd=workdir)
    return used


def narrate(client: OpenAI, text: str, dest: Path, audience: str) -> None:
    style = {"finance": "Confident, crisp, fast-paced market-open energy. Slight swagger.",
             "student": "Friendly, upbeat and a little wry, like a smart classmate explaining it quickly."}
    with client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts", voice="coral" if audience == "student" else "ash", input=text,
            instructions=style.get(audience, "Clear, warm and brisk news delivery."), response_format="mp3") as r:
        r.stream_to_file(dest)


def _ass_time(t: float) -> str:
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def captions(plan: ReelPlan, article: dict, voice_s: float, total_s: float) -> str:
    """TikTok-style captions: 3-5 word chunks timed by character share of the narration."""
    words = plan.voiceover.split()
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= 4 and (len(" ".join(cur)) > 18 or w.endswith((".", ",", "?", "!"))):
            chunks.append(" ".join(cur))
            cur = []
    if cur:
        chunks.append(" ".join(cur))
    total_chars = sum(len(c) + 2 for c in chunks)
    start = 0.35
    span = max(voice_s - 0.2, 1.0)
    events = []
    for c in chunks:
        dur = span * (len(c) + 2) / total_chars
        events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(start + dur)},Cap,,0,0,0,,{_ass_escape(c.upper())}")
        start += dur
    date = article["published_at"][:10]
    credit = f"Source: {article['publication']}, {date}"
    events.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(3.2)},Hook,,0,0,0,,{_ass_escape(plan.hook)}")
    events.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(total_s)},Brand,,0,0,0,,NEWS COUNCIL")
    events.append(f"Dialogue: 1,{_ass_time(max(total_s - 4, 0))},{_ass_time(total_s)},Credit,,0,0,0,,{_ass_escape(credit)}")
    head = """[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial,54,&H00FFFFFF,&H00FFFFFF,&H00202020,&H64000000,1,0,0,0,100,100,0,0,1,5,2,2,60,60,330,1
Style: Hook,Georgia,58,&H00202020,&H00202020,&H00F3E6FB,&H00F3E6FB,1,0,0,0,100,100,0,0,3,18,0,8,50,50,190,1
Style: Brand,Georgia,26,&H00FFFFFF,&H00FFFFFF,&H64202020,&H64000000,1,0,0,0,100,100,3,0,1,2,1,8,40,40,70,1
Style: Credit,Arial,30,&H00202020,&H00202020,&H00DDF3E4,&H00DDF3E4,1,0,0,0,100,100,0,0,3,12,0,2,50,50,150,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    return head + "\n".join(events) + "\n"


def edit(workdir: Path, shots: list[Path], voice: Path, ass: Path, total_s: float) -> None:
    inputs, filters, vlabels, alabels = [], [], [], []
    for i, shot in enumerate(shots):
        inputs += ["-i", shot.name]
        filters.append(f"[{i}:v]scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,fps=30,"
                       f"setsar=1,format=yuv420p[v{i}]")
        vlabels.append(f"[v{i}]")
        if media_info(shot)[1]:
            filters.append(f"[{i}:a]aresample=48000,volume=0.22[a{i}]")
        else:
            filters.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{media_info(shot)[0]:.2f}[a{i}]")
        alabels.append(f"[a{i}]")
    n = len(shots)
    inputs += ["-i", voice.name]
    filters.append("".join(v + a for v, a in zip(vlabels, alabels)) + f"concat=n={n}:v=1:a=1[vc][bed]")
    fonts = "C\\:/Windows/Fonts" if sys.platform == "win32" else "/usr/share/fonts"
    filters.append(f"[vc]subtitles={ass.name}:fontsdir='{fonts}'[vout]")
    filters.append(f"[{n}:a]aresample=48000,volume=1.6,apad[vo];[bed][vo]amix=inputs=2:duration=first:normalize=0[aout]")
    run_ffmpeg([*inputs, "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]",
                "-t", f"{total_s:.2f}", "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "reel.mp4"], cwd=workdir)
    run_ffmpeg(["-ss", "1.5", "-i", "reel.mp4", "-frames:v", "1", "-q:v", "3", "poster.jpg"], cwd=workdir)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "reel"


def make_reel(article: dict, audience: str, *, model: str, video_model: str, dry_run: bool = False,
              stills: bool = False, new_script: bool = False) -> Path:
    client = OpenAI(max_retries=2)
    print(f"Article: {article['headline']} ({article['publication']})", flush=True)
    workdir = OUT / f"{audience}-{slug(article['headline'])}"
    workdir.mkdir(parents=True, exist_ok=True)
    saved = workdir / "reel.json"
    if saved.exists() and not new_script:
        # Reuse the reviewed script: --dry-run, read/edit reel.json, then render.
        plan = ReelPlan.model_validate(json.loads(saved.read_text(encoding="utf-8"))["plan"])
        print("  reusing the saved script (pass --new-script to rewrite it)", flush=True)
    else:
        plan = write_plan(client, model, article, audience)
    meta = {"audience": audience, "article": {k: article[k] for k in ("id", "publication", "headline", "url",
                                                                      "published_at", "topic")},
            "plan": plan.model_dump(), "video_model": None, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (workdir / "reel.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  hook: {plan.hook}\n  voiceover: {plan.voiceover}", flush=True)
    if dry_run:
        return workdir

    voice = workdir / "voice.mp3"
    narrate(client, plan.voiceover, voice, audience)
    voice_s = media_info(voice)[0]
    shot_s = 12.0
    shots_needed = 2 if voice_s > 11.0 else 1
    total_s = min(max(voice_s + 1.2, 8.0), shot_s * shots_needed)
    shots: list[Path] = []
    used = video_model
    for i, shot in enumerate(plan.shots[:shots_needed]):
        dest = workdir / f"shot{i + 1}.mp4"
        if not dest.exists():
            print(f"  shot {i + 1}: {shot.prompt[:110]}…", flush=True)
            if not stills:
                try:
                    sora_shot(client, shot.prompt, dest, video_model, "12")
                except (OpenAIError, RuntimeError) as exc:
                    print(f"  Sora unavailable ({exc}); using a gpt-image-1 still instead.", flush=True)
                    stills = True
            if stills:
                used = f"{still_shot(client, shot.prompt, dest, shot_s, workdir)} (still + zoom)"
        shots.append(dest)
    ass = workdir / "captions.ass"
    ass.write_text(captions(plan, article, voice_s, total_s), encoding="utf-8")
    edit(workdir, shots, voice, ass, total_s)
    sora_seconds = 0 if "image" in (used or "") else shot_s * len(shots)
    meta.update(video_model=used, duration_s=round(total_s, 1),
                cost_usd={"video": round(sora_seconds * (0.30 if "pro" in used else 0.10), 2)
                          if sora_seconds else round(0.06 * len(shots), 2),
                          "script_and_voice": 0.02})
    (workdir / "reel.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {workdir / 'reel.mp4'} ({total_s:.1f}s, {used})", flush=True)
    return workdir


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--article", help="Article $id from liquid-briefing-data/ or news-data/")
    src.add_argument("--query", help="Search the feeds and use the best match")
    ap.add_argument("--audience", default="general", help=f"One of {', '.join(AUDIENCES)} or a free-text description")
    ap.add_argument("--model", help="Script model (default: main_agent in models.yaml)")
    ap.add_argument("--video-model", default="sora-2", help="sora-2 or sora-2-pro")
    ap.add_argument("--stills", action="store_true", help="Skip Sora; animate gpt-image-2 stills (cheaper)")
    ap.add_argument("--dry-run", action="store_true", help="Write the script only, to review in reel.json")
    ap.add_argument("--new-script", action="store_true", help="Rewrite the script even if one is saved")
    args = ap.parse_args()
    article = find_article(args.article, args.query)
    model = args.model or load_models().main_agent
    make_reel(article, args.audience, model=model, video_model=args.video_model, dry_run=args.dry_run,
              stills=args.stills, new_script=args.new_script)


if __name__ == "__main__":
    try:
        main()
    except (OSError, OpenAIError) as exc:
        raise SystemExit(str(exc)) from exc
