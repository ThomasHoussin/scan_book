# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = ["rapidfuzz>=3.8,<4", "olmocr[bench]==0.4.27", "numpy>=2,<3"]
# ///
"""Scores du banc OCR : uv run ocr-bench/score.py.

Jeu A (scans) : CER/WER contre reference/<id>.txt, après normalisation.
Jeu B (olmOCR-bench) : taux de tests officiels réussis (olmocr.bench.tests).
Écrit results/metrics.json et results/report.html.
"""
from __future__ import annotations

import html
import json
import re
import statistics
import unicodedata
from pathlib import Path

from rapidfuzz.distance import Levenshtein

BENCH = Path(__file__).resolve().parent
ENGINES = ("marker2", "haiku", "luna")
OPTIONAL_ENGINES = ("marker2-force",)  # variantes notées seulement si runs/<moteur>/ existe
OMISSION_RUN = 8  # mots consécutifs manquants = bloc omis

QUOTES = str.maketrans({
    "’": "'", "‘": "'", "ʼ": "'", "`": "'", "´": "'",
    "«": '"', "»": '"', "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "‑": "-", "‐": "-", "−": "-",
    " ": " ", " ": " ", " ": " ",
    "…": "...", "œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE", "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl",
})


def strip_markup(text: str) -> str:
    """Retire la mise en forme Markdown/HTML que Marker ajoute aux scans."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)            # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)          # liens
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</?(p|div|tr|td|th|li|ul|ol|table|thead|tbody|h\d)\b[^>]*>", " ", text, flags=re.I)  # blocs
    text = re.sub(r"<[^>]+>", "", text)                            # balises en ligne (sup, i, b…) : collées
    text = html.unescape(text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)     # titres
    text = re.sub(r"^\s*\|?(\s*:?-{3,}:?\s*\|)+\s*:?-*:?\s*$", " ", text, flags=re.M)  # séparateurs de tableau
    text = text.replace("|", " ")
    text = re.sub(r"(\*\*|\*)(?=\S)(.+?)(?<=\S)\1", r"\2", text)  # gras / italique
    text = re.sub(r"(?<!\w)(__|_)(?=\S)(.+?)(?<=\S)\1(?!\w)", r"\2", text)  # _x_ hors mots (a_b_c intact)
    return text


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", strip_markup(text)).translate(QUOTES)
    # Traits d'union entre lettres retirés partout : « Saint-\nYliades », « Saint- Yliades »
    # et « Saint-Yliades » deviennent identiques, que le moteur garde la césure ou recompose.
    text = re.sub(r"(\w)-\s*(?=\w)", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r" ([,.;:!?)\]])", r"\1", text)                  # espaces typographiques français
    text = re.sub(r"([(\[]) ", r"\1", text)
    text = re.sub(r'\s*"\s*', '"', text)  # « texte » = «texte» = "texte" (espaces aux guillemets ignorés)
    return text.strip()


def strip_furniture(text: str, furniture: list[str]) -> str:
    """Retire les lignes d'en-tête courant / numéro de page (exclues du score)."""
    if not furniture:
        return text
    banned = {normalize(f).casefold() for f in furniture if f.strip()}
    return "\n".join(line for line in text.splitlines() if normalize(line).casefold() not in banned)


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))


def page_metrics(reference: str, hypothesis: str) -> dict:
    ref, hyp = normalize(reference), normalize(hypothesis)
    ref_words, hyp_words = ref.split(), hyp.split()
    char_edits = Levenshtein.distance(ref, hyp)
    plain_edits = Levenshtein.distance(strip_accents(ref), strip_accents(hyp))
    inserted, deleted, omitted_blocks = 0, 0, 0
    for op in Levenshtein.opcodes(ref_words, hyp_words):
        if op.tag == "insert":
            inserted += op.dest_end - op.dest_start
        elif op.tag == "delete":
            deleted += op.src_end - op.src_start
            omitted_blocks += (op.src_end - op.src_start) >= OMISSION_RUN
        elif op.tag == "replace":
            extra = (op.dest_end - op.dest_start) - (op.src_end - op.src_start)
            inserted += max(extra, 0)
            deleted += max(-extra, 0)
            omitted_blocks += -extra >= OMISSION_RUN
    return {
        "ref_chars": len(ref),
        "ref_words": len(ref_words),
        "hyp_words": len(hyp_words),
        "cer": char_edits / max(len(ref), 1),
        "wer": Levenshtein.distance(ref_words, hyp_words) / max(len(ref_words), 1),
        "accent_edits": char_edits - plain_edits,
        "inserted_words": inserted,
        "deleted_words": deleted,
        "omitted_blocks": omitted_blocks,
        "illisible": hypothesis.count("[illisible]"),
    }


def read_meta(engine: str, item_id: str) -> dict:
    path = BENCH / "runs" / engine / f"{item_id}.meta.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"status": "missing"}


def read_output(engine: str, item: dict) -> str | None:
    path = BENCH / "runs" / engine / f"{item['id']}{item['ext']}"
    return path.read_text(encoding="utf-8") if path.exists() else None


def read_furniture(item_id: str) -> list[str]:
    path = BENCH / "reference" / f"{item_id}.furniture.txt"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def score_scans(items: list[dict]) -> dict:
    pages = {}
    for item in items:
        ref_path = BENCH / "reference" / f"{item['id']}.txt"
        if not ref_path.exists():
            continue
        furniture = read_furniture(item["id"])
        reference = strip_furniture(ref_path.read_text(encoding="utf-8"), furniture)
        pages[item["id"]] = {}
        for engine in ENGINES:
            output = read_output(engine, item)
            metrics = page_metrics(reference, strip_furniture(output or "", furniture))
            metrics["missing"] = output is None
            metrics["meta"] = read_meta(engine, item["id"])
            pages[item["id"]][engine] = metrics
    return pages


def score_olmocr(items: list[dict]) -> dict:
    from olmocr.bench.tests import load_tests

    tests_by_pdf: dict[str, list] = {}
    for jsonl in (BENCH / "inputs" / "olmocr").glob("*.jsonl"):
        for test in load_tests(str(jsonl)):
            tests_by_pdf.setdefault(test.pdf, []).append(test)
    pages = {}
    for item in items:
        pages[item["id"]] = {}
        for engine in ENGINES:
            output = read_output(engine, item)
            results = []
            for test in tests_by_pdf.get(item["pdf"], []):
                if not (output or "").strip():
                    # Sinon une sortie vide réussirait les tests « absent ».
                    passed, explanation = False, "sortie absente ou vide"
                else:
                    try:
                        passed, explanation = test.run(output)
                    except Exception as exc:  # sortie mal formée : échec du test, pas du banc
                        passed, explanation = False, f"erreur du test : {exc}"
                results.append({"id": test.id, "type": str(getattr(test.type, "value", test.type)),
                                "passed": bool(passed), "explanation": explanation})
            pages[item["id"]][engine] = {
                "missing": output is None,
                "tests": len(results),
                "passed": sum(r["passed"] for r in results),
                "results": results,
                "meta": read_meta(engine, item["id"]),
            }
    return pages


def summarize(manifest: dict, scans: dict, olmocr: dict) -> dict:
    by_id = {i["id"]: i for i in manifest["items"]}
    summary = {}
    for engine in ENGINES:
        a = [p[engine] for p in scans.values()]
        groups: dict[str, list] = {}
        for item_id, page in scans.items():
            groups.setdefault(by_id[item_id]["book"], []).append(page[engine])
        b_cat: dict[str, list[tuple[int, int]]] = {}
        for item_id, page in olmocr.items():
            r = page[engine]
            if r["tests"]:
                b_cat.setdefault(by_id[item_id]["category"], []).append((r["passed"], r["tests"]))
        metas = [p[engine]["meta"] for p in [*scans.values(), *olmocr.values()]]
        seconds = [m["seconds"] for m in metas if isinstance(m.get("seconds"), (int, float))]
        ref_chars = sum(m["ref_chars"] for m in a) or 1
        summary[engine] = {
            "scans": {
                "pages": len(a),
                "cer_global": sum(m["cer"] * m["ref_chars"] for m in a) / ref_chars,
                "cer_median": statistics.median(m["cer"] for m in a) if a else None,
                "cer_max": max((m["cer"] for m in a), default=None),
                "wer_median": statistics.median(m["wer"] for m in a) if a else None,
                "accent_edits": sum(m["accent_edits"] for m in a),
                "inserted_words": sum(m["inserted_words"] for m in a),
                "omitted_blocks": sum(m["omitted_blocks"] for m in a),
                "by_book": {book: sum(m["cer"] * m["ref_chars"] for m in ms) / (sum(m["ref_chars"] for m in ms) or 1)
                            for book, ms in groups.items()},
            },
            "olmocr": {
                # comme olmOCR-bench : moyenne des taux de réussite par catégorie
                "by_category": {c: sum(p for p, _ in v) / sum(t for _, t in v) for c, v in b_cat.items()},
                "tests_by_category": {c: sum(t for _, t in v) for c, v in b_cat.items()},
                "score": statistics.mean(sum(p for p, _ in v) / sum(t for _, t in v) for v in b_cat.values()) if b_cat else None,
            },
            "seconds_median": statistics.median(seconds) if seconds else None,
            "failures": sum(m.get("status") not in ("ok",) for m in metas),
            "missing": sum(p[engine].get("missing", False) for p in [*scans.values(), *olmocr.values()]),
        }
    return summary


def diff_html(ref: str, hyp: str) -> str:
    """Diff mot à mot (après normalisation) : <del> = manquant/faux, <ins> = ajouté par le moteur."""
    ref_w, hyp_w = normalize(ref).split(), normalize(hyp).split()
    out = []
    for op in Levenshtein.opcodes(ref_w, hyp_w):
        src = html.escape(" ".join(ref_w[op.src_start:op.src_end]))
        dst = html.escape(" ".join(hyp_w[op.dest_start:op.dest_end]))
        if op.tag == "equal":
            out.append(src)
        else:
            out += [f"<del>{src}</del>"] * bool(src) + [f"<ins>{dst}</ins>"] * bool(dst)
    return " ".join(out)


def report_data(manifest: dict, scans: dict, olmocr: dict, summary: dict) -> dict:
    """Données de la page de synthèse (rendue côté navigateur, voir report_template.html)."""
    by_id = {i["id"]: i for i in manifest["items"]}
    pages = []
    for item_id, page in [*scans.items(), *olmocr.items()]:
        item = by_id[item_id]
        entry = {
            "id": item_id, "set": item["set"],
            "group": item.get("book") or item.get("category"),
            "image": "../" + item["llm_image"], "full_image": "../" + item["image"],
            "engines": {},
        }
        if item["set"] == "scans":
            ref = (BENCH / "reference" / f"{item_id}.txt").read_text(encoding="utf-8")
            furniture = read_furniture(item_id)
            entry["reference"], entry["furniture"] = ref, furniture
        for engine in ENGINES:
            raw = read_output(engine, item)
            r = page[engine]
            e = {"raw": raw, "status": r["meta"].get("status"), "seconds": r["meta"].get("seconds")}
            if item["set"] == "scans":
                e |= {k: r[k] for k in ("cer", "wer", "accent_edits", "inserted_words", "deleted_words", "omitted_blocks")}
                e["diff"] = diff_html(entry["reference"], strip_furniture(raw or "", entry["furniture"]))
            else:
                e |= {"passed": r["passed"], "tests": r["tests"],
                      "results": [{k: t[k] for k in ("type", "passed", "explanation")} for t in r["results"]]}
            entry["engines"][engine] = e
        pages.append(entry)
    return {"engines": list(ENGINES), "summary": summary, "pages": pages}


def render_report(manifest: dict, scans: dict, olmocr: dict, summary: dict) -> str:
    data = json.dumps(report_data(manifest, scans, olmocr, summary), ensure_ascii=False).replace("</", "<\\/")
    template = (BENCH / "report_template.html").read_text(encoding="utf-8")
    return template.replace("/*__DATA__*/null", data)


def main() -> None:
    global ENGINES
    extra = tuple(e for e in OPTIONAL_ENGINES if (BENCH / "runs" / e).is_dir())
    ENGINES = ENGINES[:1] + extra + ENGINES[1:]
    manifest = json.loads((BENCH / "manifest.json").read_text(encoding="utf-8"))
    scans = score_scans([i for i in manifest["items"] if i["set"] == "scans"])
    olmocr = score_olmocr([i for i in manifest["items"] if i["set"] == "olmocr"])
    summary = summarize(manifest, scans, olmocr)
    results = BENCH / "results"
    results.mkdir(exist_ok=True)
    (results / "metrics.json").write_text(json.dumps({"summary": summary, "scans": scans, "olmocr": olmocr},
                                                     ensure_ascii=False, indent=2), encoding="utf-8")
    (results / "report.html").write_text(render_report(manifest, scans, olmocr, summary), encoding="utf-8")
    for engine, s in summary.items():
        print(f"{engine:8} CER scans {s['scans']['cer_global']:.2%}  olmOCR {s['olmocr']['score'] or 0:.1%}  "
              f"méd. {s['seconds_median']}s  échecs {s['failures']}  pages manquantes {s['missing']}")


if __name__ == "__main__":
    main()
