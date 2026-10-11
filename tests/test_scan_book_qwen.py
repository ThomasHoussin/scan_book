# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = ["Pillow>=10.4,<13", "PyMuPDF>=1.27.2,<1.28", "opencv-python>=4.11,<5", "numpy>=2,<3", "rapidocr-onnxruntime==1.4.4"]
# ///
"""Offline tests, including Tk: uv run tests/test_scan_book_qwen.py."""
import hashlib
import copy
import gc
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan = load("variant", ROOT / "tools/scan_book_qwen.py")
original = load("original", ROOT / "tools/scan_book.py")
base = load("existing_tests", ROOT / "tests/test_scan_book.py")
# Reuse transaction, acquisition and UI scenarios against the copied script.
# Existing subprocess imports of scan_book also exercise interoperability.
base.scan = scan
base.SOURCE = ROOT / "tools/scan_book_qwen.py"


class CropTests(unittest.TestCase):
    def test_margin_maps_outward_with_independent_dpi(self):
        crop, warning = scan.crop_from_prediction(
            {"certain": True, "bbox": [200.1, 100.1, 799.9, 899.9]}, (1000, 2000), (254, 508))
        self.assertEqual(crop, [195, 190, 805, 1810])
        self.assertIsNone(warning)

    def test_edges_and_whole_image_are_accepted(self):
        for bounds in ([0, 0, 1000, 1000], [0, 100, 800, 1000], [200, 0, 1000, 900]):
            crop, warning = scan.crop_from_prediction({"certain": True, "bbox": bounds},
                                                       (1000, 1000), (254, 254))
            self.assertIsNone(warning)
            self.assertEqual(crop, [max(0, bounds[0]-5), max(0, bounds[1]-5),
                                    min(1000, bounds[2]+5), min(1000, bounds[3]+5)])

    def test_invalid_uncertain_and_small_boxes_rejected(self):
        for prediction in (None, {}, {"certain": False, "bbox": [0, 0, 1000, 1000]},
                           *({"certain": True, "bbox": b} for b in (
                               [0, 0, 10, 10], [-1, 0, 900, 900], [0, 0, 1001, 900],
                               [500, 0, 400, 900], [0, 0, 900], [False, 0, 900, 900],
                               [0, 0, float('nan'), 900], ['0', 0, 900, 900]))):
            with self.subTest(prediction=prediction):
                crop, reason = scan.crop_from_prediction(prediction, (1000, 1000), (300, 300))
                self.assertIsNone(crop)
                self.assertTrue(reason)

    def test_rotations_grayscale_and_anisotropic_dpi_use_saved_coordinate_system(self):
        raw = Image.fromarray(np.arange(250*180, dtype=np.uint8).reshape(250, 180))
        for angle in (0., 2.):
            expected, expected_dpi = scan.apply_transform(raw, (150, 300),
                                        dict(quarter_turns=2, angle=angle, crop=None))
            def predict(image):
                self.assertEqual(image.tobytes(), expected.tobytes())
                return {"certain": True, "bbox": [100, 100, 900, 900]}
            with patch.object(scan, 'detect_half_turn', return_value=180), \
                 patch.object(scan, 'detect_geometry', return_value=dict(
                     quarter_turns=0, angle=angle, crop=[1, 1, 5, 5], review=False)), \
                 patch.object(scan, 'qwen_prediction', side_effect=predict):
                transform = scan.detect_transform(raw, (150, 300))
            output, dpi = scan.apply_transform(raw, (150, 300), transform)
            self.assertEqual(output.tobytes(), expected.crop(transform['crop']).tobytes())
            self.assertEqual(dpi, expected_dpi)
            self.assertEqual(output.mode, 'L')
            self.assertEqual(transform['method'], 'qwen3.5')

    def test_errors_keep_orientation_and_no_crop(self):
        for error in (TimeoutError('timeout'), ValueError('bad JSON'), OSError('offline')):
            with patch.object(scan, 'detect_half_turn', return_value=180), \
                 patch.object(scan, 'qwen_prediction', side_effect=error):
                tx = scan.detect_transform(Image.new('RGB', (200, 300)), (300, 300))
            self.assertEqual(tx['quarter_turns'], 2)
            self.assertIsNone(tx['crop'])
            self.assertTrue(tx['review'])

    def test_qwen_crop_is_applied_without_post_detection_ocr(self):
        with patch.object(scan, 'detect_half_turn', return_value=0), \
             patch.object(scan, 'qwen_prediction', return_value={'certain': True, 'bbox': [100, 100, 900, 900]}), \
             patch.object(scan, 'orientation_engine', side_effect=AssertionError('unexpected post-Qwen OCR')) as ocr:
            tx = scan.detect_transform(Image.new('RGB', (1000, 2000)), (300, 300))
        ocr.assert_not_called()
        self.assertEqual(tx['crop'], [94, 194, 906, 1806])
        self.assertNotIn('crop_warning', tx)
        self.assertNotIn('crop_error', tx)


class OllamaTests(unittest.TestCase):
    def setUp(self):
        scan.ensure_qwen_model.cache_clear()
        self.addCleanup(scan.ensure_qwen_model.cache_clear)

    def test_existing_model_reused_and_missing_model_pulled_once(self):
        for models in ([{'name': scan.QWEN_MODEL}], []):
            scan.ensure_qwen_model.cache_clear()
            with patch.object(scan, 'ollama_json', return_value={'models': models}), \
                 patch.object(scan, 'ollama_lines', return_value=iter([{'status': 'success'}])) as pull:
                scan.ensure_qwen_model()
                scan.ensure_qwen_model()
                self.assertEqual(pull.call_count, 0 if models else 1)

    def test_interrupted_download_can_retry(self):
        with patch.object(scan, 'ollama_json', return_value={'models': []}), \
             patch.object(scan, 'ollama_lines', side_effect=[iter([{'status': 'pulling'}]), iter([{'status': 'success'}])]):
            with self.assertRaises(scan.ScanError):
                scan.ensure_qwen_model()
            scan.ensure_qwen_model()

    def test_request_preserves_aspect_and_uses_bounded_structured_generation(self):
        import base64
        def answer(path, payload):
            self.assertEqual(path, 'chat')
            self.assertEqual(payload['keep_alive'], '30m')
            self.assertEqual(payload['options']['num_ctx'], 8192)
            self.assertEqual(payload['format'], scan.QWEN_SCHEMA)
            image = Image.open(io.BytesIO(base64.b64decode(payload['messages'][0]['images'][0])))
            self.assertEqual(image.size, (800, 1600))
            return {'done': True, 'message': {'content': '{"certain":true,"bbox":[0,0,1000,1000]}'}}
        with patch.object(scan, 'ensure_qwen_model'), patch.object(scan, 'ollama_json', side_effect=answer):
            self.assertTrue(scan.qwen_prediction(Image.new('L', (2000, 4000)))['certain'])

    def test_transport_is_local_and_rejects_http_errors_and_timeout(self):
        for failure in ('http', 'timeout'):
            with patch.object(scan.http.client, 'HTTPConnection') as factory:
                connection = factory.return_value
                if failure == 'http':
                    connection.getresponse.return_value.status = 500
                else:
                    connection.connect.side_effect = TimeoutError('timeout')
                with self.assertRaises(scan.ScanError):
                    scan.ollama_json('chat', {})
                factory.assert_called_once_with('127.0.0.1', 11434, timeout=5)
                connection.close.assert_called_once()

    def test_truncated_response_rejected(self):
        with patch.object(scan, 'ensure_qwen_model'), patch.object(scan, 'ollama_json', return_value={
                'done': True, 'done_reason': 'length', 'message': {'content': '{}'}}):
            with self.assertRaises(scan.ScanError):
                scan.qwen_prediction(Image.new('RGB', (20, 20)))

    def test_transport_parses_split_lines_and_enforces_overall_deadline(self):
        with patch.object(scan.http.client, 'HTTPConnection') as factory:
            response = factory.return_value.getresponse.return_value
            response.status = 200
            response.read1.side_effect = [b'{"sta', b'tus":"success"}\n', b'']
            self.assertEqual(list(scan.ollama_lines('pull', {})), [{'status': 'success'}])
        with patch.object(scan.http.client, 'HTTPConnection') as factory, \
             patch.object(scan.time, 'monotonic', side_effect=[0, 0, 0, 181]):
            factory.return_value.getresponse.return_value.status = 200
            with self.assertRaises(scan.ScanError):
                scan.ollama_json('chat', {})

    def test_malformed_json_and_stream_error_are_rejected(self):
        with patch.object(scan.http.client, 'HTTPConnection') as factory:
            response = factory.return_value.getresponse.return_value
            response.status = 200
            response.read1.side_effect = [b'not JSON\n', b'']
            with self.assertRaises(ValueError):
                scan.ollama_json('chat', {})
        with patch.object(scan, 'ollama_lines', return_value=iter([{'error': 'out of memory'}])):
            with self.assertRaises(scan.ScanError):
                scan.ollama_json('chat', {})


class WorkflowTests(base.SessionFixture):
    def test_export_skips_missing_images_before_validation_and_preserves_order(self):
        with scan.Session(self.root, "livre", create=True) as session:
            for color in ("red", "green", "blue", "white"):
                self.capture_deferred(session, color)
            for number in (1, 3):
                self.process_page(session, session.page(number))
                session.validate_page(number)
            (session.path / "pages/0002.png").unlink()
            (session.path / "raw/0004.png").unlink()
            manifest = session.manifest.read_bytes()
            with patch("builtins.print") as report:
                target = scan.export_pdf(session)
            warnings = " ".join(call.args[0] for call in report.call_args_list)
            self.assertIn("0002", warnings)
            self.assertIn("0004", warnings)
            self.assertIn("image manquante", warnings)
            self.assertEqual(session.manifest.read_bytes(), manifest)
            with base.pymupdf.open(target) as doc:
                self.assertEqual(doc.page_count, 2)
                self.assertEqual(doc[0].get_pixmap().pixel(20, 20), (255, 0, 0))
                self.assertEqual(doc[1].get_pixmap().pixel(20, 20), (0, 0, 255))

    def test_worker_skips_missing_images_and_processes_next_page(self):
        with scan.Session(self.root, "livre", create=True) as session:
            pages = [self.capture_deferred(session) for _ in range(3)]
            (session.path / "pages/0001.png").unlink()
            (session.path / "raw/0002.png").unlink()
            worker = scan.ProcessingWorker(session)
            for page in pages:
                worker.enqueue(page)
            with patch.object(scan, "detect_transform", return_value=scan.identity_transform()) as detect:
                worker.start()
                try:
                    events = [worker.results.get(timeout=5) for _ in pages]
                finally:
                    worker.stop()
                    worker.join(5)
            self.assertEqual([event[0] for event in events], ["skipped", "skipped", "processed"])
            self.assertTrue(all(event[2] is None for event in events))
            self.assertEqual(detect.call_count, 1)
            self.assertFalse((session.path / "pages/0001.png").exists())

    def test_deletion_during_processing_is_not_undone_and_restore_allows_retry(self):
        with scan.Session(self.root, "livre", create=True) as session:
            page = self.capture_deferred(session)
            revision = page["qwen"]["revision"]
            _, raw = session.processing_input(1, revision)
            path = session.path / page["image"]
            saved = path.read_bytes()
            path.unlink()
            self.assertIsNone(session.finish_processing(1, revision, scan.identity_transform(), raw, page["raw_dpi"]))
            self.assertFalse(path.exists())
            self.assertEqual(session.available_pages(), [])
            path.write_bytes(saved)
            self.assertEqual(len(session.available_pages()), 1)
            self.assertIsNotNone(self.process_page(session, page))

    def test_corrupt_image_still_blocks_export_and_preserves_existing_pdf(self):
        with scan.Session(self.root, "livre", create=True) as session:
            page = self.capture_deferred(session)
            self.process_page(session, page)
            session.validate_page(1)
            target = scan.export_pdf(session)
            previous = target.read_bytes()
            (session.path / page["image"]).write_bytes(b"invalid PNG")
            with self.assertRaises(OSError):
                scan.export_pdf(session)
            self.assertEqual(target.read_bytes(), previous)

    def capture_deferred(self, session, color="red", replace=None):
        return session.capture(Image.new("RGB", (300, 450), color), (150, 300),
                               copy.deepcopy(scan.DEFAULT_SETTINGS), replace)

    def process_page(self, session, page):
        revision = page["qwen"]["revision"]
        saved, raw = session.processing_input(page["number"], revision)
        transform = dict(quarter_turns=1, angle=0., crop=None, method="test", review=False)
        image, dpi = scan.apply_transform(raw, saved["raw_dpi"], transform)
        return session.finish_processing(page["number"], revision, transform, image, dpi)

    def test_deferred_recovery_never_calls_qwen_and_export_requires_validation(self):
        with scan.Session(self.root, "livre", create=True) as session:
            session.begin_capture(Image.new("RGB", (300, 450)), (150, 300),
                                  scan.DEFAULT_SETTINGS, deferred=True)
        with patch.object(scan, "detect_transform", side_effect=AssertionError("no inference on open")):
            with scan.Session(self.root, "livre") as session:
                page = session.page(1)
                self.assertEqual(page["qwen"]["processing"], "pending")
                with self.assertRaises(scan.ScanError):
                    scan.export_pdf(session)
                with self.assertRaises(scan.ScanError):
                    session.validate_page(1)
                self.process_page(session, page)
                with self.assertRaises(scan.ScanError):
                    scan.export_pdf(session)
                session.validate_page(1)
                self.assertTrue(scan.export_pdf(session).exists())

    def test_old_results_cannot_overwrite_retake_or_manual_correction(self):
        with scan.Session(self.root, "livre", create=True) as session:
            first = self.capture_deferred(session)
            stale_raw = scan.load_image(session.path / first["raw"])
            second = self.capture_deferred(session, "blue", replace=1)
            self.assertIsNone(session.finish_processing(1, first["qwen"]["revision"],
                scan.identity_transform(), stale_raw, first["raw_dpi"]))
            self.assertEqual(session.page(1), second)
            self.assertEqual(scan.load_image(session.path / second["raw"]).getpixel((0, 0)), (0, 0, 255))
            session.correct(1, dict(quarter_turns=0, angle=0., crop=[0, 0, 100, 100], method="manual", review=False))
            corrected = session.page(1)
            self.assertIsNone(session.finish_processing(1, second["qwen"]["revision"],
                scan.identity_transform(), stale_raw, second["raw_dpi"]))
            self.assertEqual(session.page(1), corrected)

    def test_correction_of_completed_book_restores_review_atomically(self):
        with scan.Session(self.root, "livre", create=True) as session:
            page = self.capture_deferred(session)
            self.process_page(session, page)
            session.validate_page(1)
            path = scan.export_pdf(session)
            previous_pdf = path.read_bytes()
            session.set_phase("complete")
            session.correct(1, scan.identity_transform())
            self.assertEqual(session.snapshot()["qwen_workflow"]["phase"], "review")
            self.assertFalse(session.page(1)["qwen"]["validated"])
            with self.assertRaises(scan.ScanError):
                scan.export_pdf(session)
            self.assertEqual(path.read_bytes(), previous_pdf)
        with scan.Session(self.root, "livre") as session:
            self.assertEqual(session.snapshot()["qwen_workflow"]["phase"], "review")

    def test_stop_preserves_pending_queue_and_review_phase(self):
        started, release = threading.Event(), threading.Event()
        def slow(*args):
            started.set()
            self.assertTrue(release.wait(5))
            return scan.identity_transform()
        with scan.Session(self.root, "livre", create=True) as session:
            pages = [self.capture_deferred(session) for _ in range(2)]
            session.set_phase("review")
            worker = scan.ProcessingWorker(session)
            with patch.object(scan, "detect_transform", side_effect=slow) as detect:
                for page in pages:
                    worker.enqueue(page)
                worker.start()
                try:
                    self.assertTrue(started.wait(5))
                    worker.stop()
                finally:
                    release.set()
                    worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(detect.call_count, 1)
                self.assertTrue(all(p["qwen"]["processing"] == "pending" for p in session.snapshot()["pages"]))
        with scan.Session(self.root, "livre") as session, \
             patch.object(scan, "detect_transform", return_value=scan.identity_transform()) as detect:
            self.assertEqual(session.snapshot()["qwen_workflow"]["phase"], "review")
            worker = scan.ProcessingWorker(session)
            for page in session.snapshot()["pages"]:
                worker.enqueue(page)
            worker.start()
            try:
                for _ in range(2):
                    self.assertIsNone(worker.results.get(timeout=5)[2])
            finally:
                worker.stop()
                worker.join(5)
            self.assertEqual(detect.call_count, 2)

    def test_processing_write_failure_is_reported_and_resumable(self):
        with scan.Session(self.root, "livre", create=True) as session:
            page = self.capture_deferred(session)
            worker = scan.ProcessingWorker(session)
            worker.enqueue(page)
            with patch.object(scan, "detect_transform", return_value=scan.identity_transform()), \
                 patch.object(scan, "atomic_png", side_effect=OSError("disk full")):
                worker.start()
                event, number, error = worker.results.get(timeout=5)
                worker.join(5)
            self.assertEqual((event, number), ("processing_error", 1))
            self.assertIn("disk full", error)
            self.assertEqual(session.page(1)["qwen"]["processing"], "pending")
        with scan.Session(self.root, "livre") as session:
            self.assertFalse(session.pending.exists())
            self.process_page(session, session.page(1))

    def test_network_cancellation_interrupts_blocked_inference(self):
        started, interrupted = threading.Event(), threading.Event()
        connection = Mock()
        connection.sock.shutdown.side_effect = lambda *_: interrupted.set()
        def block_response():
            started.set()
            if not interrupted.wait(5):
                raise AssertionError("HTTP was not interrupted")
            raise OSError("cancelled")
        connection.getresponse.side_effect = block_response
        with scan.Session(self.root, "livre", create=True) as session:
            page = self.capture_deferred(session)
            worker = scan.ProcessingWorker(session)
            worker.enqueue(page)
            with patch.object(scan, "detect_transform", side_effect=lambda *_: scan.ollama_json("chat", {})), \
                 patch.object(scan.http.client, "HTTPConnection", return_value=connection):
                worker.start()
                try:
                    self.assertTrue(started.wait(5))
                    worker.stop()
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(session.page(1)["qwen"]["processing"], "pending")
                finally:
                    interrupted.set()
                    worker.stop()
                    worker.join(5)


class FakeScanner:
    id = "fake"
    def __init__(self, selector):
        self.calls = 0
    def configure(self, settings):
        self.effective = {"name": "Scanner simulé", "x_dpi": 300, "color": settings["color"]}
        return copy.deepcopy(self.effective)
    def acquire(self):
        self.calls += 1
        mode = "RGB" if self.effective["color"] == "color" else "L"
        return Image.new(mode, (300, 450), self.calls * 30), (150, 300)


@unittest.skipIf(base.os.environ.get("SCAN_BOOK_SKIP_GUI") == "1", "Tk explicitement désactivé")
class WorkflowGuiTests(base.SessionFixture):
    def test_review_skips_missing_pending_and_processed_pages_then_exports(self):
        with scan.Session(self.root, "livre", create=True) as session:
            for _ in range(3):
                session.capture(Image.new("RGB", (300, 450)), (300, 300), scan.DEFAULT_SETTINGS)
            for number in (2, 3):
                session.correct(number, scan.identity_transform())
            (session.path / "pages/0001.png").unlink()
            (session.path / "pages/0002.png").unlink()
            session.set_phase("review")
        with scan.Session(self.root, "livre") as session:
            app = self.open_app(session)
            try:
                self.assertEqual(app.current["number"], 3)
                self.assertIn("0001, 0002", app.progress.get())
                self.assertIn("Images manquantes", app.progress.get())
                self.assertIn("1 à valider", app.progress.get())
                app.command("scan")
                self.pump(app)
                self.assertIn("Vérification terminée", app.status.get())
                self.assertFalse(app.storage_error)
                self.assertTrue(app.buttons["export"].instate(["!disabled"]))
                app.command("export")
                self.wait_for(app, lambda: app.closed)
                with base.pymupdf.open(session.path / "livre.pdf") as doc:
                    self.assertEqual(doc.page_count, 1)
            finally:
                self.close(app)

    def test_all_images_missing_finishes_review_without_export(self):
        with scan.Session(self.root, "livre", create=True) as session:
            session.capture(Image.new("RGB", (300, 450)), (300, 300), scan.DEFAULT_SETTINGS)
            (session.path / "pages/0001.png").unlink()
            session.set_phase("review")
            app = self.open_app(session)
            try:
                self.assertIsNone(app.current)
                self.assertIn("0001", app.progress.get())
                self.assertIn("Aucune page disponible", app.status.get())
                self.assertTrue(app.buttons["export"].instate(["disabled"]))
                self.assertFalse(app.storage_error)
            finally:
                self.close(app)

    def setUp(self):
        super().setUp()
        for mock in (patch.object(scan, "WiaScanner", FakeScanner),
                     patch.object(scan, "com_context", nullcontext),
                     patch.object(scan, "detect_transform", return_value=dict(
                         quarter_turns=0, angle=0., crop=[10, 10, 290, 440], method="fake", review=False))):
            mock.start()
            self.addCleanup(mock.stop)

    def tearDown(self):
        gc.collect()
        super().tearDown()

    def wait_for(self, app, predicate):
        deadline = time.monotonic() + 8
        while not predicate() and time.monotonic() < deadline:
            app.root.update()
            time.sleep(.01)
        self.assertTrue(predicate())

    def pump(self, app):
        self.wait_for(app, lambda: not app.busy or app.closed)

    def close(self, app):
        if not app.closed:
            app.shutdown()
            self.wait_for(app, lambda: app.closed)
        self.assertFalse(app.worker.is_alive())
        if app.processor:
            self.assertFalse(app.processor.is_alive())

    def open_app(self, session):
        app = scan.Preview(session, copy.deepcopy(session.data["settings"]))
        app.root.withdraw()
        self.pump(app)
        return app

    def test_bulk_capture_does_not_wait_for_qwen_and_progressive_review(self):
        started, release = threading.Event(), threading.Event()
        second_started, second_release = threading.Event(), threading.Event()
        calls = 0
        def slow(raw, dpi):
            nonlocal calls
            calls += 1
            started.set()
            if calls == 2:
                second_started.set()
            gate = release if calls == 1 else second_release
            if not gate.wait(8):
                raise AssertionError("test release timed out")
            return dict(quarter_turns=0, angle=0., crop=[10, 10, 290, 440], method="fake", review=False)
        with scan.Session(self.root, "livre", create=True) as session, \
             patch.object(scan, "detect_transform", side_effect=slow):
            app = self.open_app(session)
            try:
                app.command("scan")
                app.command("scan")  # No queued duplicate keystroke.
                self.pump(app)
                self.assertEqual(len(session.snapshot()["pages"]), 1)
                self.assertTrue(started.wait(5))
                app.command("scan")
                self.pump(app)
                self.assertEqual(len(session.snapshot()["pages"]), 2)
                self.assertEqual(app.display_image.size, (300, 450))
                self.assertIn("0 traitées", app.progress.get())
                app.command("export")
                self.pump(app)
                self.assertEqual(app.phase, "review")
                self.assertIsNone(app.current)
                self.assertTrue(app.buttons["export"].instate(["disabled"]))
                app.command("scan")
                self.assertFalse(app.busy)
                release.set()
                self.wait_for(app, lambda: app.current is not None)
                self.assertEqual(app.current["number"], 1)
                self.assertEqual(app.display_image.size, (280, 430))
                app.command("correct")
                app.crop = [20, 30, 200, 350]
                app.accept()
                self.pump(app)
                self.assertFalse(session.page(1)["qwen"]["validated"])
                self.assertEqual(app.display_image.size, (180, 320))
                app.key(SimpleNamespace(keysym="Return"))
                self.pump(app)
                self.assertTrue(second_started.wait(5))
                self.assertIsNone(app.current)
                self.assertIn("attente", app.status.get())
                self.assertTrue(session.page(1)["qwen"]["validated"])
                second_release.set()
                self.wait_for(app, lambda: app.current is not None)
                self.assertEqual(app.current["number"], 2)
                app.command("scan")
                self.pump(app)
                self.assertIn("Vérification terminée", app.status.get())
                app.command("export")
                self.wait_for(app, lambda: app.closed)
                with base.pymupdf.open(session.path / "livre.pdf") as doc:
                    self.assertEqual(doc.page_count, 2)
                    self.assertAlmostEqual(doc[0].rect.width, 180 * 72 / 150, places=3)
                    self.assertAlmostEqual(doc[0].rect.height, 320 * 72 / 300, places=3)
            finally:
                release.set()
                second_release.set()
                self.close(app)

    def test_resume_review_without_scanner_skips_validated_and_old_pages(self):
        with scan.Session(self.root, "livre", create=True) as session:
            self.capture(session, "red")  # Legacy page has no workflow metadata.
            for _ in range(2):
                page = session.capture(Image.new("RGB", (300, 450)), (300, 300), scan.DEFAULT_SETTINGS)
                session.correct(page["number"], scan.identity_transform())
            session.validate_page(2)
            session.set_phase("review")
        with scan.Session(self.root, "livre") as session, \
             patch.object(scan, "WiaScanner", side_effect=AssertionError("no scanner during review")), \
             patch.object(scan, "detect_transform", side_effect=AssertionError("no reprocessing")):
            app = self.open_app(session)
            try:
                self.assertEqual(app.current["number"], 3)
                app.command("correct")
                app.turn()
                app.cancel()
                self.assertEqual(session.page(3)["transform"]["quarter_turns"], 0)
                app.command("scan")
                self.pump(app)
                with patch.object(scan, "export_pdf", side_effect=OSError("disk full")):
                    app.command("export")
                    self.pump(app)
                    self.assertFalse(app.closed)
                    self.assertIn("disk full", app.status.get())
                app.command("export")
                self.wait_for(app, lambda: app.closed)
                with base.pymupdf.open(session.path / "livre.pdf") as doc:
                    self.assertEqual(doc.page_count, 3)
            finally:
                self.close(app)

    def test_mode_retake_and_completed_book_can_acquire_again(self):
        with scan.Session(self.root, "livre", create=True) as session:
            app = self.open_app(session)
            try:
                app.command("scan")
                app.key(SimpleNamespace(keysym="m"))
                self.pump(app)
                self.assertEqual(session.data["settings"]["color"], "color")
                app.command("mode")
                self.pump(app)
                app.command("scan")
                self.pump(app)
                revision = session.page(2)["qwen"]["revision"]
                app.command("retake")
                self.pump(app)
                self.assertNotEqual(session.page(2)["qwen"]["revision"], revision)
                self.assertEqual(len(session.snapshot()["pages"]), 2)
                self.assertEqual(app.display_image.mode, "L")
                self.assertEqual(app.display_image.getpixel((0, 0)), 90)
                self.wait_for(app, lambda: all(p["qwen"]["processing"] == "done" for p in session.snapshot()["pages"]))
                # Processing events must leave the acquisition preview raw.
                app.poll_events()
                self.assertEqual(app.display_image.size, (300, 450))
                app.command("export")
                self.pump(app)
                for _ in range(2):
                    app.command("scan")
                    self.pump(app)
                app.command("export")
                self.wait_for(app, lambda: app.closed)
            finally:
                self.close(app)
        with scan.Session(self.root, "livre") as session:
            app = self.open_app(session)
            try:
                self.assertEqual(app.phase, "acquisition")
                self.assertIn("niveaux de gris", app.mode_text.get())
                app.command("scan")
                self.pump(app)
                app.command("export")
                self.pump(app)
                self.wait_for(app, lambda: app.current is not None)
                self.assertEqual(app.current["number"], 3)
            finally:
                self.close(app)

    def test_close_during_wia_finishes_durable_save(self):
        started, release = threading.Event(), threading.Event()
        def slow_acquire(scanner):
            started.set()
            if not release.wait(8):
                raise AssertionError("test release timed out")
            return Image.new("L", (300, 450), 123), (300, 300)
        with scan.Session(self.root, "livre", create=True) as session, \
             patch.object(FakeScanner, "acquire", slow_acquire):
            app = self.open_app(session)
            try:
                app.command("scan")
                self.assertTrue(started.wait(5))
                app.command("quit")
                self.assertFalse(app.closed)
                self.assertTrue(app.closing)
                release.set()
                self.wait_for(app, lambda: app.closed)
                self.assertEqual(session.page(1)["qwen"]["processing"], "pending")
                self.assertEqual(scan.load_image(session.path / "raw/0001.png").getpixel((0, 0)), 123)
            finally:
                release.set()
                self.close(app)


class CompatibilityTests(unittest.TestCase):
    def test_lock_excludes_other_script_in_both_directions(self):
        for owner, other in ((scan, 'scan_book'), (original, 'scan_book_qwen')):
            with tempfile.TemporaryDirectory() as directory, owner.Session(directory, 'livre', create=True):
                code = ("import sys; sys.path.insert(0, sys.argv[1]); "
                        f"from {other} import Session; Session(sys.argv[2], 'livre').__enter__()")
                result = subprocess.run([sys.executable, '-c', code, str(ROOT / 'tools'), directory],
                                        capture_output=True, timeout=20)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'ScanError', result.stderr)

    def test_default_root_and_settings_are_identical(self):
        self.assertEqual(scan.DEFAULT_ROOT, original.DEFAULT_ROOT)
        self.assertEqual(scan.DEFAULT_SETTINGS, original.DEFAULT_SETTINGS)

    def test_sessions_work_both_ways_with_manual_correction_and_export(self):
        for first, second in ((scan, original), (original, scan)):
            with self.subTest(first=first.__name__), tempfile.TemporaryDirectory() as directory:
                raw = Image.new("RGB", (300, 400), "navy")
                with patch.object(first, "detect_transform", return_value={
                        "quarter_turns": 2, "angle": 0., "crop": [5, 6, 295, 394],
                        "method": "qwen3-vl", "review": False}):
                    with first.Session(directory, "livre", create=True) as session:
                        session.begin_capture(raw, (300, 300), first.DEFAULT_SETTINGS)
                        session.finish_capture()
                        raw_bytes = (session.path / "raw/0001.png").read_bytes()
                        saved = session.manifest.read_bytes()
                with second.Session(directory, "livre") as session:
                    self.assertEqual(session.manifest.read_bytes(), saved)
                    session.correct(1, dict(quarter_turns=0, angle=0., crop=[10, 10, 280, 380],
                                            method="manual", review=False))
                    self.assertEqual((session.path / "raw/0001.png").read_bytes(), raw_bytes)
                    self.assertTrue(second.export_pdf(session).exists())
                with first.Session(directory, "livre") as session:
                    self.assertEqual(session.page(1)["size"], [270, 370])

    def test_model_failure_does_not_block_durable_capture(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(scan, "detect_half_turn", return_value=180), \
             patch.object(scan, "qwen_prediction", side_effect=RuntimeError("model failure")):
            with scan.Session(directory, "livre", create=True) as session:
                raw = Image.new("L", (200, 300), 200)
                session.begin_capture(raw, (300, 300), scan.DEFAULT_SETTINGS)
                page = session.finish_capture()
                self.assertTrue(page["transform"]["review"])
                self.assertIsNone(page["transform"]["crop"])
                self.assertEqual(scan.load_image(session.path / page["raw"]).tobytes(), raw.tobytes())
                self.assertFalse(session.pending.exists())

    def test_import_devices_export_and_manual_correction_do_not_load_torch(self):
        code = """
import sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import scan_book_qwen as s
s.http.client.HTTPConnection = MockHTTP = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('unexpected Ollama access'))
with s.Session(sys.argv[2], 'livre', create=True) as session:
    from PIL import Image
    with patch.object(s, 'detect_transform', return_value=s.identity_transform()):
        session.begin_capture(Image.new('RGB', (100, 100)), (300, 300), s.DEFAULT_SETTINGS)
        session.finish_capture()
with patch.object(s, 'list_devices'):
    assert s.main(['devices']) == 0
assert s.main(['export', 'livre', '--root', sys.argv[2]]) == 0
with patch.object(s, 'Preview') as preview:
    preview.return_value.run.return_value = 0
    assert s.main(['correct', 'livre', '--root', sys.argv[2], '--page', '1']) == 0
assert 'torch' not in sys.modules
assert 'torchvision' not in sys.modules
assert 'win32com' not in sys.modules
assert 'tkinter' not in sys.modules
"""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-c", code, str(ROOT / "tools"), directory],
                                    capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


def load_tests(loader, tests, pattern):
    for parent in (base.SessionTests, base.WiaTests, base.ModeWorkerTests):
        class Reused(parent):
            def setUp(self):
                super().setUp()
                for mock in (patch.object(scan, "detect_half_turn", return_value=0),
                             patch.object(scan, "qwen_prediction",
                                          return_value={"certain": True, "bbox": [0, 0, 1000, 1000]})):
                    mock.start()
                    self.addCleanup(mock.stop)
        Reused.__name__ = f"Qwen{parent.__name__}"
        tests.addTests(loader.loadTestsFromTestCase(Reused))
    # Standalone correction keeps its original UI; acquisition scenarios above
    # replace the old one-step scan/export GUI tests for this variant only.
    class StandaloneEditor(base.GuiTests):
        def setUp(self):
            super().setUp()
            mocked = patch.object(scan, "detect_transform", return_value=scan.identity_transform())
            mocked.start()
            self.addCleanup(mocked.stop)
    tests.addTest(StandaloneEditor("test_editor_rotation_reset_cancel_and_save"))
    # Real RapidOCR check: rotation code remains byte-for-byte identical.
    tests.addTest(base.OrientationTests("test_real_model_upright_upside_down_and_sideways"))
    tests.addTest(base.OrientationTests("test_consensus_rejects_sparse_weak_and_conflicting_votes"))
    return tests


if __name__ == "__main__":
    scan.configure_utf8()
    unittest.main(verbosity=2)
