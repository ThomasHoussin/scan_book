# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = ["Pillow>=10.4,<13", "PyMuPDF>=1.27.2,<1.28"]
# ///
"""Tirage de l'échantillon OCR : uv run ocr-bench/prepare.py [--seed N].

Jeu A : pages intérieures tirées au hasard dans scans/<livre>/session.json.
Jeu B : pages tirées au hasard dans olmOCR-bench (Hugging Face).
Produit inputs/ (gitignoré) et manifest.json (versionné).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import shutil
import urllib.request
from pathlib import Path

import pymupdf
from PIL import Image

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
INPUTS = BENCH / "inputs"
BOOKS = {"veilleurs": "Les veilleurs", "lion-vert": "Le Lion Vert", "livre-noir": "Le Livre Noir"}
OLMOCR_URL = "https://huggingface.co/datasets/allenai/olmOCR-bench/resolve/main/bench_data"
OLMOCR_CATEGORIES = ("old_scans", "table_tests", "multi_column", "long_tiny_text")
LLM_LONG_EDGE = 2000
LLM_MAX_BYTES = 5 * 1024 * 1024
RENDER_DPI = 300
RENDER_MAX_EDGE = 3508


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download(url: str, target: Path) -> Path:
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        with urllib.request.urlopen(url, timeout=120) as response, tmp.open("wb") as out:
            shutil.copyfileobj(response, out)
        tmp.replace(target)
    return target


def llm_image(source: Path, target: Path) -> tuple[int, int]:
    """Version réduite commune aux deux LLM : bord long 2000 px, < 5 Mo."""
    with Image.open(source) as img:
        img.load()
        if img.mode not in ("L", "RGB"):
            img = img.convert("RGB")
        scale = LLM_LONG_EDGE / max(img.size)
        if scale < 1:
            img = img.resize((round(img.width * scale), round(img.height * scale)), Image.Resampling.LANCZOS)
        img.save(target, optimize=True)
        if target.stat().st_size > LLM_MAX_BYTES and img.mode == "RGB":
            img.convert("L").save(target, optimize=True)
        if target.stat().st_size > LLM_MAX_BYTES:
            raise SystemExit(f"{target} dépasse 5 Mo")
        return img.size


def entry(item_id: str, dataset: str, image: Path, llm: Path, **extra) -> dict:
    with Image.open(image) as img:
        size, mode = list(img.size), img.mode
    with Image.open(llm) as img:
        llm_size = list(img.size)
    return {
        "id": item_id,
        "set": dataset,
        "image": image.relative_to(BENCH).as_posix(),
        "llm_image": llm.relative_to(BENCH).as_posix(),
        "size": size,
        "mode": mode,
        "llm_size": llm_size,
        "sha256": sha256(image),
        "prompt": "prompt.md" if dataset == "scans" else "prompt_olmocr.md",
        "ext": ".txt" if dataset == "scans" else ".md",
        **extra,
    }


def sample_scans(rng: random.Random, per_book: int) -> list[dict]:
    out_dir = INPUTS / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    items = []
    for slug, book in BOOKS.items():
        book_dir = ROOT / "scans" / book
        pages = read_json(book_dir / "session.json")["pages"][1:-1]  # sans les couvertures
        for page in sorted(rng.sample(pages, per_book), key=lambda p: p["number"]):
            item_id = f"{slug}-{page['number']:04d}"
            image = out_dir / f"{item_id}.png"
            shutil.copyfile(book_dir / page["image"], image)
            llm = out_dir / f"{item_id}.llm.png"
            llm_image(image, llm)
            items.append(entry(item_id, "scans", image, llm, book=book, page=page["number"],
                               source=f"scans/{book}/{page['image']}"))
    return items


def sample_olmocr(rng: random.Random, per_category: int) -> list[dict]:
    bench_dir = INPUTS / "olmocr"
    cache = INPUTS / "olmocr-cache"
    out_dir = INPUTS / "images"
    items = []
    for category in OLMOCR_CATEGORIES:
        source = download(f"{OLMOCR_URL}/{category}.jsonl", cache / f"{category}.jsonl")
        tests = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
        pdfs = sorted({t["pdf"] for t in tests})
        chosen = sorted(rng.sample(pdfs, per_category))
        kept = [t for t in tests if t["pdf"] in chosen]
        (bench_dir / f"{category}.jsonl").parent.mkdir(parents=True, exist_ok=True)
        (bench_dir / f"{category}.jsonl").write_text(
            "".join(json.dumps(t, ensure_ascii=False) + "\n" for t in kept), encoding="utf-8")
        for pdf_rel in chosen:
            # Chemin issu du jsonl téléchargé : on refuse toute sortie de pdfs/.
            if not (bench_dir / "pdfs" / pdf_rel).resolve().is_relative_to((bench_dir / "pdfs").resolve()):
                raise SystemExit(f"chemin PDF refusé : {pdf_rel}")
            pdf = download(f"{OLMOCR_URL}/pdfs/{pdf_rel}", bench_dir / "pdfs" / pdf_rel)
            pages = sorted({t["page"] for t in kept if t["pdf"] == pdf_rel})
            if len(pages) != 1:
                raise SystemExit(f"{pdf_rel} : une seule page attendue, trouvé {pages}")
            item_id = f"olmocr-{category}-{Path(pdf_rel).stem}"
            image = out_dir / f"{item_id}.png"
            with pymupdf.open(pdf) as doc:
                page = doc[pages[0] - 1]
                # Plafond A4 à 300 dpi : certains vieux scans ont des pages physiques géantes.
                dpi = min(RENDER_DPI, RENDER_MAX_EDGE * 72 / max(page.rect.width, page.rect.height))
                page.get_pixmap(dpi=round(dpi)).save(image)
            llm = out_dir / f"{item_id}.llm.png"
            llm_image(image, llm)
            items.append(entry(item_id, "olmocr", image, llm, category=category, pdf=pdf_rel, page=pages[0],
                               tests=len([t for t in kept if t["pdf"] == pdf_rel])))
    return items


def add_pdf_page(pdf: Path, page_number: int, item_id: str, book: str) -> None:
    """Ajoute une page choisie à la main (jeu A) au manifest existant, sans refaire le tirage."""
    manifest_path = BENCH / "manifest.json"
    manifest = read_json(manifest_path)
    if any(i["id"] == item_id for i in manifest["items"]):
        raise SystemExit(f"{item_id} est déjà dans le manifest")
    out_dir = INPUTS / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    image = out_dir / f"{item_id}.png"
    with pymupdf.open(pdf) as doc:
        page = doc[page_number - 1]
        embedded = page.get_images()
        if len(embedded) == 1:  # page scannée : on garde l'image d'origine, sans rééchantillonnage
            Image.open(io.BytesIO(doc.extract_image(embedded[0][0])["image"])).save(image)
        else:
            page.get_pixmap(dpi=RENDER_DPI).save(image)
    llm = out_dir / f"{item_id}.llm.png"
    llm_image(image, llm)
    manifest["items"].append(entry(item_id, "scans", image, llm, book=book, page=page_number,
                                   source=str(pdf), added_by_hand=True))
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{item_id} ajouté ({book}, page PDF {page_number})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20261011)
    parser.add_argument("--per-book", type=int, default=5)
    parser.add_argument("--per-category", type=int, default=5)
    parser.add_argument("--force", action="store_true", help="refaire le tirage et écraser manifest.json")
    parser.add_argument("--add-pdf", nargs=4, metavar=("PDF", "PAGE", "ID", "LIVRE"),
                        help="ajouter une page de PDF choisie à la main au jeu A")
    args = parser.parse_args()
    if args.add_pdf:
        pdf, page, item_id, book = args.add_pdf
        add_pdf_page(Path(pdf), int(page), item_id, book)
        return
    if (BENCH / "manifest.json").exists() and not args.force:
        # Le tirage dépend du nombre de pages des sessions, qui bouge pendant un scan.
        raise SystemExit("manifest.json existe déjà : tirage figé (--force pour le refaire)")
    rng = random.Random(args.seed)
    items = sample_scans(rng, args.per_book) + sample_olmocr(rng, args.per_category)
    manifest = {"seed": args.seed, "llm_long_edge": LLM_LONG_EDGE, "render_dpi": RENDER_DPI,
                "render_max_edge": RENDER_MAX_EDGE, "items": items}
    (BENCH / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Restes d'un tirage précédent : le correcteur olmOCR exige une sortie par PDF présent.
    keep = {BENCH / i[k] for i in items for k in ("image", "llm_image")}
    keep |= {INPUTS / "olmocr" / "pdfs" / i["pdf"] for i in items if i["set"] == "olmocr"}
    for stale in [*(INPUTS / "images").glob("*.png"), *(INPUTS / "olmocr" / "pdfs").rglob("*.pdf")]:
        if stale not in keep:
            stale.unlink()
    for item in items:
        print(f"{item['id']:60} {item['size']} -> {item['llm_size']} {item['mode']}")
    print(f"{len(items)} pages")


if __name__ == "__main__":
    main()
