# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = ["Pillow>=10.4,<13", "PyMuPDF>=1.27.2,<1.28", "opencv-python>=4.11,<5", "numpy>=2,<3", "rapidocr-onnxruntime==1.4.4"]
# ///
"""Tests hors matériel : uv run tests/test_scan_book.py (Tk inclus)."""
import copy
import gc
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image, ImageDraw
import pymupdf

SOURCE = Path(__file__).resolve().parents[1] / "tools" / "scan_book.py"
spec = importlib.util.spec_from_file_location("scan_book", SOURCE)
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)


class Crash(BaseException):
    pass


class SessionFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, session, color="white", size=(300, 450), dpi=(300, 300), replace=None):
        session.begin_capture(Image.new("RGB", size, color), dpi, scan.DEFAULT_SETTINGS, replace)
        return session.finish_capture()


class SessionTests(SessionFixture):

    def test_append_resume_and_replace_only_one_page(self):
        with scan.Session(self.root, "livre", create=True) as session:
            self.capture(session, "red")
            self.capture(session, "blue")
            old_second = (session.path / "pages/0002.png").read_bytes()
            self.capture(session, "green", replace=1)
            self.assertEqual(old_second, (session.path / "pages/0002.png").read_bytes())
            self.assertEqual([p["number"] for p in session.data["pages"]], [1, 2])
        with scan.Session(self.root, "livre") as session:
            self.capture(session, "yellow")
            self.assertEqual([p["number"] for p in session.data["pages"]], [1, 2, 3])
            self.assertEqual(scan.load_image(session.path / "raw/0001.png").getpixel((0, 0)), (0, 128, 0))

    def test_raw_capture_recovered_without_scanner(self):
        with scan.Session(self.root, "livre", create=True) as session:
            session.begin_capture(Image.new("RGB", (240, 320), "blue"), (200, 200), scan.DEFAULT_SETTINGS)
        with scan.Session(self.root, "livre") as session:
            self.assertEqual(len(session.data["pages"]), 1)
            self.assertFalse(session.pending.exists())
            self.assertEqual(session.page(1)["dpi"], [200., 200.])

    def test_crash_after_image_install_before_manifest_recovers_commit(self):
        with scan.Session(self.root, "livre", create=True) as session:
            self.capture(session, "red")
            original = scan.atomic_json
            def crash(path, value):
                if Path(path) == session.manifest:
                    raise Crash()
                original(path, value)
            session.begin_capture(Image.new("RGB", (160, 220), "blue"), (150, 150), scan.DEFAULT_SETTINGS, replace=1)
            with patch.object(scan, "atomic_json", side_effect=crash):
                with self.assertRaises(Crash):
                    session.finish_capture()
        with scan.Session(self.root, "livre") as session:
            self.assertEqual(session.page(1)["size"], [160, 220])
            self.assertEqual(session.page(1)["dpi"], [150., 150.])
            self.assertEqual(scan.load_image(session.path / "pages/0001.png").getpixel((0, 0)), (0, 0, 255))

    def test_failed_replacement_rolls_back_and_keeps_new_raw(self):
        with scan.Session(self.root, "livre", create=True) as session:
            self.capture(session, "red")
            before = copy.deepcopy(session.data)
            old_raw = (session.path / "raw/0001.png").read_bytes()
            old_page = (session.path / "pages/0001.png").read_bytes()
            original = scan.atomic_copy
            failed = False
            def fail_once(source, target):
                nonlocal failed
                if Path(target) == session.path / "pages/0001.png" and not failed:
                    failed = True
                    raise OSError("disque simulé indisponible")
                original(source, target)
            session.begin_capture(Image.new("RGB", (200, 200), "blue"), (300, 300), scan.DEFAULT_SETTINGS, replace=1)
            with patch.object(scan, "atomic_copy", side_effect=fail_once):
                with self.assertRaises(OSError):
                    session.finish_capture()
            self.assertEqual(session.data, before)
            self.assertEqual((session.path / "raw/0001.png").read_bytes(), old_raw)
            self.assertEqual((session.path / "pages/0001.png").read_bytes(), old_page)
            self.assertEqual(len(list((session.path / "recovery").glob("*/raw.png"))), 1)

    def test_failed_processing_retains_raw_and_previous_page(self):
        with scan.Session(self.root, "livre", create=True) as session:
            self.capture(session, "red")
            old = (session.path / "pages/0001.png").read_bytes()
            session.begin_capture(Image.new("RGB", (200, 200), "blue"), (300, 300), scan.DEFAULT_SETTINGS, replace=1)
            with patch.object(scan, "detect_transform", side_effect=RuntimeError("traitement interrompu")):
                with self.assertRaises(RuntimeError):
                    session.finish_capture()
            self.assertEqual(old, (session.path / "pages/0001.png").read_bytes())
            self.assertTrue((session.pending / "raw.png").exists())
        with scan.Session(self.root, "livre") as session:
            self.assertEqual(session.page(1)["size"], [200, 200])

    def test_cleanup_failure_is_not_a_partial_pending_transaction(self):
        with scan.Session(self.root, "livre", create=True) as session:
            with patch.object(scan.shutil, "rmtree"):
                self.capture(session)
            self.assertFalse(session.pending.exists())
        with scan.Session(self.root, "livre") as session:
            self.assertEqual(len(session.data["pages"]), 1)

    def test_concurrent_process_is_rejected(self):
        code = "import sys; sys.path.insert(0, sys.argv[1]); from scan_book import Session; s=Session(sys.argv[2], 'livre'); s.__enter__()"
        with scan.Session(self.root, "livre", create=True):
            result = subprocess.run([sys.executable, "-c", code, str(SOURCE.parent), str(self.root)],
                                    capture_output=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"ScanError", result.stderr)

    def test_correction_preserves_raw_and_other_pages(self):
        with scan.Session(self.root, "livre", create=True) as session:
            self.capture(session, "red")
            self.capture(session, "blue")
            raw = (session.path / "raw/0001.png").read_bytes()
            other = copy.deepcopy(session.page(2))
            transform = {"quarter_turns": 1, "angle": 0, "crop": [10, 20, 400, 280], "method": "manual", "review": False}
            session.correct(1, transform)
            self.assertEqual(session.page(1)["size"], [390, 260])
            self.assertEqual(raw, (session.path / "raw/0001.png").read_bytes())
            self.assertEqual(other, session.page(2))

    def test_invalid_correction_keeps_original(self):
        with scan.Session(self.root, "livre", create=True) as session:
            self.capture(session)
            before = copy.deepcopy(session.data)
            with self.assertRaises(scan.ScanError):
                session.correct(1, {"crop": [-1, 0, 20, 20]})
            self.assertEqual(before, session.data)
            self.assertFalse(session.pending.exists())

    def test_pdf_uses_manifest_order_and_each_physical_resolution(self):
        with scan.Session(self.root, "livre", create=True) as session:
            self.capture(session, "red", (600, 300), (300, 150))
            self.capture(session, "blue", (300, 600), (150, 300))
            session.data["pages"].reverse()
            scan.atomic_json(session.manifest, session.data)
            target = scan.export_pdf(session)
            with pymupdf.open(target) as doc:
                self.assertEqual(doc.page_count, 2)
                for page in doc:
                    self.assertAlmostEqual(page.rect.width, 144, places=3)
                    self.assertAlmostEqual(page.rect.height, 144, places=3)
                self.assertEqual(doc[0].get_pixmap().pixel(20, 20), (0, 0, 255))
                self.assertEqual(doc[1].get_pixmap().pixel(20, 20), (255, 0, 0))

    def test_export_missing_image_keeps_existing_pdf(self):
        with scan.Session(self.root, "livre", create=True) as session:
            self.capture(session)
            target = scan.export_pdf(session)
            before = target.read_bytes()
            (session.path / "pages/0001.png").unlink()
            with self.assertRaises(scan.ScanError):
                scan.export_pdf(session)
            self.assertEqual(before, target.read_bytes())

    def test_empty_export_rejected(self):
        with scan.Session(self.root, "livre", create=True) as session:
            with self.assertRaises(scan.ScanError):
                scan.export_pdf(session)

    def test_export_rejects_unrecovered_transaction(self):
        with scan.Session(self.root, "livre", create=True) as session:
            self.capture(session)
            target = scan.export_pdf(session)
            before = target.read_bytes()
            session.begin_capture(Image.new("RGB", (100, 100), "blue"), (300, 300), scan.DEFAULT_SETTINGS)
            with self.assertRaises(scan.ScanError):
                scan.export_pdf(session)
            self.assertEqual(before, target.read_bytes())

    def test_export_subprocess_does_not_load_wia_or_tk(self):
        with scan.Session(self.root, "livre", create=True) as session:
            self.capture(session)
        code = """import sys
sys.path.insert(0, sys.argv[1])
import scan_book
assert scan_book.main(['export', 'livre', '--root', sys.argv[2]]) == 0
assert 'win32com' not in sys.modules
assert 'tkinter' not in sys.modules
"""
        result = subprocess.run([sys.executable, "-c", code, str(SOURCE.parent), str(self.root)],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_manifest_rejects_paths_outside_session(self):
        with scan.Session(self.root, "livre", create=True) as session:
            self.capture(session)
            session.data["pages"][0]["image"] = "../outside.png"
            scan.atomic_json(session.manifest, session.data)
        with self.assertRaises(scan.ScanError):
            with scan.Session(self.root, "livre"):
                pass

    def test_explicit_options_override_saved_settings_only(self):
        with scan.Session(self.root, "livre", create=True) as session:
            session.save_settings({"dpi": 600, "color": "gray", "area": [0, 0, 50, 100], "scanner": "old"})
        with patch.object(scan, "Preview") as preview:
            preview.return_value.run.return_value = 0
            scan.main(["scan", "livre", "--root", str(self.root), "--dpi", "300", "--area", "full"])
            settings = preview.call_args.args[1]
            self.assertEqual(settings, {"dpi": 300, "color": "gray", "area": None, "scanner": "old"})

    def test_utf8_filename_manifest_and_redirected_console(self):
        book = "été-à-la-fenêtre"
        with scan.Session(self.root, book, create=True) as session:
            self.capture(session)
            self.assertIn(book.encode("utf-8"), session.manifest.read_bytes())
        result = subprocess.run([sys.executable, str(SOURCE), "export", book, "--root", str(self.root)],
                                capture_output=True, timeout=30,
                                env={**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(book, result.stdout.decode("utf-8"))
        self.assertTrue((self.root / book / f"{book}.pdf").is_file())


class ImageTests(unittest.TestCase):
    def setUp(self):
        # Geometry fixtures use colored shapes and bars, not readable text.
        orientation = patch.object(scan, "detect_half_turn", return_value=None)
        orientation.start()
        self.addCleanup(orientation.stop)

    def page(self, angle=0, illustrated=False):
        image = Image.new("RGB", (700, 1000), (30, 30, 30))
        draw = ImageDraw.Draw(image)
        draw.rectangle((110, 120, 590, 880), fill="white")
        if illustrated:
            draw.rectangle((170, 210, 520, 460), fill="red")
            draw.ellipse((240, 250, 480, 420), fill="blue")
        for y in range(500, 820, 25):
            draw.rectangle((155, y, 540, y + 3), fill="black")
        # Distinct colored corner markers must survive crop and deskew.
        for x, y in ((120, 130), (570, 130), (120, 860), (570, 860)):
            draw.rectangle((x, y, x + 8, y + 8), fill=(0, 220, 0))
        return image.rotate(angle, Image.Resampling.BICUBIC, fillcolor=(30, 30, 30))

    def test_known_tilts_and_illustrations_preserve_content(self):
        for tilt in (-5, -2, 0, 2, 5):
            for illustrated in (False, True):
                with self.subTest(tilt=tilt, illustrated=illustrated):
                    raw = self.page(tilt, illustrated)
                    transform = scan.detect_transform(raw, (300, 300))
                    self.assertEqual(transform["method"], "paper")
                    self.assertAlmostEqual(transform["angle"], -tilt, delta=.25)
                    processed, dpi = scan.apply_transform(raw, (300, 300), transform)
                    self.assertEqual(dpi, (300, 300))
                    self.assertLess(processed.width, raw.width)
                    self.assertGreater(processed.width, 480)
                    pixels = np.asarray(processed)
                    green = (pixels[:, :, 1] > 170) & (pixels[:, :, 0] < 60) & (pixels[:, :, 2] < 60)
                    self.assertGreater(green.sum(), 180)

    def test_blank_dark_and_low_contrast_pages_stay_unchanged(self):
        for color in ("white", "black", (235, 235, 230)):
            raw = Image.new("RGB", (700, 1000), color)
            transform = scan.detect_transform(raw, (300, 300))
            self.assertEqual(transform, scan.identity_transform())
            result, _ = scan.apply_transform(raw, (300, 300), transform)
            self.assertEqual(result.tobytes(), raw.tobytes())

    def test_uncertain_boundaries_do_not_remove_margins(self):
        raw = Image.new("RGB", (600, 900), "white")
        draw = ImageDraw.Draw(raw)
        draw.ellipse((40, 60, 500, 800), fill="purple")
        transform = scan.detect_transform(raw, (300, 300))
        self.assertIsNone(transform["crop"])
        self.assertTrue(transform["review"])

    def test_text_deskew_on_white_background(self):
        raw = Image.new("RGB", (700, 1000), "white")
        draw = ImageDraw.Draw(raw)
        for y in range(130, 850, 30):
            for x in range(90, 550, 120):
                draw.rectangle((x, y, x + 100, y + 3), fill="black")
        raw = raw.rotate(3, Image.Resampling.BICUBIC, fillcolor="white")
        transform = scan.detect_transform(raw, (300, 300))
        self.assertEqual(transform["method"], "text-lines")
        self.assertAlmostEqual(transform["angle"], -3, delta=.3)
        self.assertIsNone(transform["crop"])

    def test_quarter_turn_swaps_non_square_dpi(self):
        image, dpi = scan.apply_transform(Image.new("RGB", (600, 300)), (300, 150), {"quarter_turns": 1})
        self.assertEqual(image.size, (300, 600))
        self.assertEqual(dpi, (150, 300))

    def test_arbitrary_angle_preserves_anisotropic_physical_scale(self):
        image, dpi = scan.apply_transform(Image.new("RGB", (600, 300)), (300, 150), {"angle": 2})
        self.assertEqual(dpi, (300, 300))
        self.assertEqual(image.width, image.height)

    def test_invalid_angles_and_dpi_are_rejected(self):
        for angle in (16, -16, float("nan")):
            with self.assertRaises(scan.ScanError):
                scan.apply_transform(Image.new("RGB", (100, 100)), (300, 300), {"angle": angle})
        for dpi in ((0, 300), (float("nan"), 300), (300,)):
            with self.assertRaises(scan.ScanError):
                scan.check_dpi(dpi)


class DarkPageTests(unittest.TestCase):
    def sheet(self):
        image = Image.new("RGB", (800, 1100), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((240, 210, 799, 1099), fill=(65, 65, 65))
        for y in range(320, 960, 35):
            draw.rectangle((320, y, 720, y + 3), fill="white")
        # Narrow scanner rails must not conceal the exposed backing.
        draw.rectangle((790, 0, 799, 1099), fill="black")
        draw.rectangle((0, 1095, 799, 1099), fill="black")
        return image

    def test_dark_sheet_at_each_corner_preserves_full_page(self):
        for turns in range(4):
            with self.subTest(turns=turns):
                image = self.sheet().rotate(turns * 90, expand=True)
                with patch.object(scan, "detect_half_turn", return_value=0):
                    transform = scan.detect_transform(image, (300, 300))
                self.assertEqual(transform["method"], "dark-page")
                self.assertTrue(transform["review"])
                result, _ = scan.apply_transform(image, (300, 300), transform)
                upright = result.rotate(-turns * 90, expand=True)
                self.assertEqual(upright.size, (596, 926))
                self.assertEqual(upright.crop((36, 36, 596, 926)).tobytes(),
                                 self.sheet().crop((240, 210, 800, 1100)).tobytes())

    def test_illustrations_blank_low_contrast_and_outside_text_are_rejected(self):
        images = [Image.new("RGB", (800, 1100), color)
                  for color in ("black", "white", (235, 235, 230))]
        interior = Image.new("RGB", (800, 1100), "white")
        ImageDraw.Draw(interior).rectangle((100, 100, 700, 1000), fill="black")
        images.append(interior)
        ellipse = Image.new("RGB", (800, 1100), "white")
        ImageDraw.Draw(ellipse).ellipse((200, 200, 950, 1250), fill="black")
        images.append(ellipse)
        printed = self.sheet()
        draw = ImageDraw.Draw(printed)
        for y in range(30, 170, 15):
            draw.rectangle((80, y, 700, y + 4), fill="black")
        images.append(printed)
        low_contrast = Image.new("RGB", (800, 1100), "white")
        ImageDraw.Draw(low_contrast).rectangle((240, 210, 799, 1099), fill=(200, 200, 200))
        images.append(low_contrast)
        for i, image in enumerate(images):
            with self.subTest(case=i):
                self.assertIsNone(scan.detect_dark_page_crop(image, (300, 300)))

    def test_half_turn_uses_oriented_crop_coordinates(self):
        upright = self.sheet()
        with patch.object(scan, "detect_half_turn", return_value=180):
            raw = upright.transpose(Image.Transpose.ROTATE_180)
            transform = scan.detect_transform(raw, (300, 300))
        actual, _ = scan.apply_transform(raw, (300, 300), transform)
        self.assertEqual(transform["quarter_turns"], 2)
        self.assertEqual(actual.tobytes(), upright.crop((204, 174, 800, 1100)).tobytes())


class EdgeCropTests(unittest.TestCase):
    def sheet(self, sides):
        image = Image.new("RGB", (800, 1000), "white")
        draw = ImageDraw.Draw(image)
        # Preserve a distinct piece of printed content inside every boundary.
        draw.rectangle((140, 150, 670, 850), outline="black", width=3)
        bounds = [0, 0, 800, 1000]
        for side in sides:
            if side == "left":
                draw.rectangle((38, 0, 44, 999), fill=(205, 205, 205))
                bounds[0] = 45
            elif side == "right":
                draw.rectangle((752, 0, 758, 999), fill=(205, 205, 205))
                bounds[2] = 752
            elif side == "top":
                draw.rectangle((0, 58, 799, 64), fill=(45, 45, 45))
                bounds[1] = 65
            elif side == "bottom":
                draw.rectangle((0, 932, 799, 938), fill=(45, 45, 45))
                bounds[3] = 932
        return image, bounds

    def test_each_side_and_combinations_are_independent(self):
        # All 15 nonempty combinations, including bottom/right and all sides.
        names = ["left", "top", "right", "bottom"]
        for mask in range(1, 16):
            sides = [name for i, name in enumerate(names) if mask & (1 << i)]
            with self.subTest(sides=sides):
                image, expected = self.sheet(sides)
                crop = scan.detect_edge_crop(image)
                self.assertIsNotNone(crop)
                for actual, target in zip(crop, expected):
                    self.assertAlmostEqual(actual, target, delta=2)
                self.assertTrue(crop[0] < 140 < 670 < crop[2])
                self.assertTrue(crop[1] < 150 < 850 < crop[3])

    def test_no_border_or_interior_frame_does_not_crop(self):
        for image in (Image.new("RGB", (800, 1000), "white"), self.sheet([])[0]):
            self.assertIsNone(scan.detect_edge_crop(image))

    def test_printed_rule_with_text_outside_is_preserved(self):
        image = Image.new("RGB", (800, 1000), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 900, 799, 904), fill="black")
        for x in range(100, 700, 30):
            draw.rectangle((x, 945, x + 15, 960), fill="black")
        self.assertIsNone(scan.detect_edge_crop(image))


class OrientationTests(SessionFixture):
    def text_page(self):
        # Built-in PDF fonts make this real model test portable without fixtures.
        with pymupdf.open() as doc:
            page = doc.new_page(width=450, height=600)
            for i, line in enumerate([
                "Le soleil se leve sur la petite ville.",
                "Les enfants regardent les oiseaux voler.",
                "Nous ouvrons le livre pour lire une histoire.",
                "Chaque page raconte un nouveau voyage.",
                "The quick brown fox jumps over the lazy dog.",
                "Please keep this page facing the right way.",
            ]):
                page.insert_text((40, 90 + i * 35), line, fontsize=14, fontname="tiro")
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
            return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)

    def test_real_model_upright_upside_down_and_sideways(self):
        page = self.text_page()
        self.assertEqual(scan.detect_half_turn(page), 0)
        self.assertEqual(scan.detect_half_turn(page.transpose(Image.Transpose.ROTATE_180)), 180)
        self.assertIsNone(scan.detect_half_turn(page.transpose(Image.Transpose.ROTATE_90)))

    def test_consensus_rejects_sparse_weak_and_conflicting_votes(self):
        for results in ([], [("180", .99)] * 2, [("180", .8)] * 8,
                        [("180", .99)] * 3 + [("0", .99)] * 2,
                        [("180", .99)] * 3 + [("180", .6)] * 4):
            self.assertIsNone(scan.orientation_consensus(results))
        self.assertEqual(scan.orientation_consensus([("180", .99)] * 4 + [("0", .99)]), 180)
        self.assertEqual(scan.orientation_consensus([("0", .99)] * 4), 0)

    def test_capture_rotates_processed_page_and_preserves_raw(self):
        upright = self.text_page()
        raw = upright.transpose(Image.Transpose.ROTATE_180)
        with scan.Session(self.root, "livre", create=True) as session:
            session.begin_capture(raw, (300, 300), scan.DEFAULT_SETTINGS)
            result = session.finish_capture()
            self.assertEqual(result["transform"]["quarter_turns"], 2)
            self.assertEqual(scan.load_image(session.path / result["raw"]).tobytes(), raw.tobytes())
            self.assertEqual(scan.load_image(session.path / result["image"]).tobytes(), upright.tobytes())

    def test_half_turn_preserves_deskew_and_crop_coordinate_system(self):
        upright = ImageTests().page(3, illustrated=True)
        raw = upright.transpose(Image.Transpose.ROTATE_180)
        expected_transform = scan.detect_geometry(upright, (300, 300))
        self.assertIsNotNone(expected_transform["crop"])
        with patch.object(scan, "detect_half_turn", return_value=180):
            transform = scan.detect_transform(raw, (300, 300))
        expected, _ = scan.apply_transform(upright, (300, 300), expected_transform)
        actual, _ = scan.apply_transform(raw, (300, 300), transform)
        self.assertEqual(actual.tobytes(), expected.tobytes())

    def test_model_failure_keeps_capture_and_marks_for_review(self):
        with scan.Session(self.root, "livre", create=True) as session:
            with patch.object(scan, "detect_half_turn", side_effect=RuntimeError("model unavailable")):
                result = self.capture(session)
            self.assertEqual(result["transform"]["quarter_turns"], 0)
            self.assertTrue(result["transform"]["review"])
            self.assertIn("model unavailable", result["transform"]["orientation_error"])
            self.assertTrue((session.path / result["raw"]).is_file())


class FakeProperty:
    def __init__(self, value, kind=0, readonly=False, values=(), minimum=0, maximum=10000, step=1):
        self.Value, self.SubType, self.IsReadOnly = value, kind, readonly
        self.Name = "fake"
        self.SubTypeValues = values
        self.SubTypeMin, self.SubTypeMax, self.SubTypeStep = minimum, maximum, step


class WiaTests(unittest.TestCase):
    def test_rejects_unsupported_resolution_and_readonly(self):
        prop = FakeProperty(300, kind=2, values=(150, 300, 600))
        with self.assertRaises(scan.ScanError):
            scan.set_wia_property({6147: prop}, "x_dpi", 400)
        scan.set_wia_property({6147: prop}, "x_dpi", 600)
        self.assertEqual(prop.Value, 600)
        prop.IsReadOnly = True
        with self.assertRaises(scan.ScanError):
            scan.set_wia_property({6147: prop}, "x_dpi", 300)

    def test_range_step_validation(self):
        prop = FakeProperty(100, kind=1, minimum=100, maximum=600, step=100)
        with self.assertRaises(scan.ScanError):
            scan.set_wia_property({6147: prop}, "x_dpi", 250)
        scan.set_wia_property({6147: prop}, "x_dpi", 300)

    def test_readback_rejects_silent_driver_coercion(self):
        class Coerced(FakeProperty):
            @property
            def Value(self):
                return 300
            @Value.setter
            def Value(self, value):
                pass
        with self.assertRaises(scan.ScanError):
            scan.set_wia_property({6147: Coerced(300)}, "x_dpi", 600)

    def test_area_limits_and_color_verified_before_transfer(self):
        scanner = object.__new__(scan.WiaScanner)
        scanner.id, scanner.label = "fake-id", "Fake scanner"
        scanner.device = type("Device", (), {"Properties": []})()
        scanner.props = {identifier: FakeProperty(0) for identifier in scan.WIA_IDS.values()}
        scanner.props[6147] = FakeProperty(300, kind=2, values=(150, 300, 600))
        scanner.props[6148] = FakeProperty(300, kind=2, values=(150, 300, 600))
        scanner.props[6151] = FakeProperty(2550, kind=1, minimum=1, maximum=2550)
        scanner.props[6152] = FakeProperty(3500, kind=1, minimum=1, maximum=3500)
        scanner.props[4103] = FakeProperty(3)
        settings = {**scan.DEFAULT_SETTINGS, "area": [10, 20, 100, 150]}
        effective = scanner.configure(settings)
        self.assertEqual(effective["x"], round(10 * 300 / 25.4))
        with self.assertRaises(scan.ScanError):
            scanner.configure({**settings, "area": [0, 0, 500, 500]})
        scanner.props[4103].Value = 2
        with self.assertRaises(scan.ScanError):
            scanner.configure(scan.DEFAULT_SETTINGS)


class ModeWorkerTests(SessionFixture):
    def test_failed_change_restores_driver_and_saved_settings(self):
        from contextlib import nullcontext
        for failure in ("driver", "save", "rollback"):
            with self.subTest(failure=failure), scan.Session(self.root, failure, create=True) as session:
                scanner = Mock(id="fake")
                scanner.configure.side_effect = [
                    {"color": "color"},
                    RuntimeError("mode refusé") if failure != "save" else {"color": "gray"},
                    RuntimeError("restauration refusée") if failure == "rollback" else {"color": "color"},
                ]
                worker = scan.ScanWorker(session, copy.deepcopy(scan.DEFAULT_SETTINGS), True)
                with patch.object(scan, "WiaScanner", return_value=scanner), patch.object(scan, "com_context", nullcontext):
                    worker.start()
                    try:
                        worker.jobs.put(("connect", None))
                        self.assertIsNone(worker.results.get(timeout=5)[2])
                        original = copy.deepcopy(session.data["settings"])
                        with patch.object(session, "save_settings", side_effect=OSError("disque indisponible")):
                            worker.jobs.put(("mode", None))
                            action, _, error = worker.results.get(timeout=5)
                        self.assertEqual(action, "fatal" if failure == "rollback" else "mode")
                        self.assertIsNotNone(error)
                        self.assertEqual(session.data["settings"], original)
                        self.assertEqual(worker.settings, original)
                        self.assertEqual(scanner.configure.call_args.args[0], original)
                    finally:
                        worker.jobs.put(None)
                        worker.join(timeout=5)
                self.assertEqual(scan.json.loads(session.manifest.read_text(encoding="utf-8"))["settings"], original)


@unittest.skipIf(os.environ.get("SCAN_BOOK_SKIP_GUI") == "1", "Tk explicitement désactivé")
class GuiTests(SessionFixture):
    def tearDown(self):
        # Dispose Tk reference cycles on the main thread before another worker runs.
        gc.collect()
        super().tearDown()

    def test_editor_rotation_reset_cancel_and_save(self):
        with scan.Session(self.root, "livre", create=True) as session:
            self.capture(session, "red")
            app = scan.Preview(session, scan.DEFAULT_SETTINGS, edit_page=1)
            app.root.withdraw()
            try:
                app.root.update()
                app.turn()
                self.assertEqual(app.display_image.size, (450, 300))
                app.reset()
                self.assertEqual(app.display_image.size, (300, 450))
                app.crop = [20, 30, 200, 350]
                app.zoom.set("100%")
                app.render()
                app.accept()
                deadline = time.monotonic() + 10
                while not app.closed and time.monotonic() < deadline:
                    app.root.update()
                    time.sleep(.03)
                self.assertTrue(app.closed)
                self.assertEqual(session.page(1)["size"], [180, 320])
                self.assertFalse(session.page(1)["transform"]["review"])
            finally:
                if not app.closed:
                    app.busy = False
                    app.shutdown()
            original = copy.deepcopy(session.page(1))
            app = scan.Preview(session, scan.DEFAULT_SETTINGS, edit_page=1)
            app.root.withdraw()
            app.root.update()
            app.turn()
            app.cancel()
            self.assertEqual(session.page(1), original)

    def pump(self, app):
        deadline = time.monotonic() + 10
        while app.busy and not app.closed and time.monotonic() < deadline:
            app.root.update()
            time.sleep(.02)
        self.assertFalse(app.busy)

    def test_mode_shortcut_mixed_pages_and_resume(self):
        from contextlib import nullcontext
        from types import SimpleNamespace

        class FakeScanner:
            id = "fake"
            def __init__(self, selector):
                pass
            def configure(self, settings):
                self.effective = {"name": "Scanner simulé", "x_dpi": 300, "color": settings["color"]}
                return copy.deepcopy(self.effective)
            def acquire(self):
                mode = "RGB" if self.effective["color"] == "color" else "L"
                return Image.new(mode, (300, 450), "white"), (300, 300)

        with patch.object(scan, "WiaScanner", FakeScanner), patch.object(scan, "com_context", nullcontext):
            with scan.Session(self.root, "livre", create=True) as session:
                app = scan.Preview(session, copy.deepcopy(scan.DEFAULT_SETTINGS))
                app.root.withdraw()
                try:
                    self.pump(app)
                    self.assertIn("Couleur", app.mode_text.get())
                    app.command("scan")
                    app.key(SimpleNamespace(keysym="m"))  # Ignored during acquisition.
                    self.pump(app)
                    self.assertEqual(session.data["settings"]["color"], "color")
                    cover = copy.deepcopy(session.page(1))
                    app.key(SimpleNamespace(keysym="M"))
                    self.pump(app)
                    self.assertIn("niveaux de gris", app.mode_text.get())
                    app.command("scan")
                    self.pump(app)
                    self.assertEqual(session.page(1), cover)
                    self.assertEqual(scan.load_image(session.path / "pages/0001.png").mode, "RGB")
                    self.assertEqual(scan.load_image(session.path / "pages/0002.png").mode, "L")
                    app.command("correct")
                    app.key(SimpleNamespace(keysym="m"))
                    self.assertFalse(app.busy)
                    self.assertTrue(app.buttons["mode"].instate(["disabled"]))
                    app.cancel()
                    app.command("quit")
                finally:
                    if not app.closed:
                        self.pump(app)
                        app.shutdown()
                del app
                gc.collect()
            with scan.Session(self.root, "livre") as session:
                app = scan.Preview(session, copy.deepcopy(session.data["settings"]))
                app.root.withdraw()
                try:
                    self.pump(app)
                    self.assertIn("niveaux de gris", app.mode_text.get())
                    app.buttons["mode"].invoke()
                    self.pump(app)
                    self.assertIn("Couleur", app.mode_text.get())
                    self.assertEqual(session.data["settings"]["color"], "color")
                    app.command("export")
                    self.pump(app)
                    with pymupdf.open(session.path / "livre.pdf") as doc:
                        self.assertEqual(doc.page_count, 2)
                        self.assertEqual(pymupdf.Pixmap(doc, doc[0].get_images()[0][0]).colorspace.n, 3)
                        self.assertEqual(pymupdf.Pixmap(doc, doc[1].get_images()[0][0]).colorspace.n, 1)
                finally:
                    if not app.closed:
                        self.pump(app)
                        app.shutdown()

    def test_scan_retake_resume_and_finish_with_fake_hardware(self):
        from contextlib import nullcontext
        class FakeScanner:
            id = "fake"
            effective = {"name": "Scanner simulé", "x_dpi": 300, "color": "color"}
            calls = 0
            def __init__(self, selector):
                pass
            def configure(self, settings):
                return self.effective
            def acquire(self):
                self.calls += 1
                return Image.new("RGB", (300, 450), (self.calls * 30, 0, 0)), (300, 300)
        with patch.object(scan, "WiaScanner", FakeScanner), patch.object(scan, "com_context", nullcontext):
            with scan.Session(self.root, "livre", create=True) as session:
                app = scan.Preview(session, copy.deepcopy(scan.DEFAULT_SETTINGS))
                app.root.withdraw()
                try:
                    self.pump(app)
                    self.assertTrue(app.connected)
                    app.command("scan")
                    app.command("scan")  # Must be ignored while busy.
                    self.pump(app)
                    self.assertEqual(len(session.data["pages"]), 1)
                    app.command("scan")
                    self.pump(app)
                    app.command("retake")
                    self.pump(app)
                    self.assertEqual(len(session.data["pages"]), 2)
                    self.assertEqual(scan.load_image(session.path / "pages/0002.png").getpixel((0, 0)), (90, 0, 0))
                    app.command("quit")
                finally:
                    if not app.closed:
                        app.busy = False
                        app.shutdown()
            with scan.Session(self.root, "livre") as session:
                app = scan.Preview(session, copy.deepcopy(session.data["settings"]))
                app.root.withdraw()
                try:
                    self.pump(app)
                    app.command("export")
                    self.pump(app)
                    self.assertTrue(app.closed)
                    with pymupdf.open(session.path / "livre.pdf") as doc:
                        self.assertEqual(doc.page_count, 2)
                finally:
                    if not app.closed:
                        app.busy = False
                        app.shutdown()


if __name__ == "__main__":
    scan.configure_utf8()
    unittest.main(verbosity=2)
