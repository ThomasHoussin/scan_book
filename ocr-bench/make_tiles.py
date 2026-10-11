# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = ["Pillow>=10.4,<13"]
# ///
"""Tuiles 300 dpi pour rédiger les références : uv run ocr-bench/make_tiles.py.

Chaque page du jeu A est découpée en 2 colonnes x 3 rangées avec recouvrement,
pour qu'un lecteur voie le texte à pleine résolution (une page entière serait réduite).
Écrit inputs/tiles/<id>/r<rang>c<col>.png.
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

BENCH = Path(__file__).resolve().parent
COLS, ROWS, OVERLAP = 2, 3, 0.08


def tiles(image: Image.Image) -> list[tuple[str, tuple[int, int, int, int]]]:
    w, h = image.size
    tw, th = w / COLS, h / ROWS
    pad_x, pad_y = round(tw * OVERLAP), round(th * OVERLAP)
    out = []
    for r in range(ROWS):
        for c in range(COLS):
            box = (max(0, round(c * tw) - pad_x), max(0, round(r * th) - pad_y),
                   min(w, round((c + 1) * tw) + pad_x), min(h, round((r + 1) * th) + pad_y))
            out.append((f"r{r + 1}c{c + 1}", box))
    return out


def main() -> None:
    items = json.loads((BENCH / "manifest.json").read_text(encoding="utf-8"))["items"]
    for item in (i for i in items if i["set"] == "scans"):
        out_dir = BENCH / "inputs" / "tiles" / item["id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        with Image.open(BENCH / item["image"]) as img:
            for name, box in tiles(img):
                img.crop(box).save(out_dir / f"{name}.png", optimize=True)
        print(item["id"])


if __name__ == "__main__":
    main()
