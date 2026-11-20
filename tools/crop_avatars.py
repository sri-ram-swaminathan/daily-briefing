"""Cut the line-art people out of img/1-4.png (4 x 2 grids) into square avatars.

    uv run tools/crop_avatars.py            # writes site/assets/people/<sheet>-<row>-<col>.png

Each figure is found by projecting dark pixels onto the axes and splitting at the
white gaps, then padded to a square on a transparent background, so the site can
put them on pastel discs.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "site" / "assets" / "people"


def runs(mask: list[bool], min_len: int, min_gap: int) -> list[tuple[int, int]]:
    out, start, gap = [], None, 0
    for i, on in enumerate(mask + [False] * (min_gap + 1)):
        if on:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap > min_gap:
                end = i - gap + 1
                if end - start >= min_len:
                    out.append((start, end))
                start, gap = None, 0
    return out


def figures(img: Image.Image) -> list[list[tuple[int, int, int, int]]]:
    g = ImageOps.grayscale(img)
    w, h = g.size
    px = g.load()
    dark = [[px[x, y] < 128 for x in range(w)] for y in range(h)]
    row_mask = [sum(r) > 2 for r in dark]
    grid = []
    for y0, y1 in runs(row_mask, min_len=h // 6, min_gap=12):
        col_mask = [sum(dark[y][x] for y in range(y0, y1)) > 1 for x in range(w)]
        boxes = []
        for x0, x1 in runs(col_mask, min_len=w // 16, min_gap=14):
            ys = [y for y in range(y0, y1) if any(dark[y][x0:x1])]
            boxes.append((x0, ys[0], x1, ys[-1] + 1))
        grid.append(boxes)
    return grid


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for sheet in sorted((ROOT / "img").glob("*.png")):
        img = Image.open(sheet).convert("RGBA")
        for r, row in enumerate(figures(img), 1):
            for c, (x0, y0, x1, y1) in enumerate(row, 1):
                crop = img.crop((x0, y0, x1, y1))
                # white -> transparent, keep ink
                data = [(0, 0, 0, 0) if (p[0] > 235 and p[1] > 235 and p[2] > 235) else (32, 32, 40, 255 - min(p[0], p[1], p[2]) // 2)
                        for p in crop.getdata()]
                crop.putdata(data)
                side = int(max(crop.size) * 1.12)
                canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
                canvas.paste(crop, ((side - crop.width) // 2, side - crop.height), crop)
                canvas = canvas.resize((320, 320), Image.LANCZOS)
                name = f"{sheet.stem}-{r}-{c}.png"
                canvas.save(OUT / name, optimize=True)
                print(name, (x0, y0, x1, y1))


if __name__ == "__main__":
    main()
