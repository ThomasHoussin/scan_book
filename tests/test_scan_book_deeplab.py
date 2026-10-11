# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = ["Pillow>=10.4,<13", "PyMuPDF>=1.27.2,<1.28", "opencv-python>=4.11,<5", "numpy>=2,<3", "rapidocr-onnxruntime==1.4.4"]
# ///
"""Offline tests, including Tk: uv run tests/test_scan_book_deeplab.py."""
import hashlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan = load("variant", ROOT / "tools/scan_book_deeplab.py")
original = load("original", ROOT / "tools/scan_book.py")
base = load("existing_tests", ROOT / "tests/test_scan_book.py")
# Reuse transaction, acquisition and UI scenarios against the copied script.
# Existing subprocess imports of scan_book also exercise interoperability.
base.scan = scan
base.SOURCE = ROOT / "tools/scan_book_deeplab.py"


class CropTests(unittest.TestCase):
    def test_margin_maps_outward_with_independent_dpi_and_scales(self):
        p = np.zeros((100, 100), dtype=np.float32)
        p[10:90, 20:80] = .99
        crop, warning = scan.crop_from_probabilities(p, (1000, 2000), (254, 508))
        self.assertEqual(crop, [195, 190, 805, 1810])
        self.assertIsNone(warning)

    def test_all_edges_and_full_frame_are_valid(self):
        for bounds in ((0, 10, 80, 90), (20, 10, 100, 90),
                       (10, 0, 90, 80), (10, 20, 90, 100), (0, 0, 100, 100)):
            with self.subTest(bounds=bounds):
                x0, y0, x1, y1 = bounds
                p = np.zeros((100, 100), dtype=np.float32)
                p[y0:y1, x0:x1] = .99
                crop, warning = scan.crop_from_probabilities(p, (1000, 1000), (254, 254))
                self.assertEqual(crop, [max(0, x0 * 10 - 5), max(0, y0 * 10 - 5),
                                        min(1000, x1 * 10 + 5), min(1000, y1 * 10 + 5)])
                self.assertIsNone(warning)

    def test_large_interior_illustration_hole_is_retained(self):
        p = np.zeros((100, 100), dtype=np.float32)
        p[10:90, 10:90] = .99
        p[20:80, 20:80] = 0
        crop, _ = scan.crop_from_probabilities(p, (1000, 1000), (254, 254))
        self.assertEqual(crop, [95, 95, 905, 905])

    def test_uncertain_empty_small_fragmented_and_invalid_maps_do_not_crop(self):
        small = np.zeros((100, 100)); small[30:50, 30:50] = .99
        fragmented = np.zeros((100, 100))
        fragmented[10:90, 5:45] = .99
        fragmented[10:90, 55:95] = .99
        for p in (np.zeros((100, 100)), np.full((100, 100), .6),
                  small, fragmented, np.full((100, 100), np.nan),
                  np.full((100, 100), 2), np.zeros((0, 0)), np.zeros((3,))):
            with self.subTest(shape=p.shape):
                crop, reason = scan.crop_from_probabilities(p, (1000, 1000), (300, 300))
                self.assertIsNone(crop)
                self.assertTrue(reason)

    def test_half_turn_and_angle_use_exact_saved_coordinate_system(self):
        rng = np.random.default_rng(0)
        raw = Image.fromarray(rng.integers(0, 256, (250, 180, 3), dtype=np.uint8))
        old = dict(quarter_turns=0, angle=2., crop=[0, 0, 10, 10], method="paper", review=False)
        expected, dpi = scan.apply_transform(raw, (300, 300),
                                             dict(quarter_turns=2, angle=2., crop=None))
        def predict(image):
            self.assertEqual(image.tobytes(), expected.tobytes())
            p = np.zeros((100, 100)); p[5:95, 10:90] = .99
            return p, "cuda"
        with patch.object(scan, "detect_half_turn", return_value=180), \
             patch.object(scan, "detect_geometry", return_value=old), \
             patch.object(scan, "crop_cuts_text", return_value=False), \
             patch.object(scan, "deeplab_probabilities", side_effect=predict):
            transform = scan.detect_transform(raw, (300, 300))
        result, result_dpi = scan.apply_transform(raw, (300, 300), transform)
        self.assertEqual(result.tobytes(), expected.crop(transform["crop"]).tobytes())
        self.assertEqual(result_dpi, dpi)
        self.assertEqual(transform["quarter_turns"], 2)
        self.assertEqual(transform["method"], "deeplabv3")
        self.assertNotEqual(transform["crop"], [0, 0, 10, 10])

    def test_grayscale_anisotropic_dpi_and_original_pixels_preserved(self):
        raw = Image.fromarray(np.arange(200 * 100, dtype=np.uint8).reshape(200, 100))
        p = np.zeros((100, 100)); p[10:90, 10:90] = .99
        with patch.object(scan, "detect_half_turn", return_value=0), \
             patch.object(scan, "crop_cuts_text", return_value=False), \
             patch.object(scan, "deeplab_probabilities", return_value=(p, "cpu")):
            transform = scan.detect_transform(raw, (150, 300))
        output, dpi = scan.apply_transform(raw, (150, 300), transform)
        self.assertEqual(output.mode, "L")
        self.assertEqual(dpi, (150, 300))
        self.assertEqual(output.tobytes(), raw.crop(transform["crop"]).tobytes())

    def test_model_error_keeps_rotation_and_discards_old_crop(self):
        old = dict(quarter_turns=0, angle=1., crop=[5, 5, 90, 90],
                   method="paper", review=False)
        with patch.object(scan, "detect_half_turn", return_value=180), \
             patch.object(scan, "detect_geometry", return_value=old), \
             patch.object(scan, "deeplab_probabilities", side_effect=RuntimeError("GPU unavailable")):
            transform = scan.detect_transform(Image.new("RGB", (100, 120)), (300, 300))
        self.assertEqual(transform["quarter_turns"], 2)
        self.assertEqual(transform["angle"], 1.)
        self.assertIsNone(transform["crop"])
        self.assertTrue(transform["review"])
        self.assertEqual(transform["crop_error"], "GPU unavailable")

    def test_text_veto_includes_vertical_labels_and_outside_columns(self):
        image = Image.new("RGB", (1000, 2000))
        # Coordinates on the 800 x 1600 preview.
        for box in ([[20, 100], [150, 100], [150, 120], [20, 120]],
                    [[700, 100], [715, 100], [715, 300], [700, 300]]):
            with patch.object(scan, "orientation_engine", return_value=Mock(return_value=([box], None))):
                self.assertTrue(scan.crop_cuts_text(image, [100, 0, 850, 2000]))
                self.assertFalse(scan.crop_cuts_text(image, [0, 0, 1000, 2000]))

    def test_unsafe_model_crop_is_rejected_instead_of_trimming_content(self):
        p = np.zeros((100, 100)); p[10:90, 10:90] = .99
        with patch.object(scan, "detect_half_turn", return_value=0), \
             patch.object(scan, "deeplab_probabilities", return_value=(p, "cuda")), \
             patch.object(scan, "crop_cuts_text", return_value=True):
            transform = scan.detect_transform(Image.new("RGB", (1000, 1000)), (300, 300))
        self.assertIsNone(transform["crop"])
        self.assertTrue(transform["review"])
        self.assertIn("texte", transform["crop_warning"])


class DownloadTests(unittest.TestCase):
    def test_download_is_atomic_verified_and_reused_offline(self):
        data = b"test model"
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(scan, "DEEPLAB_CACHE", Path(directory)), \
             patch.object(scan, "DEEPLAB_SHA256", hashlib.sha256(data).hexdigest()):
            with patch.object(scan.urllib.request, "urlopen", return_value=io.BytesIO(data)) as urlopen:
                path = scan.deeplab_checkpoint()
                self.assertEqual(path.read_bytes(), data)
                urlopen.assert_called_once()
            with patch.object(scan.urllib.request, "urlopen", side_effect=AssertionError("offline")):
                self.assertEqual(scan.deeplab_checkpoint(), path)
            self.assertEqual(list(Path(directory).glob("*.part")), [])

    def test_bad_download_never_becomes_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(scan, "DEEPLAB_CACHE", Path(directory)), \
             patch.object(scan.urllib.request, "urlopen", return_value=io.BytesIO(b"HTML error")):
            with self.assertRaises(scan.ScanError):
                scan.deeplab_checkpoint()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_corrupt_cache_is_rejected_without_loading(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(scan, "DEEPLAB_CACHE", Path(directory)):
            (Path(directory) / scan.DEEPLAB_NAME).write_bytes(b"broken")
            with self.assertRaises(scan.ScanError):
                scan.deeplab_checkpoint()


class CompatibilityTests(unittest.TestCase):
    def test_default_root_and_settings_are_identical(self):
        self.assertEqual(scan.DEFAULT_ROOT, original.DEFAULT_ROOT)
        self.assertEqual(scan.DEFAULT_SETTINGS, original.DEFAULT_SETTINGS)

    def test_sessions_work_both_ways_with_manual_correction_and_export(self):
        for first, second in ((scan, original), (original, scan)):
            with self.subTest(first=first.__name__), tempfile.TemporaryDirectory() as directory:
                raw = Image.new("RGB", (300, 400), "navy")
                with patch.object(first, "detect_transform", return_value={
                        "quarter_turns": 2, "angle": 0., "crop": [5, 6, 295, 394],
                        "method": "deeplabv3", "review": False}):
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
             patch.object(scan, "deeplab_probabilities", side_effect=RuntimeError("model failure")):
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
import scan_book_deeplab as s
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
    for parent in (base.SessionTests, base.WiaTests, base.ModeWorkerTests, base.GuiTests):
        class Reused(parent):
            def setUp(self):
                super().setUp()
                for mock in (patch.object(scan, "detect_half_turn", return_value=0),
                             patch.object(scan, "deeplab_probabilities",
                                          return_value=(np.full((100, 100), .99), "cpu"))):
                    mock.start()
                    self.addCleanup(mock.stop)
        Reused.__name__ = f"DeepLab{parent.__name__}"
        tests.addTests(loader.loadTestsFromTestCase(Reused))
    # Real RapidOCR check: rotation code remains byte-for-byte identical.
    tests.addTest(base.OrientationTests("test_real_model_upright_upside_down_and_sideways"))
    tests.addTest(base.OrientationTests("test_consensus_rejects_sparse_weak_and_conflicting_votes"))
    return tests


if __name__ == "__main__":
    scan.configure_utf8()
    unittest.main(verbosity=2)
