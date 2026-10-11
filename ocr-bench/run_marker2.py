# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = ["marker-pdf==2.0.0", "torch>=2.7"]
# [tool.uv.sources]
# torch = { index = "pytorch-cu128" }
# [[tool.uv.index]]
# name = "pytorch-cu128"
# url = "https://download.pytorch.org/whl/cu128"
# explicit = true
# ///
"""OCR Marker 2 en local (GPU) : uv run ocr-bench/run_marker2.py [--only ID ...].

Mode auto (pas de force_ocr) : les entrées sont des PNG sans couche texte, donc OCR de toute façon.
Écrit runs/marker2/<id>.<ext> et <id>.meta.json pour chaque page du manifest.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import subprocess
import time
from pathlib import Path

os.environ.setdefault("TORCH_DEVICE", "cuda")

BENCH = Path(__file__).resolve().parent
ENGINE = "marker2"
CONFIG = {"output_format": "markdown", "disable_image_extraction": True}


def gpu_memory_used_mb() -> int | None:
    """VRAM totale occupée (le serveur vLLM tourne hors du process, torch ne la voit pas)."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, check=True, timeout=10).stdout
        return int(out.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def is_done(out_dir: Path, item: dict) -> bool:
    """Seules les pages réussies sont sautées ; les échecs et pages vides sont refaits."""
    meta = out_dir / f"{item['id']}.meta.json"
    return meta.exists() and json.loads(meta.read_text(encoding="utf-8")).get("status") == "ok"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", help="identifiants de pages à traiter")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--force-ocr", action="store_true",
                        help="variante force_ocr (runs/marker2-force) : en mode auto, Marker 2 laisse parfois des blocs vides")
    args = parser.parse_args()
    engine = "marker2-force" if args.force_ocr else ENGINE
    config_dict = CONFIG | {"force_ocr": True} if args.force_ocr else CONFIG
    items = json.loads((BENCH / "manifest.json").read_text(encoding="utf-8"))["items"]
    if args.only:
        items = [i for i in items if i["id"] in set(args.only)]
    out_dir = BENCH / "runs" / engine
    out_dir.mkdir(parents=True, exist_ok=True)
    todo = [i for i in items if args.overwrite or not is_done(out_dir, i)]
    if not todo:
        print("rien à faire")
        return

    import torch
    from marker.config.parser import ConfigParser
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    from marker.output import text_from_rendered

    start = time.perf_counter()
    config = ConfigParser(config_dict)
    converter = PdfConverter(config=config.generate_config_dict(), artifact_dict=create_model_dict(),
                             processor_list=config.get_processors(), renderer=config.get_renderer())
    load_seconds = time.perf_counter() - start
    # Surya 2 démarre un serveur vLLM (conteneur Docker) au premier appel : passe de chauffe hors chrono.
    start = time.perf_counter()
    converter(str(BENCH / todo[0]["image"]))
    warmup_seconds = time.perf_counter() - start
    (out_dir / "config.json").write_text(json.dumps({
        "config": config_dict, "marker": importlib.metadata.version("marker-pdf"),
        "surya": importlib.metadata.version("surya-ocr"), "torch": torch.__version__,
        "device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu",
        "model_load_seconds": round(load_seconds, 2), "warmup_seconds": round(warmup_seconds, 2)},
        indent=2), encoding="utf-8")

    for item in todo:
        meta_path = out_dir / f"{item['id']}.meta.json"
        start = time.perf_counter()
        try:
            text, _, _ = text_from_rendered(converter(str(BENCH / item["image"])))
        except Exception as exc:  # une page en échec ne bloque pas les autres
            (out_dir / f"{item['id']}{item['ext']}").unlink(missing_ok=True)
            meta_path.write_text(json.dumps({"engine": engine, "status": "error",
                                             "error": f"{type(exc).__name__}: {exc}"}, indent=2), encoding="utf-8")
            print(f"{item['id']}: ERREUR {exc}", flush=True)
            continue
        seconds = time.perf_counter() - start
        text = text.strip()
        (out_dir / f"{item['id']}{item['ext']}").write_text(text + "\n", encoding="utf-8")
        meta = {
            "engine": engine,
            "seconds": round(seconds, 2),
            "gpu_memory_used_mb": gpu_memory_used_mb(),
            "chars": len(text),
            "status": "ok" if text else "empty",
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"{item['id']}: {meta['status']} {meta['seconds']}s {meta['chars']} car.", flush=True)


if __name__ == "__main__":
    main()
