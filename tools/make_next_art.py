"""Draw the 'under construction' illustrations for site/next.html with gpt-image-2.

    uv run tools/make_next_art.py            # writes site/assets/next/<name>.png (skips existing)

Line-art style to match the portraits in img/: black ink outlines, white paper, a few pastel
accents, no text. The page blends them onto its paper colour with mix-blend-mode: multiply.
"""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

from openai import OpenAI

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

OUT = Path(__file__).resolve().parent.parent / "site" / "assets" / "next"

STYLE = ("Hand-drawn doodle illustration in clean black ink line art, like a friendly sketchbook drawing: "
         "simple rounded outlines, small cute characters with dot eyes, no shading, no gradients, flat. "
         "Pure white background with lots of empty space around the scene. Only a few soft pastel fills "
         "({accent}). Absolutely no text, no letters, no numbers, no words, no logos anywhere.")

ART = {
    "cost": ("A tiny construction site: two little workers in hard hats use a small crane to lower a big round "
             "coin into a piggy bank wrapped in scaffolding. A worker trims a long paper receipt with giant "
             "scissors. Traffic cones and a striped barrier in front.", "butter yellow and sage green"),
    "business": ("A tiny construction site: little workers in hard hats build a cosy newspaper kiosk that is "
                 "growing upward into a tall building with scaffolding, a crane on top, and stacks of folded "
                 "newspapers being hoisted up. A few small people queue at the kiosk window.", "lavender and sky blue"),
    "issues": ("A tiny road-repair site: little workers in hard hats fix a small bridge made of speech bubbles. "
               "One worker inspects a crack with a big magnifying glass, another carries a plank, a warning "
               "sign with an exclamation mark shape and traffic cones stand nearby.", "blush pink and peach"),
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    client = OpenAI()
    for name, (scene, accent) in ART.items():
        dest = OUT / f"{name}.png"
        if dest.exists() and "--force" not in sys.argv:
            print(f"skip {dest.name}")
            continue
        img = client.images.generate(model="gpt-image-2", prompt=f"{scene} {STYLE.format(accent=accent)}",
                                     size="1536x1024", quality="medium", n=1)
        raw = base64.b64decode(img.data[0].b64_json)
        try:  # shrink for the web: 960px wide, optimised PNG
            from PIL import Image

            im = Image.open(io.BytesIO(raw)).convert("RGB")
            im = im.resize((960, round(960 * im.height / im.width)), Image.LANCZOS)
            im = im.quantize(colors=64, dither=Image.Dither.NONE)
            im.save(dest, optimize=True)
        except ImportError:
            dest.write_bytes(raw)
        print(f"wrote {dest.name} ({dest.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
