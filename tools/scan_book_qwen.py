# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = [
#   "pywin32>=311,<312; sys_platform == 'win32'",
#   "Pillow>=10.4,<13",
#   "PyMuPDF>=1.27.2,<1.28",
#   "opencv-python>=4.11,<5",
#   "numpy>=2,<3",
#   "rapidocr-onnxruntime==1.4.4",
# ]
# ///
"""Numérisation de livres : uv run tools/scan_book_qwen.py --help.

WIA et Tkinter sont chargés uniquement par les commandes qui les utilisent.
Le manifeste est la source de vérité ; ne pas modifier une session ouverte.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache, wraps
import base64
import http.client
import time
import io
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import sys
import threading
import uuid

from PIL import Image, ImageDraw

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "scans"
DEFAULT_SETTINGS = {"dpi": 300, "color": "color", "area": None, "scanner": None}
QWEN_MODEL = "qwen3.5:9b"
QWEN_TIMEOUT = 180
QWEN_SCHEMA = {
    "type": "object",
    "properties": {
        "certain": {"type": "boolean"},
        "bbox": {"type": "array", "items": {"type": "number"},
                 "minItems": 4, "maxItems": 4},
    },
    "required": ["certain", "bbox"], "additionalProperties": False,
}
QWEN_PROMPT = """Recadre la page sur les bords du papier, en t'assurant de ne
couper aucun texte ni aucune image, mais en retirant les ombres, les marques de
reliure et les bordures des pages.
Réponds uniquement en JSON : {"certain": true, "bbox": [gauche, haut, droite, bas]}.
Les coordonnées vont de 0 à 1000 relativement à l'image entière.
Si tu ne peux pas déterminer le recadrage, indique certain=false."""
WIA_IDS = {"intent": 6146, "x_dpi": 6147, "y_dpi": 6148,
           "x": 6149, "y": 6150, "width": 6151, "height": 6152}
PNG_FORMAT = "{B96B3CAF-0728-11D3-9D7B-0000F81EF32E}"
PROCESSING_CONTEXT = threading.local()
FLATBED_CATEGORY = "{FB607B1F-43F3-488B-855B-FB703EC342A6}"


class ScanError(Exception):
    """Erreur présentable sans trace technique à l'utilisateur."""


def now():
    return datetime.now(timezone.utc).isoformat()


def configure_utf8():
    """Do not inherit a legacy Windows code page for redirected text streams."""
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def atomic_png(path, image, dpi):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("wb") as stream:
        image.save(stream, format="PNG", dpi=tuple(dpi), compress_level=3)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def atomic_copy(source, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    with Path(source).open("rb") as src, temp.open("wb") as dst:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(temp, target)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScanError(f"Impossible de lire {path} : {exc}") from exc


def load_image(path):
    with Image.open(path) as image:
        image.load()
        return image.convert("L" if image.mode in ("1", "L", "I;16") else "RGB")


def check_dpi(dpi):
    if not isinstance(dpi, (list, tuple)) or len(dpi) != 2:
        raise ScanError("Résolution de page absente ou invalide.")
    if any(not isinstance(v, (float, int)) or not math.isfinite(v) or v <= 0 for v in dpi):
        raise ScanError("La résolution doit contenir deux nombres positifs.")
    return tuple(float(v) for v in dpi)


def safe_child(root, relative):
    candidate = (root / relative).resolve()
    if Path(relative).is_absolute() or not candidate.is_relative_to(root.resolve()):
        raise ScanError(f"Chemin hors de la session : {relative}")
    return candidate


def book_slug(value):
    if (not value or value in (".", "..") or re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
            or value.endswith((".", " "))
            or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value)):
        raise argparse.ArgumentTypeError("Nom de livre invalide : utiliser un seul nom de dossier.")
    return value


def synchronized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self.mutex:
            return method(self, *args, **kwargs)
    return locked


class Session:
    """Verrou exclusif et transactions récupérables, images puis manifeste.

    .pending/capture.json + raw.png : acquisition durable à traiter à la reprise.
    .pending/transaction.json : décision de commit durable, rejouable après crash.
    Les sources restent dans .pending jusqu'au commit du manifeste. En cas
    d'erreur d'E/S interceptée, les sauvegardes permettent un retour à l'état initial.
    """

    def __init__(self, root, book, create=False):
        book_slug(book)
        self.path = Path(root).resolve() / book
        self.book = book
        self.manifest = self.path / "session.json"
        self.pending = self.path / ".pending"
        self.create = create
        self.lock = None
        self.data = None
        self.mutex = threading.RLock()

    def __enter__(self):
        if not self.create and not self.manifest.exists():
            raise ScanError(f"Session introuvable : {self.manifest}")
        self.path.mkdir(parents=True, exist_ok=True)
        self.lock = (self.path / ".session.lock").open("a+b")
        try:
            self.lock.seek(0, os.SEEK_END)
            if self.lock.tell() == 0:
                self.lock.write(b"0")
                self.lock.flush()
            self.lock.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lock.close()
            self.lock = None
            raise ScanError("Cette session est déjà ouverte dans un autre processus.") from exc
        try:
            for name in ("raw", "pages"):
                (self.path / name).mkdir(exist_ok=True)
            if not self.manifest.exists():
                atomic_json(self.manifest, {"version": 1, "book": self.book,
                    "created_at": now(), "settings": copy.deepcopy(DEFAULT_SETTINGS), "pages": []})
            self.data = read_json(self.manifest)
            self.validate()
            self.recover()
            if self.pending.exists():
                raise ScanError("Le journal n'a pas pu être nettoyé. Vérifier les droits et l'espace disque, puis reprendre.")
            self.validate()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.lock:
            self.lock.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_UN)
            self.lock.close()
            self.lock = None

    def validate(self):
        if not isinstance(self.data, dict) or self.data.get("version") != 1:
            raise ScanError("Version de manifeste non prise en charge.")
        if not isinstance(self.data.get("pages"), list) or not isinstance(self.data.get("settings"), dict):
            raise ScanError("Manifeste incomplet.")
        workflow = self.data.get("qwen_workflow", {})
        if not isinstance(workflow, dict) or workflow.get("phase", "acquisition") not in ("acquisition", "review", "complete"):
            raise ScanError("Phase du parcours Qwen invalide.")
        numbers = set()
        try:
            for page in self.data["pages"]:
                number = page["number"]
                if not isinstance(number, int) or number < 1 or number in numbers:
                    raise ScanError("Numéros de pages invalides ou dupliqués.")
                numbers.add(number)
                for key in ("raw", "image"):
                    safe_child(self.path, page[key])
                check_dpi(page["dpi"])
                check_dpi(page["raw_dpi"])
                state = page.get("qwen")
                if state is not None and (not isinstance(state, dict)
                        or not isinstance(state.get("revision"), str) or not state["revision"]
                        or state.get("processing") not in ("pending", "done")
                        or not isinstance(state.get("validated"), bool)
                        or (state["validated"] and state["processing"] != "done")):
                    raise ScanError("État de traitement Qwen invalide.")
        except (KeyError, TypeError) as exc:
            raise ScanError("Entrée de page incomplète dans le manifeste.") from exc

    @synchronized
    def page(self, number):
        for page in self.data["pages"]:
            if page["number"] == number:
                return copy.deepcopy(page)
        raise ScanError(f"Page {number} introuvable.")

    @synchronized
    def snapshot(self):
        return copy.deepcopy(self.data)

    def missing_images(self, page):
        return [page[key] for key in ("raw", "image")
                if not safe_child(self.path, page[key]).exists()]

    def available_pages(self, report=None):
        pages = []
        for page in self.snapshot()["pages"]:
            missing = self.missing_images(page)
            if missing:
                if report:
                    report(f"Page {page['number']:04d} ignorée — image manquante : {', '.join(missing)}")
            else:
                pages.append(page)
        return pages

    @synchronized
    def set_phase(self, phase):
        data = copy.deepcopy(self.data)
        data["qwen_workflow"] = {"phase": phase}
        atomic_json(self.manifest, data)
        self.data = data

    @synchronized
    def validate_page(self, number):
        data = copy.deepcopy(self.data)
        page = next(p for p in data["pages"] if p["number"] == number)
        state = page.get("qwen")
        if state:
            if state["processing"] != "done":
                raise ScanError("Cette page attend encore son traitement.")
            state["validated"] = True
        atomic_json(self.manifest, data)
        self.data = data
        return copy.deepcopy(page)

    @synchronized
    def save_settings(self, settings):
        data = copy.deepcopy(self.data)
        data["settings"] = copy.deepcopy(settings)
        atomic_json(self.manifest, data)
        self.data = data

    @synchronized
    def begin_capture(self, image, dpi, settings, replace=None, deferred=False):
        if self.pending.exists():
            raise ScanError("Une acquisition attend une récupération. Fermer et reprendre la session.")
        number = replace if replace is not None else max(
            (p["number"] for p in self.data["pages"]), default=0) + 1
        if replace is not None:
            self.page(replace)
        check_dpi(dpi)
        self.pending.mkdir()
        atomic_json(self.pending / "capture.json", {"number": number, "dpi": list(dpi),
                    "settings": settings, "acquired_at": now(), "deferred": deferred})
        atomic_png(self.pending / "raw.png", image, dpi)
        return number

    @synchronized
    def finish_capture(self):
        meta = read_json(self.pending / "capture.json")
        raw = load_image(self.pending / "raw.png")
        deferred = meta.get("deferred", False)
        transform = identity_transform() if deferred else detect_transform(raw, meta["dpi"])
        processed, dpi = apply_transform(raw, meta["dpi"], transform)
        number = meta["number"]
        page = {"number": number, "raw": f"raw/{number:04d}.png",
                "image": f"pages/{number:04d}.png", "raw_dpi": meta["dpi"],
                "dpi": list(dpi), "size": list(processed.size), "transform": transform,
                "acquired_at": meta["acquired_at"], "updated_at": now(),
                "settings": meta["settings"]}
        if deferred:
            page["qwen"] = {"revision": uuid.uuid4().hex, "processing": "pending", "validated": False}
        self.commit_page(page, processed, include_raw=True)
        return page

    @synchronized
    def capture(self, image, dpi, settings, replace=None):
        self.begin_capture(image, dpi, settings, replace, deferred=True)
        return self.finish_capture()

    @synchronized
    def processing_input(self, number, revision):
        page = self.page(number)
        state = page.get("qwen", {})
        if (state.get("revision") != revision or state.get("processing") != "pending"
                or self.missing_images(page)):
            return None
        return page, load_image(safe_child(self.path, page["raw"]))

    @synchronized
    def finish_processing(self, number, revision, transform, image, dpi):
        page = self.page(number)
        state = page.get("qwen", {})
        if (state.get("revision") != revision or state.get("processing") != "pending"
                or self.missing_images(page)):
            return None
        if self.pending.exists():
            raise ScanError("Une transaction attend une récupération. Fermer puis reprendre la session.")
        page.update(transform=transform, size=list(image.size), dpi=list(dpi), updated_at=now())
        state["processing"] = "done"
        self.pending.mkdir()
        self.commit_page(page, image, include_raw=False)
        return page

    @synchronized
    def correct(self, number, transform):
        page = copy.deepcopy(self.page(number))
        if self.pending.exists():
            raise ScanError("Une acquisition attend une récupération. Reprendre la session.")
        raw = load_image(safe_child(self.path, page["raw"]))
        processed, dpi = apply_transform(raw, page["raw_dpi"], transform)
        page.update(transform=transform, size=list(processed.size), dpi=list(dpi), updated_at=now())
        if "qwen" in page:
            page["qwen"] = {"revision": uuid.uuid4().hex, "processing": "done", "validated": False}
        self.pending.mkdir()
        phase = "review" if self.data.get("qwen_workflow", {}).get("phase") == "complete" and "qwen" in page else None
        self.commit_page(page, processed, include_raw=False, phase=phase)
        return page

    def commit_page(self, page, image, include_raw, phase=None):
        atomic_png(self.pending / "page.png", image, page["dpi"])
        data = copy.deepcopy(self.data)
        if phase is not None:
            data["qwen_workflow"] = {"phase": phase}
        for index, old in enumerate(data["pages"]):
            if old["number"] == page["number"]:
                data["pages"][index] = page
                break
        else:
            data["pages"].append(page)
        writes = [("page.png", page["image"])]
        if include_raw:
            writes.insert(0, ("raw.png", page["raw"]))
        records = []
        for index, (source, target) in enumerate(writes):
            existing = safe_child(self.path, target)
            backup = f"backup-{index}.png" if existing.exists() else None
            if backup:
                atomic_copy(existing, self.pending / backup)
            records.append({"source": source, "target": target, "backup": backup})
        tx = {"phase": "commit", "before": self.data, "after": data, "writes": records}
        atomic_json(self.pending / "transaction.json", tx)
        try:
            self.install(tx)
        except Exception:
            tx["phase"] = "rollback"
            atomic_json(self.pending / "transaction.json", tx)
            self.rollback(tx)
            raise

    def install(self, tx):
        for entry in tx["writes"]:
            atomic_copy(safe_child(self.pending, entry["source"]),
                        safe_child(self.path, entry["target"]))
        atomic_json(self.manifest, tx["after"])
        self.data = tx["after"]
        # Rename before cleanup: a partial rmtree must never leave an incomplete
        # transaction looking replayable. A failed rename leaves all sources intact.
        garbage = self.path / f".committed-{uuid.uuid4().hex}"
        try:
            os.replace(self.pending, garbage)
        except OSError:
            return
        shutil.rmtree(garbage, ignore_errors=True)

    def rollback(self, tx):
        for entry in tx["writes"]:
            target = safe_child(self.path, entry["target"])
            if entry["backup"]:
                atomic_copy(safe_child(self.pending, entry["backup"]), target)
            else:
                target.unlink(missing_ok=True)
        atomic_json(self.manifest, tx["before"])
        self.data = tx["before"]
        recovery = self.path / "recovery"
        recovery.mkdir(exist_ok=True)
        os.replace(self.pending, recovery / uuid.uuid4().hex)

    def recover(self):
        if not self.pending.exists():
            return
        transaction = self.pending / "transaction.json"
        if transaction.exists():
            tx = read_json(transaction)
            if tx["phase"] == "rollback":
                self.rollback(tx)
            elif tx["phase"] == "commit":
                self.install(tx)
            else:
                raise ScanError("Journal de récupération invalide.")
        elif (self.pending / "capture.json").exists() and (self.pending / "raw.png").exists():
            print("Récupération du scan brut sauvegardé avant l'interruption…", flush=True)
            self.finish_capture()
        else:
            # No complete capture and no durable commit: existing pages are intact.
            shutil.rmtree(self.pending)


def identity_transform(review=True):
    return {"quarter_turns": 0, "angle": 0.0, "crop": None,
            "method": "unchanged", "review": review}


def rotate_image(image, quarter_turns, angle):
    if quarter_turns % 4:
        image = image.transpose({1: Image.Transpose.ROTATE_90,
                                 2: Image.Transpose.ROTATE_180,
                                 3: Image.Transpose.ROTATE_270}[quarter_turns % 4])
    if abs(angle) > 0.001:
        image = image.rotate(angle, Image.Resampling.BICUBIC, expand=True,
                             fillcolor=255 if image.mode == "L" else (255, 255, 255))
    return image


def apply_transform(raw, dpi, transform):
    dpi = check_dpi(dpi)
    turns = int(transform.get("quarter_turns", 0)) % 4
    angle = float(transform.get("angle", 0))
    if not math.isfinite(angle) or abs(angle) > 15:
        raise ScanError("L'angle de redressement doit être compris entre -15 et 15°.")
    # Rotation at non-square pixel resolution would shear physical geometry.
    # Resample to square pixels before an arbitrary-angle rotation.
    if abs(angle) > 0.001 and abs(dpi[0] - dpi[1]) > 0.01:
        resolution = max(dpi)
        raw = raw.resize((round(raw.width * resolution / dpi[0]),
                          round(raw.height * resolution / dpi[1])), Image.Resampling.BICUBIC)
        dpi = (resolution, resolution)
    image = rotate_image(raw, turns, angle)
    if turns % 2:
        dpi = dpi[::-1]
    crop = transform.get("crop")
    if crop is not None:
        if len(crop) != 4:
            raise ScanError("Rectangle de recadrage invalide.")
        left, top, right, bottom = map(int, crop)
        if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
            raise ScanError("Le rectangle de recadrage sort de l'image.")
        image = image.crop((left, top, right, bottom))
    return image, dpi


@lru_cache(maxsize=1)
def orientation_engine():
    # Models are bundled in the wheel; inference stays local. Load once, only
    # when processing a scan (devices/export/manual correction do not need it).
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR(use_rec=False, intra_op_num_threads=2, inter_op_num_threads=1)


def orientation_consensus(results):
    """Require three strong votes, majority coverage, and 80% agreement."""
    confident = [str(label) for label, score in results if float(score) >= .9]
    if len(confident) < 3 or len(confident) < len(results) * .5:
        return None
    for degrees in (0, 180):
        votes = confident.count(str(degrees))
        if votes >= 3 and votes / len(confident) >= .8:
            return degrees
    return None


def detect_half_turn(raw):
    """Classify horizontal text lines as upright/upside-down, without OCR."""
    import cv2
    import numpy as np

    preview = raw.convert("RGB")
    preview.thumbnail((1600, 1600))
    pixels = np.asarray(preview)
    if min(preview.size) < 80 or float(pixels.std()) < 5:
        return None
    engine = orientation_engine()
    pixels = cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR)
    boxes, _ = engine(pixels, use_det=True, use_cls=False, use_rec=False)
    if boxes is None:
        return None
    horizontal = []
    for box in boxes:
        points = np.asarray(box, dtype=np.float32)
        top, side = points[1] - points[0], points[3] - points[0]
        # Reject vertical/short fragments: their classification must not turn
        # a sideways page or a page number into a whole-page 180-degree vote.
        if (np.linalg.norm(top) >= 3 * max(np.linalg.norm(side), 1)
                and abs(float(top[1])) <= .25 * abs(float(top[0]))):
            horizontal.append(points)
    if len(horizontal) < 3:
        return None
    # Bound classification work and sample across the page, not just its title.
    horizontal.sort(key=lambda box: float(box[:, 1].mean()))
    indices = np.linspace(0, len(horizontal) - 1, min(24, len(horizontal)), dtype=int)
    crops = engine.get_crop_img_list(pixels, [horizontal[i] for i in indices])
    _, results, _ = engine.text_cls(crops)
    return orientation_consensus(results)


def detect_transform(raw, dpi):
    check_dpi(dpi)
    orientation_error = None
    try:
        degrees = detect_half_turn(raw)
    except Exception as exc:
        # A model/runtime failure must not prevent durable capture or recovery.
        degrees = None
        orientation_error = str(exc)
        print(f"Orientation automatique indisponible : {exc}", file=sys.stderr, flush=True)
    oriented = raw.transpose(Image.Transpose.ROTATE_180) if degrees == 180 else raw
    # Keep the existing angle estimator; its crop is deliberately discarded.
    transform = detect_geometry(oriented, dpi)
    transform.update(crop=None, method="qwen3.5", crop_model=QWEN_MODEL)
    if degrees is not None:
        transform["quarter_turns"] = degrees // 90
        transform["orientation"] = "text-lines"
    else:
        transform["review"] = True
    if orientation_error is not None:
        transform["orientation_error"] = orientation_error
    try:
        # Use the same transform path as saving/manual correction, including DPI.
        straight, output_dpi = apply_transform(raw, dpi, transform)
        prediction = qwen_prediction(straight)
        crop, reason = crop_from_prediction(prediction, straight.size, output_dpi)
        if crop is None:
            transform.update(review=True, crop_warning=reason)
        else:
            transform["crop"] = crop
    except Exception as exc:
        transform.update(crop=None, review=True, crop_error=str(exc))
        print(f"Recadrage Qwen indisponible : {exc}", file=sys.stderr, flush=True)
    return transform


def ollama_lines(path, payload=None, timeout=QWEN_TIMEOUT):
    """Local-only HTTP, no proxies/redirects; enforce an overall deadline."""
    deadline = time.monotonic() + timeout
    connection = http.client.HTTPConnection("127.0.0.1", 11434, timeout=min(5, timeout))
    owner = getattr(PROCESSING_CONTEXT, "worker", None)
    try:
        if owner:
            owner.register_connection(connection)
        connection.connect()
        if owner:
            owner.register_connection(connection)
        sock = connection.sock
        def remaining():
            if owner and owner.stopping.is_set():
                raise ScanError("Traitement interrompu ; la page sera reprise.")
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError("Délai Ollama dépassé.")
            sock.settimeout(value)
        remaining()
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        connection.request("POST" if body is not None else "GET", "/api/" + path,
                           body=body, headers={"Content-Type": "application/json"})
        remaining()
        response = connection.getresponse()
        if response.status != 200:
            raise ScanError(f"Ollama : erreur HTTP {response.status} ({path}).")
        pending = b""
        while True:
            remaining()
            chunk = response.read1(65536)
            if not chunk:
                break
            pending += chunk
            if len(pending) > 2 * 1024 * 1024:
                raise ScanError("Réponse Ollama trop volumineuse.")
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if line.strip():
                    yield json.loads(line)
        if pending.strip():
            yield json.loads(pending)
    except (OSError, http.client.HTTPException) as exc:
        raise ScanError("Ollama local indisponible ou délai dépassé. "
                        "Démarrer Ollama puis réessayer. " + str(exc)) from exc
    finally:
        connection.close()
        if owner:
            owner.register_connection(None)


def ollama_json(path, payload=None, timeout=QWEN_TIMEOUT):
    records = list(ollama_lines(path, payload, timeout))
    if len(records) != 1 or not isinstance(records[0], dict):
        raise ScanError("Réponse Ollama invalide.")
    result = records[0]
    if result.get("error"):
        raise ScanError(str(result["error"]))
    return result


@lru_cache(maxsize=1)
def ensure_qwen_model():
    """Only successful preparation is cached; a stopped service can recover."""
    models = ollama_json("tags", timeout=5).get("models", [])
    if any(model.get("name") == QWEN_MODEL for model in models):
        return
    print("Téléchargement Qwen3.5 9B dans le cache Ollama (~6,6 Go)…", flush=True)
    last = None
    success = False
    for record in ollama_lines("pull", {"model": QWEN_MODEL, "stream": True}, timeout=1800):
        if record.get("error"):
            raise ScanError(str(record["error"]))
        status = record.get("status", "")
        total = record.get("total", 0)
        percent = int(100 * record.get("completed", 0) / total) if total else None
        progress = (status, percent // 5 if percent is not None else None)
        if progress != last:
            print(f"Qwen : {status}" + (f" · {percent}%" if percent is not None else ""), flush=True)
            last = progress
        success = status == "success"
    if not success:
        raise ScanError("Téléchargement Qwen incomplet ; relancer pour le reprendre.")


def qwen_prediction(image):
    ensure_qwen_model()
    preview = image.convert("RGB")
    preview.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    encoded = io.BytesIO()
    preview.save(encoded, format="PNG")
    result = ollama_json("chat", {
        "model": QWEN_MODEL, "stream": False, "think": False,
        "keep_alive": "30m", "format": QWEN_SCHEMA,
        "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 256},
        "messages": [{"role": "user", "content": QWEN_PROMPT,
                      "images": [base64.b64encode(encoded.getvalue()).decode("ascii")]}],
    })
    if result.get("done") is not True or result.get("done_reason") == "length":
        raise ScanError("Réponse Qwen incomplète.")
    return json.loads(result["message"]["content"])


def crop_from_prediction(prediction, size, dpi):
    """Map normalized coordinates in the already straightened image outward."""
    dx, dy = check_dpi(dpi)
    if not isinstance(prediction, dict) or prediction.get("certain") is not True:
        return None, "Qwen ne distingue pas les limites de la page avec certitude."
    bounds = prediction.get("bbox")
    if (not isinstance(bounds, list) or len(bounds) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in bounds)):
        return None, "Coordonnées Qwen invalides."
    left, top, right, bottom = bounds
    if not (0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
        return None, "Rectangle Qwen invalide."
    if (right - left) * (bottom - top) < 200000:
        return None, "Qwen propose une zone trop petite pour une page complète."
    width, height = size
    mx, my = math.ceil(dx * .5 / 25.4), math.ceil(dy * .5 / 25.4)
    return [max(0, math.floor(left * width / 1000) - mx),
            max(0, math.floor(top * height / 1000) - my),
            min(width, math.ceil(right * width / 1000) + mx),
            min(height, math.ceil(bottom * height / 1000) + my)], None


def detect_geometry(raw, dpi):
    """Contours de papier très contrastés, sinon consensus de lignes de texte.

    Sans contour fiable, ne retire aucune marge. Aucune tentative de déformation
    de reliure. Les bruts restent toujours disponibles.
    """
    import cv2
    import numpy as np

    dpi = check_dpi(dpi)
    # Detection and application must share the same coordinate system.
    if abs(dpi[0] - dpi[1]) > 0.01:
        return identity_transform()
    preview = raw.convert("RGB")
    preview.thumbnail((1400, 1400))
    gray = cv2.cvtColor(np.asarray(preview), cv2.COLOR_RGB2GRAY)
    height, width = gray.shape
    if min(width, height) < 80 or float(gray.std()) < 5:
        return identity_transform()
    _, mask = cv2.threshold(cv2.GaussianBlur(gray, (5, 5), 0), 0, 255,
                            cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:3]:
        area = cv2.contourArea(contour)
        fraction = area / (width * height)
        polygon = cv2.approxPolyDP(contour, .015 * cv2.arcLength(contour, True), True)
        if not (.25 < fraction < .96 and len(polygon) == 4 and cv2.isContourConvex(polygon)):
            continue
        x, y, w, h = cv2.boundingRect(polygon)
        if x <= 2 or y <= 2 or x + w >= width - 2 or y + h >= height - 2:
            continue
        rect = cv2.minAreaRect(contour)
        if area / max(rect[1][0] * rect[1][1], 1) < .94:
            continue
        inside = np.zeros_like(gray)
        cv2.drawContours(inside, [polygon], -1, 255, -1)
        if float(np.median(gray[inside > 0])) - float(np.median(gray[inside == 0])) < 30:
            continue
        box = cv2.boxPoints(rect)
        angles = []
        for i in range(4):
            dx, dy = box[(i + 1) % 4] - box[i]
            theta = (math.degrees(math.atan2(float(dy), float(dx))) + 45) % 90 - 45
            angles.append(theta)
        angle = float(np.median(angles))
        if abs(angle) > 12:
            continue
        angle = 0.0 if abs(angle) < .15 else round(angle, 3)
        page_mask = Image.new("L", raw.size)
        points = [(round(float(p[0]) * raw.width / width),
                   round(float(p[1]) * raw.height / height)) for p in polygon[:, 0, :]]
        ImageDraw.Draw(page_mask).polygon(points, fill=255)
        if angle:
            page_mask = page_mask.rotate(angle, Image.Resampling.NEAREST, expand=True, fillcolor=0)
        left, top, right, bottom = page_mask.getbbox()
        mx, my = (math.ceil(v * 3 / 25.4) for v in dpi)
        crop = [max(0, left - mx), max(0, top - my),
                min(page_mask.width, right + mx), min(page_mask.height, bottom + my)]
        return {"quarter_turns": 0, "angle": angle, "crop": crop,
                "method": "paper", "review": False}

    # Only straighten text-like pages with many agreeing horizontal segments.
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    joined = cv2.morphologyEx(ink, cv2.MORPH_CLOSE,
                             np.ones((1, max(3, width // 70)), dtype=np.uint8))
    edges = cv2.Canny(joined, 60, 160)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 1800, threshold=60,
                           minLineLength=max(40, width // 7), maxLineGap=width // 60)
    candidates = []
    if lines is not None:
        for x1, y1, x2, y2 in lines[:, 0]:
            angle = math.degrees(math.atan2(int(y2 - y1), int(x2 - x1)))
            if abs(angle) <= 10:
                candidates.append((angle, (y1 + y2) / 2))
    ink_fraction = float((gray < 150).mean())
    if len(candidates) >= 8 and .005 < ink_fraction < .25:
        angles, ys = np.array(candidates).T
        median = float(np.median(angles))
        agreement = np.abs(angles - median) < .75
        if agreement.mean() >= .85 and np.ptp(ys[agreement]) > height * .3 and .2 < abs(median) < 9.5:
            result = identity_transform()
            result.update(angle=round(median, 3), method="text-lines")
            return result
    return identity_transform()


def export_pdf(session):
    import pymupdf

    if session.pending.exists():
        raise ScanError("Une transaction attend une récupération. Fermer puis reprendre la session avant l'export.")
    pages = session.available_pages(report=print)
    if any(p.get("qwen") and (p["qwen"]["processing"] != "done" or
                              not p["qwen"]["validated"]) for p in pages):
        raise ScanError("Des pages attendent un traitement ou une validation. "
                        "Reprendre avec la commande scan du script Qwen, puis F pour vérifier.")
    if not pages:
        raise ScanError("Session vide ou images manquantes : aucune page à exporter.")
    # Validate every source before touching even the temporary PDF.
    for page in pages:
        image_path = safe_child(session.path, page["image"])
        if not image_path.is_file():
            raise ScanError(f"Image manquante : {image_path}")
        check_dpi(page["dpi"])
        with Image.open(image_path) as image:
            image.verify()
    target = session.path / f"{session.book}.pdf"
    temp = target.with_name(target.name + ".tmp")
    try:
        with pymupdf.open() as document:
            for entry in pages:
                image_path = safe_child(session.path, entry["image"])
                with Image.open(image_path) as image:
                    width, height = image.size
                dx, dy = check_dpi(entry["dpi"])
                page = document.new_page(width=width * 72 / dx, height=height * 72 / dy)
                page.insert_image(page.rect, filename=str(image_path), keep_proportion=False)
            document.set_metadata({"title": session.book, "creator": "scan_book"})
            document.save(temp, deflate=True, garbage=3)
        with temp.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return target


@contextmanager
def com_context():
    if sys.platform != "win32":
        raise ScanError("L'acquisition WIA nécessite Windows.")
    import pythoncom
    pythoncom.CoInitialize()
    try:
        yield
    finally:
        pythoncom.CoUninitialize()


def property_map(item):
    return {int(prop.PropertyID): prop for prop in item.Properties}


def property_description(prop):
    result = {"name": str(prop.Name), "value": prop.Value, "readonly": bool(prop.IsReadOnly)}
    kind = int(prop.SubType)
    if kind == 1:
        result.update(min=prop.SubTypeMin, max=prop.SubTypeMax, step=prop.SubTypeStep)
    elif kind in (2, 3):
        result["values"] = list(prop.SubTypeValues)
    return result


def set_wia_property(props, key, value):
    prop = props.get(WIA_IDS.get(key, key))
    if prop is None:
        raise ScanError(f"Le pilote WIA n'expose pas le réglage {key}.")
    info = property_description(prop)
    current = info["value"]
    if current == value:
        return
    if info["readonly"]:
        raise ScanError(f"{info['name']} est en lecture seule (valeur : {current}).")
    kind = int(prop.SubType)
    if kind == 1:
        step = info["step"]
        if not info["min"] <= value <= info["max"] or (step and (value - info["min"]) % step):
            raise ScanError(f"{info['name']} : {value} refusé ; plage {info['min']}–{info['max']}, pas {step}.")
    elif kind == 2 and value not in info["values"]:
        raise ScanError(f"{info['name']} : {value} refusé ; valeurs possibles : {info['values']}.")
    elif kind == 3:
        mask = 0
        for flag in info["values"]:
            mask |= int(flag)
        if value & ~mask:
            raise ScanError(f"{info['name']} : option {value} non prise en charge.")
    try:
        prop.Value = value
    except Exception as exc:
        raise ScanError(f"Le pilote refuse {info['name']} = {value} : {exc}") from exc
    if prop.Value != value:
        raise ScanError(f"Le pilote a appliqué {info['name']} = {prop.Value}, au lieu de {value}.")


def maximum_extent(prop):
    info = property_description(prop)
    if "max" in info:
        return int(info["max"])
    if info.get("values"):
        return int(max(info["values"]))
    return int(info["value"])


class WiaScanner:
    """Toutes les méthodes et références COM restent dans le même thread."""

    def __init__(self, selector=None):
        import win32com.client
        self.manager = win32com.client.Dispatch("WIA.DeviceManager")
        infos = [info for info in self.manager.DeviceInfos if info.Type == 1]
        if not infos:
            raise ScanError("Aucun scanner WIA détecté. Vérifier alimentation, USB, pilote Epson et service WIA (stisvc).")
        if selector:
            matches = [info for info in infos if info.DeviceID == selector]
            if not matches:
                matches = [info for info in infos if selector.casefold() in self.name(info).casefold()]
            infos = matches
        if len(infos) != 1:
            choices = "; ".join(f"{self.name(info)} [{info.DeviceID}]" for info in infos)
            raise ScanError(f"Choisir un scanner avec --scanner IDENTIFIANT. {choices or 'Aucun appareil correspondant.'}")
        self.info = infos[0]
        self.id = self.info.DeviceID
        self.label = self.name(self.info)
        self.device = self.info.Connect()
        items = list(self.device.Items)
        flatbeds = [item for item in items if 4125 in property_map(item)
                    and str(property_map(item)[4125].Value).upper() == FLATBED_CATEGORY]
        if flatbeds:
            self.item = flatbeds[0]
        else:
            candidates = [item for item in items if WIA_IDS["x_dpi"] in property_map(item)]
            if len(candidates) != 1:
                raise ScanError("Impossible de sélectionner la vitre WIA sans ambiguïté.")
            self.item = candidates[0]
        self.props = property_map(self.item)
        self.effective = None

    @staticmethod
    def name(info):
        props = property_map(info)
        return str(props[7].Value) if 7 in props else str(info.DeviceID)

    def configure(self, settings):
        props = self.props
        # WIA 1.0 devices may expose a source selector on the root device.
        root_props = property_map(self.device)
        if 3088 in root_props:
            set_wia_property(root_props, 3088, 2)  # FLATBED
        set_wia_property(props, "intent", 1 if settings["color"] == "color" else 2)
        set_wia_property(props, "x_dpi", settings["dpi"])
        set_wia_property(props, "y_dpi", settings["dpi"])
        dx, dy = (int(props[WIA_IDS[key]].Value) for key in ("x_dpi", "y_dpi"))
        set_wia_property(props, "x", 0)
        set_wia_property(props, "y", 0)
        max_width = maximum_extent(props[WIA_IDS["width"]])
        max_height = maximum_extent(props[WIA_IDS["height"]])
        area = settings.get("area")
        if area:
            x, y, width, height = [round(value * resolution / 25.4)
                                  for value, resolution in zip(area, (dx, dy, dx, dy))]
            if x + width > max_width or y + height > max_height or min(width, height) <= 0:
                raise ScanError("La zone demandée dépasse la vitre ou devient vide à cette résolution.")
        else:
            x, y, width, height = 0, 0, max_width, max_height
        # Shrink extents before moving the origin to avoid temporarily exceeding the bed.
        set_wia_property(props, "width", width)
        set_wia_property(props, "height", height)
        set_wia_property(props, "x", x)
        set_wia_property(props, "y", y)
        expected = {"x_dpi": settings["dpi"], "y_dpi": settings["dpi"],
                    "x": x, "y": y, "width": width, "height": height}
        actual = {key: int(props[WIA_IDS[key]].Value) for key in expected}
        if actual != expected:
            raise ScanError(f"Le pilote a modifié les réglages : demandé {expected}, obtenu {actual}.")
        if 4103 in props and int(props[4103].Value) != (3 if settings["color"] == "color" else 2):
            raise ScanError("Le pilote n'a pas appliqué le mode couleur/gris demandé.")
        self.effective = {**actual, "scanner": self.id, "name": self.label, "color": settings["color"]}
        return copy.deepcopy(self.effective)

    def acquire(self):
        if self.effective is None:
            raise ScanError("Le scanner doit être configuré avant acquisition.")
        result = self.item.Transfer(PNG_FORMAT)
        # Transfer may return the driver's preferred format. Decode the bytes,
        # never assume PNG simply because PNG was requested.
        with Image.open(io.BytesIO(bytes(result.FileData.BinaryData))) as source:
            source.load()
            image = source.convert("RGB" if self.effective["color"] == "color" else "L")
        self.effective["received_size"] = list(image.size)
        return image, (self.effective["x_dpi"], self.effective["y_dpi"])


def list_devices():
    import win32com.client
    with com_context():
        manager = win32com.client.Dispatch("WIA.DeviceManager")
        found = False
        for info in manager.DeviceInfos:
            if info.Type != 1:
                continue
            found = True
            print(f"\n{WiaScanner.name(info)}\n  Identifiant : {info.DeviceID}")
            try:
                device = info.Connect()
                for index, item in enumerate(device.Items, 1):
                    props = property_map(item)
                    print(f"  Source {index} :")
                    for key, identifier in WIA_IDS.items():
                        if identifier in props:
                            print(f"    {key}: {json.dumps(property_description(props[identifier]), ensure_ascii=False)}")
            except Exception as exc:
                print(f"  Capacités indisponibles : {exc}")
        if not found:
            raise ScanError("Aucun scanner WIA détecté. Vérifier USB, alimentation, pilote et service stisvc.")


class ProcessingWorker(threading.Thread):
    """Disk references in the queue; only one inference in memory at a time."""

    def __init__(self, session):
        super().__init__(name="qwen-processing", daemon=False)
        self.session = session
        self.jobs, self.results = queue.Queue(), queue.Queue()
        self.stopping = threading.Event()
        self.connection = None
        self.connection_socket = None
        self.connection_lock = threading.Lock()

    def enqueue(self, page):
        state = page.get("qwen", {})
        if state.get("processing") == "pending":
            self.jobs.put((page["number"], state["revision"]))

    def register_connection(self, connection):
        with self.connection_lock:
            if connection is not None and self.stopping.is_set():
                connection.close()
                raise ScanError("Traitement interrompu ; la page sera reprise.")
            self.connection = connection
            self.connection_socket = connection.sock if connection is not None else None

    def stop(self):
        self.stopping.set()
        self.jobs.put(None)
        with self.connection_lock:
            if self.connection_socket is not None:
                try:
                    self.connection_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def run(self):
        PROCESSING_CONTEXT.worker = self
        try:
            while not self.stopping.is_set():
                job = self.jobs.get()
                if job is None or self.stopping.is_set():
                    break
                number, revision = job
                try:
                    source = self.session.processing_input(number, revision)
                    if source is None:
                        self.results.put(("skipped", number, None))
                        continue
                    page, raw = source
                    try:
                        transform = detect_transform(raw, page["raw_dpi"])
                        image, dpi = apply_transform(raw, page["raw_dpi"], transform)
                    except Exception as exc:
                        transform = identity_transform()
                        transform["crop_error"] = str(exc)
                        image, dpi = raw, page["raw_dpi"]
                    if self.stopping.is_set():
                        break
                    result = self.session.finish_processing(number, revision, transform, image, dpi)
                    if result is not None:
                        self.results.put(("processed", result, None))
                    else:
                        self.results.put(("skipped", number, None))
                except Exception as exc:
                    self.results.put(("processing_error", number, str(exc)))
                    # Stop writes after an I/O error; pending work remains durable.
                    break
        finally:
            del PROCESSING_CONTEXT.worker


class ScanWorker(threading.Thread):
    def __init__(self, session, settings, acquisition):
        super().__init__(name="scan-book-worker", daemon=False)
        self.session, self.settings, self.acquisition = session, settings, acquisition
        self.jobs, self.results = queue.Queue(), queue.Queue()

    def run(self):
        scanner = None
        context = com_context() if self.acquisition else None
        try:
            if context:
                context.__enter__()
            while True:
                job = self.jobs.get()
                if job is None:
                    break
                action, payload = job
                try:
                    if action == "connect":
                        scanner = WiaScanner(self.settings.get("scanner"))
                        effective = scanner.configure(self.settings)
                        self.settings["scanner"] = scanner.id
                        self.session.save_settings(self.settings)
                        self.session.set_phase("acquisition")
                        result = effective
                    elif action == "mode":
                        if scanner is None:
                            raise ScanError("Scanner non connecté.")
                        settings = {**self.settings, "color":
                                    "gray" if self.settings["color"] == "color" else "color"}
                        try:
                            result = scanner.configure(settings)
                            self.session.save_settings(settings)
                        except Exception as exc:
                            # A driver can partially apply settings before failing.
                            try:
                                scanner.configure(self.settings)
                            except Exception as rollback_error:
                                self.results.put(("fatal", None,
                                    f"Changement de mode impossible : {exc}. "
                                    f"Restauration impossible : {rollback_error}. Relancer la session."))
                                break
                            raise ScanError(f"Changement de mode impossible : {exc}. Mode précédent conservé.") from exc
                        self.settings = settings
                    elif action == "scan":
                        if scanner is None:
                            raise ScanError("Scanner non connecté.")
                        image, dpi = scanner.acquire()
                        metadata = {**self.settings, "effective": scanner.effective}
                        result = self.session.capture(image, dpi, metadata, replace=payload)
                    elif action == "correct":
                        result = self.session.correct(payload[0], payload[1])
                    elif action == "review":
                        self.session.set_phase("review")
                        result = None
                    elif action == "validate":
                        result = self.session.validate_page(payload)
                    elif action == "export":
                        with self.session.mutex:
                            result = export_pdf(self.session)
                            self.session.set_phase("complete")
                    else:
                        raise ScanError(f"Commande inconnue : {action}")
                    self.results.put((action, result, None))
                except Exception as exc:
                    self.results.put((action, None, str(exc)))
        except Exception as exc:
            self.results.put(("fatal", None, str(exc)))
        finally:
            # Release COM proxies before CoUninitialize on this same thread.
            scanner = None
            if context:
                context.__exit__(None, None, None)


class Preview:
    """Tk on the main thread; acquisition and inference have separate workers."""

    def __init__(self, session, settings, edit_page=None):
        import tkinter as tk
        from tkinter import ttk
        from PIL import ImageTk

        self.tk, self.ttk, self.ImageTk = tk, ttk, ImageTk
        self.session, self.edit_page = session, edit_page
        self.root = tk.Tk()
        self.root.title(f"Scan Book — {session.book}")
        self.root.geometry("1100x850")
        self.busy = False
        self.connected = False
        self.editing = False
        self.closed = False
        self.exit_code = 0
        self.phase = session.snapshot().get("qwen_workflow", {}).get("phase", "acquisition")
        if self.phase == "complete":
            self.phase = "acquisition"
        self.closing = False
        self.storage_error = False
        self.current = None
        self.display_image = None
        self.photo = None
        self.scale = 1.0
        self.preview_margin = 16
        self.crop = None
        self.drag_start = None
        self.updating = False
        self.redraw_job = None
        self.status = tk.StringVar(value="Prêt")
        self.progress = tk.StringVar(value="")
        self.mode_text = tk.StringVar(value="Mode du prochain scan : connexion…")
        self.angle = tk.DoubleVar(value=0)
        self.zoom = tk.StringVar(value="Ajuster")
        self.turns = 0
        self.worker = ScanWorker(session, settings, acquisition=edit_page is None and self.phase == "acquisition")
        self.processor = ProcessingWorker(session) if edit_page is None else None

        toolbar = ttk.Frame(self.root, padding=8)
        toolbar.pack(fill="x")
        self.buttons = {}
        for key, label in (("scan", "Entrée · Scanner"), ("correct", "C · Corriger"),
                           ("retake", "R · Refaire"), ("export", "F · PDF et fin"), ("quit", "Q · Quitter")):
            button = ttk.Button(toolbar, text=label, command=lambda k=key: self.command(k))
            button.pack(side="left", padx=3)
            self.buttons[key] = button
        ttk.Label(toolbar, text="Zoom").pack(side="left", padx=(16, 4))
        zoom = ttk.Combobox(toolbar, textvariable=self.zoom,
                            values=("Ajuster", "25%", "50%", "100%", "150%", "200%"), width=8, state="readonly")
        zoom.pack(side="left")
        zoom.bind("<<ComboboxSelected>>", lambda _: self.render())

        self.modebar = None
        if edit_page is None and self.phase == "acquisition":
            modebar = ttk.Frame(self.root, padding=(11, 0, 8, 8))
            self.modebar = modebar
            modebar.pack(fill="x")
            ttk.Label(modebar, textvariable=self.mode_text).pack(side="left")
            self.buttons["mode"] = ttk.Button(modebar, text="M · Couleur / N&B",
                                               command=lambda: self.command("mode"))
            self.buttons["mode"].pack(side="left", padx=12)

        self.editor = ttk.Frame(self.root, padding=8)
        ttk.Label(self.editor, text="Angle (°)").pack(side="left")
        self.angle_control = ttk.Scale(self.editor, from_=-15, to=15, variable=self.angle,
                                      command=self.angle_changed, length=230)
        self.angle_control.pack(side="left", padx=5)
        self.angle_text = tk.StringVar(value="0.00°")
        ttk.Label(self.editor, textvariable=self.angle_text, width=9).pack(side="left")
        for label, callback in (("↶ 90°", self.turn), ("Brut / réinitialiser", self.reset),
                                ("Valider", self.accept), ("Annuler", self.cancel)):
            ttk.Button(self.editor, text=label, command=callback).pack(side="left", padx=4)

        ttk.Label(self.root, text="Hachures : hors image · En correction, le cadre vert délimite la zone conservée.",
                  padding=(11, 0, 8, 6)).pack(fill="x")
        self.canvas_frame = ttk.Frame(self.root)
        self.canvas_frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(self.canvas_frame, background="white", highlightthickness=0)
        hbar = ttk.Scrollbar(self.canvas_frame, orient="horizontal", command=self.canvas.xview)
        vbar = ttk.Scrollbar(self.canvas_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        self.canvas_frame.rowconfigure(0, weight=1)
        self.canvas_frame.columnconfigure(0, weight=1)
        ttk.Label(self.root, textvariable=self.status, padding=10, wraplength=1050).pack(fill="x")
        ttk.Label(self.root, textvariable=self.progress, padding=(10, 0, 10, 6)).pack(fill="x")
        self.canvas.bind("<Configure>", lambda _: self.schedule_render())
        self.canvas.bind("<ButtonPress-1>", self.drag_begin)
        self.canvas.bind("<B1-Motion>", self.drag_move)
        self.canvas.bind("<ButtonRelease-1>", self.drag_end)
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-int(e.delta / 120), "units"))
        self.root.bind("<KeyPress>", self.key)
        self.root.protocol("WM_DELETE_WINDOW", lambda: self.command("quit"))
        try:
            if edit_page is None:
                pages = session.available_pages(report=self.say)
                if self.phase == "acquisition" and pages:
                    self.show_page(pages[-1], announce=False, raw=True)
            else:
                self.show_page(session.page(edit_page))
                self.start_edit()
        except BaseException:
            self.root.destroy()
            raise
        self.worker.start()
        if self.processor:
            for page in session.snapshot()["pages"]:
                self.processor.enqueue(page)
            self.processor.start()
        if edit_page is None and self.phase == "acquisition":
            self.submit("connect", None, "Connexion et vérification des réglages WIA…")
        elif edit_page is None:
            self.show_review()
        self.root.after(80, self.poll)
        self.update_buttons()

    def say(self, message):
        self.status.set(message)
        print(message, flush=True)

    def update_buttons(self):
        pages = self.session.available_pages()
        missing = [p for p in self.session.snapshot()["pages"] if self.session.missing_images(p)]
        remaining = [p for p in pages if p.get("qwen") and not p["qwen"]["validated"]]
        ready = sum(p.get("qwen", {}).get("processing", "done") == "done" for p in pages)
        self.progress.set(f"{len(pages)} pages acquises · {ready} traitées · {len(remaining)} à valider")
        if missing:
            numbers = ", ".join(f"{p['number']:04d}" for p in missing)
            self.progress.set(self.progress.get() + f" · Images manquantes — pages ignorées : {numbers}")
        reviewing = self.phase == "review" and self.edit_page is None
        self.buttons["scan"].configure(text="Entrée · Valider / suivante" if reviewing else "Entrée · Scanner")
        self.buttons["export"].configure(text="F · PDF et fin" if reviewing else "F · Vérifier les pages")
        for name, button in self.buttons.items():
            allowed = not self.busy and not self.editing and not self.closing and not self.storage_error
            if name == "scan":
                allowed = allowed and self.edit_page is None and (
                    self.current is not None if reviewing else self.connected)
            if name in ("retake", "mode"):
                allowed = allowed and self.connected and self.edit_page is None and self.phase == "acquisition"
            if name == "correct":
                allowed = allowed and (reviewing or self.edit_page is not None)
            if name in ("correct", "retake"):
                allowed = allowed and self.current is not None
            if name == "export":
                allowed = allowed and bool(pages) and self.edit_page is None and (not reviewing or not remaining)
            if name == "quit":
                allowed = not self.closing
            button.configure(state="normal" if allowed else "disabled")

    def show_review(self):
        if self.modebar is not None:
            self.modebar.pack_forget()
        pages = self.session.available_pages(report=self.say)
        remaining = [p for p in pages if p.get("qwen") and not p["qwen"]["validated"]]
        self.current = None
        self.display_image = None
        self.crop = None
        if not remaining:
            self.render()
            self.say("Vérification terminée — F pour créer le PDF." if pages else
                     "Aucune page disponible à exporter — images manquantes ou session vide.")
        elif remaining[0]["qwen"]["processing"] != "done":
            self.render()
            self.say(f"Vérification · attente du traitement de la page {remaining[0]['number']:04d}…")
        else:
            self.show_page(remaining[0])
        self.update_buttons()

    def submit(self, action, payload, message):
        self.busy = True
        self.say(message)
        self.update_buttons()
        self.worker.jobs.put((action, payload))

    def key(self, event):
        # Editor widgets keep normal keyboard behavior; global shortcuts apply
        # only outside the correction form, except Escape to cancel editing.
        if self.editing:
            if event.keysym == "Escape":
                self.cancel()
            return
        if self.root.focus_get() is not None and self.root.focus_get().winfo_class() == "TCombobox":
            return
        mapping = {"Return": "scan", "c": "correct", "r": "retake", "m": "mode", "f": "export", "q": "quit"}
        action = mapping.get(event.keysym, mapping.get(event.keysym.lower()))
        if action:
            self.command(action)
            return "break"

    def command(self, action):
        try:
            self.dispatch_command(action)
        except Exception as exc:
            self.say(f"Erreur : {exc}")

    def dispatch_command(self, action):
        if action == "quit":
            self.shutdown()
            return
        if self.busy or self.closing or self.storage_error or self.editing:
            return
        if action == "mode" and self.connected and self.edit_page is None and self.phase == "acquisition":
            self.submit("mode", None, "Changement du mode couleur/N&B…")
        elif action == "scan" and self.edit_page is None and self.phase == "review" and self.current:
            self.submit("validate", self.current["number"], "Validation de la page…")
        elif action == "scan" and self.connected and self.phase == "acquisition":
            self.submit("scan", None, "Acquisition en cours…")
        elif action == "retake" and self.connected and self.current and self.phase == "acquisition":
            self.submit("scan", self.current["number"], f"Nouvelle acquisition de la page {self.current['number']}…")
        elif action == "correct" and self.current and (self.phase == "review" or self.edit_page is not None):
            self.start_edit()
        elif action == "export" and self.session.snapshot()["pages"] and self.edit_page is None:
            if self.phase == "acquisition":
                self.submit("review", None, "Ouverture de la vérification…")
            elif all(not p.get("qwen") or (p["qwen"]["validated"] and p["qwen"]["processing"] == "done")
                     for p in self.session.available_pages()):
                self.submit("export", None, "Assemblage du PDF…")

    def poll(self):
        try:
            self.poll_events()
        except Exception as exc:
            self.busy = False
            self.say(f"Erreur d'aperçu : {exc}")
            self.update_buttons()
            if not self.closed:
                self.root.after(80, self.poll)

    def poll_events(self):
        if self.closed:
            return
        if self.closing:
            if not self.worker.is_alive() and (self.processor is None or not self.processor.is_alive()):
                self.closed = True
                for job in self.root.tk.call("after", "info"):
                    self.root.after_cancel(job)
                self.root.destroy()
            else:
                self.root.after(80, self.poll)
            return
        # Read console keys without blocking Tk; drain keys pressed while busy
        # so they cannot unexpectedly start another acquisition after completion.
        if sys.platform == "win32" and sys.stdin.isatty():
            import msvcrt
            while msvcrt.kbhit():
                char = msvcrt.getwch()
                if char in ("\x00", "\xe0"):
                    msvcrt.getwch()
                    continue
                if char == "\x03":
                    self.command("quit")
                elif not self.editing:
                    action = {"\r": "scan", "c": "correct", "r": "retake",
                              "m": "mode", "f": "export", "q": "quit"}.get(char.lower())
                    if action:
                        self.command(action)
                if self.closed:
                    return
        while not self.worker.results.empty():
            action, result, error = self.worker.results.get_nowait()
            self.busy = False
            if error:
                self.say(f"Erreur : {error}")
                if action in ("connect", "fatal"):
                    self.exit_code = 1
                    self.shutdown()
                    return
                with self.session.mutex:
                    pending = self.session.pending.exists()
                if pending:
                    self.say(f"Erreur : {error} — brut/journal conservé ; fermer puis reprendre cette session.")
                    self.connected = False
                    self.storage_error = True
            elif action in ("connect", "mode"):
                self.connected = True
                mode = "Couleur" if result["color"] == "color" else "N&B (niveaux de gris)"
                self.mode_text.set(f"Mode du prochain scan : {mode}")
                self.say(f"{result['name']} · {result['x_dpi']} ppp · {mode} — Entrée : scanner ; F : vérifier les pages.")
            elif action in ("scan", "correct"):
                self.show_page(result, raw=action == "scan")
                if action == "scan" and self.processor:
                    self.processor.enqueue(result)
                if action == "correct" and self.edit_page is not None:
                    self.shutdown()
                    return
            elif action == "review":
                self.phase = "review"
                self.show_review()
            elif action == "validate":
                self.show_review()
            elif action == "export":
                self.say(f"PDF créé : {result}")
                self.shutdown()
                return
            self.update_buttons()
        if self.processor:
            while not self.processor.results.empty():
                action, result, error = self.processor.results.get_nowait()
                if error:
                    self.storage_error = True
                    self.say(f"Erreur de traitement de la page {result} : {error} — fermer puis reprendre la session.")
                elif self.phase == "review" and self.current is None and not self.busy and not self.editing and not self.storage_error:
                    self.show_review()
                self.update_buttons()
        self.root.after(80, self.poll)

    def show_page(self, page, announce=True, raw=False):
        self.current = copy.deepcopy(page)
        self.display_image = load_image(safe_child(self.session.path, page["raw" if raw else "image"]))
        self.crop = None
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        self.render()
        if announce:
            if raw:
                self.say(f"Page {page['number']:04d} acquise et sauvegardée — Entrée : scan suivant ; R : refaire ; F : vérifier.")
                return
            review = " · À VÉRIFIER (C pour corriger)" if page["transform"].get("review") else ""
            rotation = " · Rotation auto 180°" if (page["transform"].get("orientation")
                       and page["transform"].get("quarter_turns") == 2) else ""
            warning = " · Orientation auto indisponible" if page["transform"].get("orientation_error") else ""
            if page["transform"].get("crop_error"):
                warning += " · Recadrage Qwen indisponible"
            elif page["transform"].get("crop_warning"):
                warning += " · Recadrage Qwen incertain"
            self.say(f"Page {page['number']:04d} · {page['size'][0]} × {page['size'][1]} px{rotation}{warning}{review} — Entrée : valider ; C : corriger.")

    def start_edit(self):
        self.raw = load_image(safe_child(self.session.path, self.current["raw"]))
        self.editing = True
        self.updating = True
        transform = self.current["transform"]
        self.turns = transform.get("quarter_turns", 0)
        self.angle.set(transform.get("angle", 0))
        self.crop = copy.deepcopy(transform.get("crop"))
        self.updating = False
        self.editor.pack(before=self.canvas_frame, fill="x")
        self.refresh_edit()
        self.say("Correction : tracer le rectangle à conserver ; régler l'angle avant de recadrer. Valider ou Annuler.")
        self.update_buttons()

    def refresh_edit(self):
        if not self.editing or self.busy:
            return
        transform = {"quarter_turns": self.turns, "angle": round(self.angle.get(), 3), "crop": None}
        self.display_image, _ = apply_transform(self.raw, self.current["raw_dpi"], transform)
        self.angle_text.set(f"{self.angle.get():.2f}°")
        self.render()

    def angle_changed(self, _=None):
        if self.updating or not self.editing or self.busy:
            return
        self.crop = None
        self.refresh_edit()

    def turn(self):
        if not self.editing or self.busy:
            return
        self.turns = (self.turns + 1) % 4
        self.crop = None
        self.refresh_edit()

    def reset(self):
        if not self.editing or self.busy:
            return
        self.updating = True
        self.turns = 0
        self.angle.set(0)
        self.crop = None
        self.updating = False
        self.refresh_edit()

    def accept(self):
        if not self.editing or self.busy:
            return
        transform = {"quarter_turns": self.turns, "angle": round(self.angle.get(), 3),
                     "crop": self.crop, "method": "manual", "review": False}
        self.editing = False
        self.editor.pack_forget()
        self.submit("correct", (self.current["number"], transform), "Sauvegarde de la correction…")

    def cancel(self):
        if not self.editing or self.busy:
            return
        self.editing = False
        self.editor.pack_forget()
        if self.edit_page is not None:
            self.shutdown()
        else:
            self.show_page(self.current)
            self.update_buttons()

    def schedule_render(self):
        if self.redraw_job:
            self.root.after_cancel(self.redraw_job)
        self.redraw_job = self.root.after(100, self.render)

    def render(self):
        if self.closed:
            return
        self.redraw_job = None
        self.canvas.delete("all")
        if self.display_image is None:
            self.draw_background(self.canvas.winfo_width(), self.canvas.winfo_height())
            return
        image = self.display_image
        margin = self.preview_margin
        if self.zoom.get() == "Ajuster":
            self.scale = min(max(1, self.canvas.winfo_width() - 2 * margin) / image.width,
                             max(1, self.canvas.winfo_height() - 2 * margin) / image.height, 1)
        else:
            self.scale = float(self.zoom.get().rstrip("%")) / 100
        width, height = max(1, round(image.width * self.scale)), max(1, round(image.height * self.scale))
        self.photo = self.ImageTk.PhotoImage(image.resize((width, height), Image.Resampling.BILINEAR))
        area_width = max(self.canvas.winfo_width(), width + 2 * margin)
        area_height = max(self.canvas.winfo_height(), height + 2 * margin)
        self.draw_background(area_width, area_height)
        self.canvas.create_image(margin, margin, image=self.photo, anchor="nw")
        self.canvas.create_rectangle(margin - 1, margin - 1, margin + width, margin + height,
                                     outline="#8c8c8c", tags="page-border")
        self.canvas.configure(scrollregion=(0, 0, area_width, area_height))
        self.draw_crop()

    def draw_background(self, width, height):
        # Canvas-only decoration: never changes the scanned image or PDF.
        for offset in range(-height, width, 14):
            self.canvas.create_line(offset, 0, offset + height, height,
                                    fill="#d5d5d5", tags="background")

    def point(self, event):
        return (min(self.display_image.width, max(0, round((self.canvas.canvasx(event.x) - self.preview_margin) / self.scale))),
                min(self.display_image.height, max(0, round((self.canvas.canvasy(event.y) - self.preview_margin) / self.scale))))

    def drag_begin(self, event):
        if self.editing and not self.busy:
            self.drag_start = self.point(event)

    def drag_move(self, event):
        if self.drag_start is None or not self.editing or self.busy:
            return
        x, y = self.point(event)
        sx, sy = self.drag_start
        self.crop = [min(x, sx), min(y, sy), max(x, sx), max(y, sy)]
        self.draw_crop()

    def drag_end(self, event):
        self.drag_move(event)
        self.drag_start = None
        if self.crop and (self.crop[2] - self.crop[0] < 5 or self.crop[3] - self.crop[1] < 5):
            self.crop = None
        self.draw_crop()

    def draw_crop(self):
        self.canvas.delete("crop")
        if self.editing and self.crop:
            self.canvas.create_rectangle(*(self.preview_margin + v * self.scale for v in self.crop),
                                         outline="#32e0b7", width=2, tags="crop")

    def shutdown(self):
        if self.closed or self.closing:
            return
        self.closing = True
        self.say("Fermeture : sauvegarde de l’acquisition en cours et arrêt du traitement…")
        if self.processor:
            self.processor.stop()
        self.worker.jobs.put(None)
        self.worker.join(timeout=.1)
        if self.processor:
            self.processor.join(timeout=.1)
        if not self.worker.is_alive() and (self.processor is None or not self.processor.is_alive()):
            self.closed = True
            for job in self.root.tk.call("after", "info"):
                self.root.after_cancel(job)
            self.root.destroy()
            return
        self.update_buttons()
        # Keep Tk responsive while a non-cancellable WIA transfer finishes.
        self.root.after(80, self.poll)

    def run(self):
        try:
            self.root.mainloop()
        finally:
            # Ctrl+C cannot cancel an in-flight WIA Transfer. Finish its durable
            # save before releasing the session lock, even if Tk has exited.
            if self.processor:
                self.processor.stop()
            self.worker.jobs.put(None)
            self.worker.join()
            if self.processor:
                self.processor.join()
            if not self.closed:
                self.root.destroy()
                self.closed = True
        return self.exit_code


def area_argument(value):
    if value.lower() == "full":
        return None
    try:
        parts = [float(v) for v in value.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Zone attendue : x,y,largeur,hauteur en mm, ou full.") from exc
    if len(parts) != 4 or any(not math.isfinite(v) for v in parts) or min(parts[:2]) < 0 or min(parts[2:]) <= 0:
        raise argparse.ArgumentTypeError("Zone invalide : origine positive ou nulle, dimensions positives.")
    return parts


def positive_int(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Un entier positif est requis.") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("Un entier positif est requis.")
    return number


def make_parser():
    parser = argparse.ArgumentParser(description="Scan de livres WIA, sessions reprenables et PDF.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("devices", help="Lister les scanners WIA et leurs capacités.")
    for command, help_text in (("scan", "Créer ou reprendre une session."),
                                ("export", "Assembler le PDF sans scanner."),
                                ("correct", "Corriger une page depuis son scan brut.")):
        child = sub.add_parser(command, help=help_text)
        child.add_argument("book", type=book_slug, help="Nom du dossier du livre.")
        child.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Dossier contenant les livres (défaut : scans près du script).")
        if command == "scan":
            child.add_argument("--scanner", help="Identifiant WIA ou partie unique du nom.")
            child.add_argument("--dpi", type=positive_int, help="Résolution (défaut nouvelle session : 300).")
            child.add_argument("--color", choices=("color", "gray"), help="Couleur ou gris (défaut : color).")
            child.add_argument("--area", type=area_argument, default=argparse.SUPPRESS,
                               help="x,y,largeur,hauteur en mm, ou full pour toute la vitre.")
        if command == "correct":
            child.add_argument("--page", required=True, type=positive_int, help="Numéro de la page à corriger.")
    return parser


def main(argv=None):
    configure_utf8()
    args = make_parser().parse_args(argv)
    try:
        if args.command == "devices":
            list_devices()
            return 0
        with Session(args.root, args.book, create=args.command == "scan") as session:
            if args.command == "export":
                print(f"PDF créé : {export_pdf(session)}")
                return 0
            settings = {**DEFAULT_SETTINGS, **session.data["settings"]}
            if args.command == "scan":
                for key in ("scanner", "dpi", "color", "area"):
                    if hasattr(args, key) and (getattr(args, key) is not None or key == "area"):
                        settings[key] = getattr(args, key)
            else:
                session.page(args.page)
            return Preview(session, settings, edit_page=getattr(args, "page", None)).run()
    except KeyboardInterrupt:
        print("\nSession conservée. Reprendre avec la même commande.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
