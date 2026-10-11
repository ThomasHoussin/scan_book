# Scan Book

**English** | [Français](README.fr.md)

Scan a book page by page on Windows (WIA scanner), with automatic deskew and crop, a correction preview,
resumable sessions and PDF export.

## Requirements

- Windows and the scanner's WIA driver (tested: Epson Perfection V330)
- [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.12 with Tcl/Tk

Dependencies are installed automatically on the first `uv run`.

## Usage

```powershell
uv run tools/scan_book.py devices                # list scanners
uv run tools/scan_book.py scan les-veilleurs     # scan (or resume) a book
uv run tools/scan_book.py correct les-veilleurs --page 12
uv run tools/scan_book.py export les-veilleurs   # (re)build the PDF
```

Place one page on the glass, then:

| Key | Action |
|---|---|
| Enter | Scan the next page |
| C | Correct the last page (angle, rotation, crop) |
| R | Rescan the last page |
| M | Toggle color / grayscale for the next scans |
| F | Build the PDF and quit |
| Q | Quit without PDF (the session is kept) |

Running the same command again resumes the book. Useful options:

| Option | Effect |
|---|---|
| `--dpi 600` | Resolution (default 300) |
| `--color gray` | Initial color mode |
| `--area x,y,w,h` | Area in mm from the glass corner (`full` for the whole glass) |
| `--scanner "EPSON"` | Scanner selection |
| `--root "E:\Scans"` | Books folder (default `scans/`) |

## Automatic processing

Each page goes through these steps, in order:
1. turned right side up if it is upside down (local text detection with RapidOCR);
2. deskewed;
3. cropped to the paper edges.

Doubtful cases are flagged **À VÉRIFIER** (to check), and the original stays in `raw/`.

The script does no OCR and does not split double pages. It does not correct binding curvature either.

## Three crop variants

The three scripts have the same commands and keys. They share sessions, folders, the PDF and the lock. You can
therefore switch from one to another in the middle of a book:
1. wait for the current acquisition to finish;
2. quit with **Q**;
3. relaunch the same book with the other script.

They only differ in how they find the page frame. Orientation and deskew are the same in all three.

| | `scan_book.py` (original) | `scan_book_deeplab.py` | `scan_book_qwen.py` |
|---|---|---|---|
| Crop | Image heuristics (paper contour, edge shadow, scanner background) | DeepLabV3-ResNet50 segmentation ([LearnOpenCV](https://github.com/spmallick/learnopencv/tree/master/Document-Scanner-Custom-Semantic-Segmentation-using-PyTorch-DeepLabV3) weights) | Qwen3.5 9B run locally through [Ollama](https://ollama.com), which returns a rectangle |
| Margin kept | 3 mm | 0.5 mm | 0.5 mm |
| Safeguard | Uncertain edges are kept | Crop rejected if it cuts detected text | Uncertain answer or area < 20% rejected |
| Dependencies | Nothing heavy | PyTorch CUDA (~3 GB), 161 MB weights downloaded to `.cache/` | Ollama running, `qwen3.5:9b` model (~6.6 GB) |
| Speed | Instant | Fast on GPU | 2.5 to 3 s per page once the model is loaded |
| Workflow | Each page is processed and shown before the next one | Same | Bulk scan, background processing, then page-by-page review before export |

All processing stays local: no image is sent to a remote service.

Trial results, on 4 pages only:
- **DeepLab** sometimes mistakes the page for its printed blocks. Its crops are often rejected by the safeguard,
  which leaves the full page to crop by hand. It is not a general improvement.
- **Qwen** crops inner pages correctly, but its coordinates remain approximate and the frame is sometimes tight
  near titles. Check the preview.

In the Qwen workflow:
- **Enter** validates a page under review and **C** corrects it.
- **F** only exports once every page is validated.
- A page whose image was deleted from `pages/` is skipped at export.
- **A Qwen session in progress must be resumed with the Qwen script.** The other scripts do not know about its
  pending pages.

## Files

```text
scans/<book>/
    session.json      page order, settings, transformations
    raw/0001.png      raw acquisitions
    pages/0001.png    corrected pages
    <book>.pdf
```

A lock prevents two commands from modifying the same book at once. After an interruption, `.pending` is
recovered on the next launch: do not delete it.

## OCR benchmark

The PDFs have no text layer. `ocr-bench/` compares three engines for adding one:
- **Marker 2**, local on GPU;
- **Claude Haiku 5.5**;
- **GPT-6 Luna**, through AWS Bedrock.

Two page sets are used:
- **16 pages from our French scans**, scored against a reference;
- **20 pages from [olmOCR-bench](https://huggingface.co/datasets/allenai/olmOCR-bench)**, scored on their
  human-verified snippets.

The score is the character error rate (CER), lower is better:

| | Marker 2 (force_ocr) | Claude Haiku 5.5 | GPT-6 Luna |
|---|---|---|---|
| Our scans (excluding the hard page) | 0.72% | **0.35%** | 0.37% |
| Page in a stylized handwriting font | 1.1% (empty in auto mode) | **0.75%** | 1.6% |
| olmOCR snippets | 1.0% | **0.46%** | 0.80% |
| Median time per page | 18 s | ≈ 38 s (via agent) | 17 s |

Key takeaways:
- **Haiku is the pick**: the most accurate, it keeps page and chapter titles, and needs no infrastructure.
- **Marker 2 in auto mode can return an empty page without any error.** It also drops titles, even with
  `force_ocr`. And it runs through a vLLM server in Docker.
- **Luna fixes the book's typos** and does not accept `temperature=0`.

Benchmark documents (in French):
- **[ocr-bench/SYNTHESE.md](ocr-bench/SYNTHESE.md)**: reusable parameters (image size, prompt, checks);
- **[ocr-bench/RESULTATS.md](ocr-bench/RESULTATS.md)**: full analysis;
- **[ocr-bench/README.md](ocr-bench/README.md)**: protocol;
- **`ocr-bench/results/report.html`**: visual page-by-page comparison.

## Tests

```powershell
uv run tests/test_scan_book.py          # no scanner or GPU needed
uv run tests/test_scan_book_deeplab.py
uv run tests/test_scan_book_qwen.py
uv run tests/test_ocr_bench.py
```

Set `SCAN_BOOK_SKIP_GUI=1` to skip the Tkinter tests on a machine without a desktop.

## Troubleshooting

- **No scanner found**:
  - turn the scanner on and check the USB cable;
  - close other scanning software;
  - check the service with `Get-Service stisvc`.
- **`Can't find a usable init.tcl`**: run from a regular Windows terminal, or add `--python 3.12` to `uv run`.
- **Garbled accents in PowerShell 5**: read files with `Get-Content -Encoding UTF8`.
