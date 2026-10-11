# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = ["Pillow>=10.4,<13", "PyMuPDF>=1.27.2,<1.28", "rapidfuzz>=3.8,<4", "boto3>=1.40,<2"]
# ///
"""Tests hors réseau et hors GPU du banc OCR : uv run tests/test_ocr_bench.py."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError
from PIL import Image

BENCH_DIR = Path(__file__).resolve().parents[1] / "ocr-bench"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"ocr_bench_{name}", BENCH_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


score = load("score")
prepare = load("prepare")
luna = load("run_luna")
marker2 = load("run_marker2")  # marker / torch ne sont importés que dans main()
haiku_meta = load("haiku_meta")


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)


class NormalizeTests(unittest.TestCase):
    def test_nfc_composes_combining_accents(self):
        self.assertEqual(score.normalize("été"), "été")

    def test_apostrophes_and_quotes_unified(self):
        self.assertEqual(score.normalize("l’arbre ‘x’ lʼun"), "l'arbre 'x' l'un")
        self.assertEqual(score.normalize("« bonjour » “monde”"), '"bonjour""monde"')
        self.assertEqual(score.normalize("«bonjour»"), score.normalize("« bonjour »"))

    def test_dashes_unified(self):
        self.assertEqual(score.normalize("a – b — c − d"), "a - b - c - d")

    def test_ligatures_expanded(self):
        self.assertEqual(score.normalize("ﬁn ﬂux ﬀ ﬃ ﬄ"), "fin flux ff ffi ffl")

    def test_ellipsis(self):
        self.assertEqual(score.normalize("attends…"), "attends...")

    def test_end_of_line_hyphenation_joined(self):
        self.assertEqual(score.normalize("une impor-\ntante idée"), "une importante idée")
        self.assertEqual(score.normalize("impor- \n  tante"), "importante")

    def test_hyphens_between_letters_dropped_everywhere(self):
        # césure gardée, recomposée ou mot composé : même forme pour tous les moteurs
        self.assertEqual(score.normalize("Saint-\nYliades"), "SaintYliades")
        self.assertEqual(score.normalize("Saint- Yliades"), "SaintYliades")
        self.assertEqual(score.normalize("Saint-Yliades"), "SaintYliades")

    def test_inline_html_tags_glued(self):
        self.assertEqual(score.normalize("mot<sup>1</sup> <i>x</i>s"), "mot1 xs")

    def test_furniture_lines_removed(self):
        text = "LES VEILLEURS\nCorps du texte.\n46"
        self.assertEqual(score.strip_furniture(text, ["Les Veilleurs", "46"]), "Corps du texte.")
        self.assertEqual(score.strip_furniture("### LES VEILLEURS\nx", ["LES VEILLEURS"]), "x")

    def test_markdown_headings_bold_italics(self):
        self.assertEqual(score.normalize("## Titre\n\n**gras** et *italique* et __g__ _i_"),
                         "Titre gras et italique et g i")

    def test_snake_case_underscores_kept(self):
        self.assertEqual(score.normalize("a_b_c"), "a_b_c")

    def test_images_and_links(self):
        self.assertEqual(score.normalize("![alt](x.png) voir [ici](http://x)"), "voir ici")

    def test_html_stripped_and_unescaped(self):
        self.assertEqual(score.normalize("<p>a &amp; b</p><br/>c"), "a & b c")

    def test_markdown_table_reduced_to_cells(self):
        table = "| A | B |\n|---|:---:|\n| 1 | 2 |"
        self.assertEqual(score.normalize(table), "A B 1 2")

    def test_html_table(self):
        self.assertEqual(score.normalize("<table><tr><td>A</td><td>B</td></tr></table>"), "A B")

    def test_whitespace_collapsed_and_stripped(self):
        self.assertEqual(score.normalize("  a \t\n\n b  c  "), "a b c")

    def test_french_typographic_spaces_before_punctuation(self):
        self.assertEqual(score.normalize("Quoi ? Oui ; non : ( ok )"), "Quoi? Oui; non: (ok)")

    def test_empty(self):
        self.assertEqual(score.normalize(""), "")


class StripAccentsTests(unittest.TestCase):
    def test_strip(self):
        self.assertEqual(score.strip_accents("Éléphant àçôü"), "Elephant acou")

    def test_idempotent_on_ascii(self):
        self.assertEqual(score.strip_accents("abc"), "abc")


class PageMetricsTests(unittest.TestCase):
    def test_identical_text(self):
        m = score.page_metrics("Le chat dort.", "Le chat dort.")
        self.assertEqual(m["cer"], 0)
        self.assertEqual(m["wer"], 0)
        self.assertEqual(m["accent_edits"], 0)
        self.assertEqual((m["inserted_words"], m["deleted_words"], m["omitted_blocks"]), (0, 0, 0))

    def test_formatting_only_difference_is_free(self):
        m = score.page_metrics("Le **chat** dort", "Le chat\ndort")
        self.assertEqual(m["cer"], 0)

    def test_one_substituted_char(self):
        m = score.page_metrics("abcdefghij", "abcdefghix")
        self.assertEqual(m["ref_chars"], 10)
        self.assertAlmostEqual(m["cer"], 0.1)
        self.assertEqual(m["accent_edits"], 0)
        self.assertEqual(m["wer"], 1.0)

    def test_accent_only_error(self):
        m = score.page_metrics("été", "ete")
        self.assertEqual(m["accent_edits"], 2)
        self.assertAlmostEqual(m["cer"], 2 / 3)

    def test_non_accent_error_not_counted_as_accent(self):
        self.assertEqual(score.page_metrics("chat", "chet")["accent_edits"], 0)

    def test_inserted_words(self):
        m = score.page_metrics("un deux trois", "un deux voici trois encore")
        self.assertEqual(m["inserted_words"], 2)
        self.assertEqual(m["deleted_words"], 0)
        self.assertEqual(m["hyp_words"], 5)

    def test_omitted_block_of_eight_words(self):
        ref = "debut " + " ".join(f"mot{i}" for i in range(8)) + " fin"
        m = score.page_metrics(ref, "debut fin")
        self.assertEqual(m["deleted_words"], 8)
        self.assertEqual(m["omitted_blocks"], 1)

    def test_seven_words_missing_is_not_a_block(self):
        ref = "debut " + " ".join(f"mot{i}" for i in range(7)) + " fin"
        m = score.page_metrics(ref, "debut fin")
        self.assertEqual(m["deleted_words"], 7)
        self.assertEqual(m["omitted_blocks"], 0)

    def test_empty_hypothesis(self):
        m = score.page_metrics("un deux trois", "")
        self.assertEqual(m["cer"], 1.0)
        self.assertEqual(m["wer"], 1.0)
        self.assertEqual(m["hyp_words"], 0)
        self.assertEqual(m["deleted_words"], 3)

    def test_empty_reference_does_not_divide_by_zero(self):
        m = score.page_metrics("", "abc")
        self.assertEqual(m["cer"], 3)
        self.assertEqual(m["ref_words"], 0)

    def test_illisible_counted_in_raw_hypothesis(self):
        m = score.page_metrics("a b", "a [illisible] b [illisible]")
        self.assertEqual(m["illisible"], 2)


class SummarizeTests(unittest.TestCase):
    def metrics(self, cer, chars, **kw):
        base = {"cer": cer, "ref_chars": chars, "wer": cer, "accent_edits": 1,
                "inserted_words": 2, "omitted_blocks": 0, "meta": {"status": "ok", "seconds": 10}}
        return {**base, **kw}

    def test_summary(self):
        manifest = {"items": [
            {"id": "a", "book": "B1", "set": "scans"},
            {"id": "b", "book": "B2", "set": "scans"},
            {"id": "o1", "category": "old_scans", "set": "olmocr"},
            {"id": "o2", "category": "table_tests", "set": "olmocr"},
        ]}
        scans = {"a": {}, "b": {}}
        olm = {"o1": {}, "o2": {}}
        for e in score.ENGINES:
            scans["a"][e] = self.metrics(0.0, 100)
            scans["b"][e] = self.metrics(0.4, 300, meta={"status": "error"})
            olm["o1"][e] = {"tests": 4, "passed": 2, "meta": {"status": "ok", "seconds": 20}}
            olm["o2"][e] = {"tests": 0, "passed": 0, "meta": {"status": "missing"}}
        s = score.summarize(manifest, scans, olm)["luna"]
        self.assertEqual(s["scans"]["pages"], 2)
        self.assertAlmostEqual(s["scans"]["cer_global"], 0.4 * 300 / 400)
        self.assertAlmostEqual(s["scans"]["cer_median"], 0.2)
        self.assertEqual(s["scans"]["cer_max"], 0.4)
        self.assertEqual(s["scans"]["accent_edits"], 2)
        self.assertEqual(s["scans"]["inserted_words"], 4)
        self.assertEqual(s["scans"]["by_book"], {"B1": 0.0, "B2": 0.4})
        self.assertEqual(s["olmocr"]["by_category"], {"old_scans": 0.5})
        self.assertEqual(s["olmocr"]["score"], 0.5)
        self.assertEqual(s["seconds_median"], 15)  # 10 (page a) et 20 (o1) ; erreurs sans durée
        self.assertEqual(s["failures"], 2)  # error + missing

    def test_empty_sets(self):
        s = score.summarize({"items": []}, {}, {})["marker2"]
        self.assertEqual(s["scans"]["pages"], 0)
        self.assertIsNone(s["scans"]["cer_median"])
        self.assertIsNone(s["olmocr"]["score"])
        self.assertIsNone(s["seconds_median"])
        self.assertEqual(s["failures"], 0)


class ScoreScansTests(TempDirCase):
    def test_missing_output_scored_as_empty_and_flagged(self):
        (self.root / "reference").mkdir()
        (self.root / "reference" / "p1.txt").write_text("un deux", encoding="utf-8")
        (self.root / "runs" / "luna").mkdir(parents=True)
        (self.root / "runs" / "luna" / "p1.txt").write_text("un deux", encoding="utf-8")
        (self.root / "runs" / "luna" / "p1.meta.json").write_text('{"status": "ok"}', encoding="utf-8")
        with patch.object(score, "BENCH", self.root):
            pages = score.score_scans([{"id": "p1", "ext": ".txt"}, {"id": "nope", "ext": ".txt"}])
        self.assertEqual(list(pages), ["p1"])  # sans référence : ignorée
        self.assertEqual(pages["p1"]["luna"]["cer"], 0)
        self.assertFalse(pages["p1"]["luna"]["missing"])
        self.assertEqual(pages["p1"]["luna"]["meta"], {"status": "ok"})
        self.assertTrue(pages["p1"]["haiku"]["missing"])
        self.assertEqual(pages["p1"]["haiku"]["cer"], 1.0)
        self.assertEqual(pages["p1"]["haiku"]["meta"], {"status": "missing"})


def make_png(path: Path, size, mode="L", color=255):
    Image.new(mode, size, color).save(path)


class LlmImageTests(TempDirCase):
    def test_large_image_resized_to_long_edge_2000_keeping_ratio_and_L(self):
        src, dst = self.root / "s.png", self.root / "d.png"
        make_png(src, (3000, 4000))
        size = prepare.llm_image(src, dst)
        self.assertEqual(size, (1500, 2000))
        with Image.open(dst) as img:
            self.assertEqual(img.size, (1500, 2000))
            self.assertEqual(img.mode, "L")

    def test_wide_image_long_edge_is_width(self):
        src, dst = self.root / "s.png", self.root / "d.png"
        make_png(src, (4000, 1000))
        self.assertEqual(prepare.llm_image(src, dst), (2000, 500))

    def test_small_image_not_upscaled(self):
        src, dst = self.root / "s.png", self.root / "d.png"
        make_png(src, (300, 200))
        self.assertEqual(prepare.llm_image(src, dst), (300, 200))

    def test_rgb_kept_and_other_modes_converted(self):
        src, dst = self.root / "s.png", self.root / "d.png"
        make_png(src, (50, 50), "RGB", (1, 2, 3))
        prepare.llm_image(src, dst)
        with Image.open(dst) as img:
            self.assertEqual(img.mode, "RGB")
        make_png(src, (50, 50), "RGBA", (1, 2, 3, 255))
        prepare.llm_image(src, dst)
        with Image.open(dst) as img:
            self.assertEqual(img.mode, "RGB")

    def test_oversize_file_aborts(self):
        src, dst = self.root / "s.png", self.root / "d.png"
        make_png(src, (50, 50))
        with patch.object(prepare, "LLM_MAX_BYTES", 10), self.assertRaises(SystemExit):
            prepare.llm_image(src, dst)


class EntryTests(TempDirCase):
    def test_entry_fields(self):
        with patch.object(prepare, "BENCH", self.root):
            (self.root / "inputs").mkdir()
            image, llm = self.root / "inputs" / "x.png", self.root / "inputs" / "x.llm.png"
            make_png(image, (30, 20), "RGB", (9, 9, 9))
            make_png(llm, (15, 10), "RGB", (9, 9, 9))
            e = prepare.entry("x", "scans", image, llm, book="Livre", page=3)
            o = prepare.entry("y", "olmocr", image, llm, category="old_scans")
        self.assertEqual(e["id"], "x")
        self.assertEqual(e["set"], "scans")
        self.assertEqual(e["image"], "inputs/x.png")
        self.assertEqual(e["llm_image"], "inputs/x.llm.png")
        self.assertEqual(e["size"], [30, 20])
        self.assertEqual(e["llm_size"], [15, 10])
        self.assertEqual(e["mode"], "RGB")
        self.assertEqual(e["sha256"], prepare.sha256(image))
        self.assertEqual(len(e["sha256"]), 64)
        self.assertEqual((e["prompt"], e["ext"]), ("prompt.md", ".txt"))
        self.assertEqual((e["book"], e["page"]), ("Livre", 3))
        self.assertEqual((o["prompt"], o["ext"]), ("prompt_olmocr.md", ".md"))
        self.assertEqual(o["category"], "old_scans")


class PrepareForceGuardTests(TempDirCase):
    def run_main(self, *argv):
        with patch.object(prepare, "BENCH", self.root), patch.object(prepare, "INPUTS", self.root / "inputs"), \
                patch.object(prepare, "sample_scans", return_value=[]), \
                patch.object(prepare, "sample_olmocr", return_value=[]), \
                patch.object(sys, "argv", ["prepare.py", *argv]):
            prepare.main()

    def test_existing_manifest_refused_without_force(self):
        manifest = self.root / "manifest.json"
        manifest.write_text("OLD", encoding="utf-8")
        with self.assertRaises(SystemExit) as ctx:
            self.run_main()
        self.assertIn("--force", str(ctx.exception))
        self.assertEqual(manifest.read_text(encoding="utf-8"), "OLD")

    def test_force_overwrites_manifest(self):
        (self.root / "manifest.json").write_text("OLD", encoding="utf-8")
        self.run_main("--force", "--seed", "7")
        data = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(data["seed"], 7)
        self.assertEqual(data["items"], [])

    def test_fresh_run_writes_manifest_and_removes_stale_files(self):
        images = self.root / "inputs" / "images"
        images.mkdir(parents=True)
        stale = images / "old.png"
        stale.write_bytes(b"x")
        self.run_main()
        self.assertTrue((self.root / "manifest.json").exists())
        self.assertFalse(stale.exists())


class FakeClient:
    """Client Bedrock factice : rejoue une liste de réponses ou d'exceptions."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def converse(self, **request):
        self.requests.append(json.loads(json.dumps(request, default=lambda b: "<bytes>")))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def client_error(code, message="boom"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "Converse")


def ok_response(text="Bonjour", stop="end_turn"):
    return {"output": {"message": {"content": [{"text": text}]}}, "stopReason": stop,
            "usage": {"inputTokens": 5, "outputTokens": 2}, "metrics": {"latencyMs": 123}}


class LunaTests(TempDirCase):
    def setUp(self):
        super().setUp()
        (self.root / "p.md").write_text("Transcris.", encoding="utf-8")
        make_png(self.root / "p.png", (10, 10))
        self.item = {"id": "page1", "prompt": "p.md", "llm_image": "p.png", "ext": ".txt"}
        patcher = patch.object(luna, "BENCH", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_success_builds_meta(self):
        client = FakeClient(ok_response("  Bonjour  \n"))
        result = luna.transcribe(client, self.item, 1234)
        self.assertEqual(result["text"], "Bonjour")
        meta = result["meta"]
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(meta["engine"], "luna")
        self.assertEqual(meta["model"], luna.MODEL_ID)
        self.assertEqual(meta["chars"], 7)
        self.assertEqual(meta["latency_ms"], 123)
        self.assertEqual(meta["usage"], {"inputTokens": 5, "outputTokens": 2})
        self.assertEqual(meta["temperature"], 0)
        request = client.requests[0]
        self.assertEqual(request["modelId"], luna.MODEL_ID)
        self.assertEqual(request["inferenceConfig"], {"maxTokens": 1234, "temperature": 0})
        content = request["messages"][0]["content"]
        self.assertEqual(content[1], {"text": "Transcris."})
        self.assertEqual(content[0]["image"]["format"], "png")

    def test_multiple_text_blocks_joined_and_non_text_ignored(self):
        resp = ok_response()
        resp["output"]["message"]["content"] = [{"text": "a"}, {"reasoningContent": {}}, {"text": "b"}]
        self.assertEqual(luna.transcribe(FakeClient(resp), self.item, 10)["text"], "a\nb")

    def test_suspect_when_truncated_or_empty(self):
        truncated = luna.transcribe(FakeClient(ok_response(stop="max_tokens")), self.item, 10)
        empty = luna.transcribe(FakeClient(ok_response(text="")), self.item, 10)
        self.assertEqual(truncated["meta"]["status"], "suspect")
        self.assertEqual(empty["meta"]["status"], "suspect")

    def test_temperature_dropped_and_retried_without_sleep(self):
        client = FakeClient(client_error("ValidationException", "temperature is not supported"), ok_response())
        with patch.object(luna.time, "sleep") as sleep:
            result = luna.transcribe(client, self.item, 10)
        self.assertEqual(len(client.requests), 2)
        self.assertIn("temperature", client.requests[0]["inferenceConfig"])
        self.assertNotIn("temperature", client.requests[1]["inferenceConfig"])
        self.assertIsNone(result["meta"]["temperature"])
        self.assertEqual(result["meta"]["status"], "ok")
        sleep.assert_not_called()

    def test_other_validation_error_not_retried(self):
        client = FakeClient(client_error("ValidationException", "bad image"))
        with patch.object(luna.time, "sleep"), self.assertRaises(ClientError):
            luna.transcribe(client, self.item, 10)
        self.assertEqual(len(client.requests), 1)

    def test_throttling_retried_with_backoff(self):
        client = FakeClient(client_error("ThrottlingException"), client_error("ThrottlingException"), ok_response())
        with patch.object(luna.time, "sleep") as sleep:
            result = luna.transcribe(client, self.item, 10)
        self.assertEqual(len(client.requests), 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 10])
        self.assertEqual(result["meta"]["status"], "ok")

    def test_non_retryable_error_raised_immediately(self):
        client = FakeClient(client_error("AccessDeniedException"))
        with patch.object(luna.time, "sleep") as sleep, self.assertRaises(ClientError):
            luna.transcribe(client, self.item, 10)
        sleep.assert_not_called()

    def test_gives_up_after_six_attempts(self):
        client = FakeClient(*[client_error("ThrottlingException") for _ in range(6)])
        with patch.object(luna.time, "sleep"), self.assertRaises(ClientError):
            luna.transcribe(client, self.item, 10)
        self.assertEqual(len(client.requests), 6)

    def test_run_item_writes_output_and_meta(self):
        out_dir = self.root / "runs"
        out_dir.mkdir()
        line = luna.run_item(FakeClient(ok_response("Texte")), self.item, out_dir, 10, False)
        self.assertEqual((out_dir / "page1.txt").read_text(encoding="utf-8"), "Texte\n")
        meta = json.loads((out_dir / "page1.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "ok")
        self.assertIn("page1: ok", line)

    def test_run_item_error_writes_error_meta_and_no_output(self):
        out_dir = self.root / "runs"
        out_dir.mkdir()
        line = luna.run_item(FakeClient(client_error("AccessDeniedException", "nope")), self.item, out_dir, 10, False)
        meta = json.loads((out_dir / "page1.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "error")
        self.assertIn("ClientError", meta["error"])
        self.assertIn("nope", meta["error"])
        self.assertFalse((out_dir / "page1.txt").exists())
        self.assertIn("ERREUR", line)

    def test_run_item_skips_done_unless_overwrite(self):
        out_dir = self.root / "runs"
        out_dir.mkdir()
        (out_dir / "page1.txt").write_text("old", encoding="utf-8")
        (out_dir / "page1.meta.json").write_text('{"status": "ok"}', encoding="utf-8")
        client = FakeClient(ok_response("new"))
        self.assertIn("déjà fait", luna.run_item(client, self.item, out_dir, 10, False))
        self.assertEqual(client.requests, [])
        luna.run_item(client, self.item, out_dir, 10, True)
        self.assertEqual((out_dir / "page1.txt").read_text(encoding="utf-8"), "new\n")


class GpuMemoryTests(unittest.TestCase):
    def run_with(self, **kw):
        return patch.object(marker2.subprocess, "run", **kw)

    def test_parses_first_gpu(self):
        with self.run_with(return_value=Mock(stdout="8123\n4000\n")) as run:
            self.assertEqual(marker2.gpu_memory_used_mb(), 8123)
        self.assertEqual(run.call_args.args[0][0], "nvidia-smi")

    def test_none_when_nvidia_smi_missing(self):
        with self.run_with(side_effect=FileNotFoundError()):
            self.assertIsNone(marker2.gpu_memory_used_mb())

    def test_none_when_command_fails_or_times_out(self):
        with self.run_with(side_effect=subprocess.CalledProcessError(1, "nvidia-smi")):
            self.assertIsNone(marker2.gpu_memory_used_mb())
        with self.run_with(side_effect=subprocess.TimeoutExpired("nvidia-smi", 10)):
            self.assertIsNone(marker2.gpu_memory_used_mb())

    def test_none_on_garbage_or_empty_output(self):
        with self.run_with(return_value=Mock(stdout="N/A\n")):
            self.assertIsNone(marker2.gpu_memory_used_mb())
        with self.run_with(return_value=Mock(stdout="")):
            self.assertIsNone(marker2.gpu_memory_used_mb())


class HaikuMetaTests(TempDirCase):
    def run_meta(self, text=None):
        manifest = {"items": [{"id": "p1", "ext": ".txt"}]}
        (self.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / "runs" / "haiku").mkdir(parents=True, exist_ok=True)
        if text is not None:
            (self.root / "runs" / "haiku" / "p1.txt").write_text(text, encoding="utf-8")
        with patch.object(haiku_meta, "BENCH", self.root), \
                patch.object(sys, "argv", ["haiku_meta.py", "p1", "12.345", "999"]):
            haiku_meta.main()
        return json.loads((self.root / "runs" / "haiku" / "p1.meta.json").read_text(encoding="utf-8"))

    def test_ok(self):
        meta = self.run_meta("  Bonjour\n")
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(meta["chars"], 7)
        self.assertEqual(meta["seconds"], round(12.345, 2))
        self.assertEqual(meta["agent_tokens"], 999)
        self.assertEqual(meta["engine"], "haiku")

    def test_empty(self):
        meta = self.run_meta("  \n")
        self.assertEqual(meta["status"], "empty")
        self.assertEqual(meta["chars"], 0)

    def test_missing(self):
        meta = self.run_meta()
        self.assertEqual(meta["status"], "missing")
        self.assertEqual(meta["chars"], 0)


if __name__ == "__main__":
    unittest.main()
