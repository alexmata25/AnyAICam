"""License Plate Recognition: given a vehicle already detected by the
existing YOLO pipeline (ai_person_detector()/save_yolo_events() in
main.py), locates a candidate plate region inside that vehicle's own
crop and reads its text -- purely local, purely offline, no network
calls and no third-party service.

Deliberately dependency-light in the same sense camera_mapping.py and
smart_motion.py are: no import of `main`, so this module (and its
tests) stay free of main.py's own test-discovery-order fragility. It
does import cv2 and pytesseract, both already real dependencies of
this project (opencv-python already ships the Haar cascade this module
uses at cv2.data.haarcascades -- no new model file, no download, no
external asset); pytesseract wraps the tesseract-ocr engine, installed
via the Dockerfile, entirely local.

Every public function is exception-safe by design (returns None on any
failure) -- a plate that can't be found or read must never interrupt
the caller's own detection/recording pipeline.
"""

import os
import re
import time

import cv2
import pytesseract

LPR_ENABLED = os.environ.get("ANYAICAM_LPR_ENABLED", "true").strip().lower() == "true"

_DEFAULT_VEHICLE_CLASSES = "car,truck,bus"
LPR_VEHICLE_CLASSES = frozenset(
    value.strip()
    for value in (os.environ.get("ANYAICAM_LPR_VEHICLE_CLASSES") or _DEFAULT_VEHICLE_CLASSES).split(",")
    if value.strip()
)

# Tesseract's mean per-character confidence (0-100) below which a read
# is treated as unreliable and discarded rather than stored.
LPR_MIN_CONFIDENCE = max(0.0, min(100.0, float(os.environ.get("ANYAICAM_LPR_MIN_CONFIDENCE", "45"))))

# Real plates are short, bounded strings -- these bounds reject both
# OCR noise (1-2 stray characters) and obviously-wrong long reads
# (OCR picking up bumper text/stickers instead of a plate).
LPR_MIN_PLATE_LENGTH = max(1, int(os.environ.get("ANYAICAM_LPR_MIN_PLATE_LENGTH", "5")))
LPR_MAX_PLATE_LENGTH = max(LPR_MIN_PLATE_LENGTH, int(os.environ.get("ANYAICAM_LPR_MAX_PLATE_LENGTH", "10")))

_HAAR_CASCADE_FILENAME = "haarcascade_russian_plate_number.xml"

_PLATE_CHAR_WHITELIST = re.compile(r"[^A-Z0-9]")

_cascade = None
_cascade_load_failed = False


def _get_cascade():
    """Lazy singleton, matching get_yolo_model()'s own pattern in
    main.py -- the cascade file is read from disk once, on first use,
    not at import time (so importing this module never touches the
    filesystem or fails just because opencv's data directory moved)."""
    global _cascade, _cascade_load_failed
    if _cascade is not None or _cascade_load_failed:
        return _cascade
    try:
        cascade_path = os.path.join(cv2.data.haarcascades, _HAAR_CASCADE_FILENAME)
        cascade = cv2.CascadeClassifier(cascade_path)
        if cascade.empty():
            raise RuntimeError(f"Haar cascade failed to load from {cascade_path}")
        _cascade = cascade
    except Exception:
        _cascade_load_failed = True
        _cascade = None
    return _cascade


def reset_state() -> None:
    """Test-only: clears the lazy-loaded cascade singleton so tests can
    exercise both the loaded and not-yet-loaded paths independently."""
    global _cascade, _cascade_load_failed
    _cascade = None
    _cascade_load_failed = False


def normalize_plate_text(raw: str) -> str:
    """Uppercases and strips everything except A-Z0-9 -- plates don't
    reliably use spaces/hyphens the same way across regions/OCR runs,
    so normalizing to a single bare alphanumeric form is what makes
    plate search (substring match against plate_number, already wired
    in the existing Event Center filter) actually reliable."""
    return _PLATE_CHAR_WHITELIST.sub("", (raw or "").upper())


def is_plausible_plate(text: str) -> bool:
    """A normalized plate must be alphanumeric, bounded in length, and
    contain at least one digit -- a real-world plate is never an
    all-letter string this short (excludes OCR noise reading bumper
    stickers/text as a "plate")."""
    if not (LPR_MIN_PLATE_LENGTH <= len(text) <= LPR_MAX_PLATE_LENGTH):
        return False
    return any(character.isdigit() for character in text)


def detect_plate_region(vehicle_crop_bgr):
    """Returns (x, y, w, h) of the best candidate plate region inside
    an already-cropped vehicle image, or None if the cascade found
    nothing or isn't available. Picks the widest match (a plate is
    consistently the widest near-rectangular high-contrast region on
    the rear/front of a vehicle) when more than one candidate exists."""
    cascade = _get_cascade()
    if cascade is None or vehicle_crop_bgr is None or vehicle_crop_bgr.size == 0:
        return None
    try:
        gray = cv2.cvtColor(vehicle_crop_bgr, cv2.COLOR_BGR2GRAY)
        regions = cascade.detectMultiScale(gray, scaleFactor=1.05, minNeighbors=4, minSize=(40, 12))
    except Exception:
        return None
    if len(regions) == 0:
        return None
    x, y, w, h = max(regions, key=lambda region: region[2])
    return int(x), int(y), int(w), int(h)


def read_plate_text(plate_crop_bgr):
    """Runs OCR on an already-cropped plate region. Returns
    (normalized_text, confidence) or None -- never raises; any
    tesseract/opencv failure or an implausible/low-confidence read is
    treated the same as "no plate found", not an error."""
    if plate_crop_bgr is None or plate_crop_bgr.size == 0:
        return None
    try:
        gray = cv2.cvtColor(plate_crop_bgr, cv2.COLOR_BGR2GRAY)
        # Plates are usually small crops (a Haar match a few dozen
        # pixels wide) -- tesseract reads short, dense text far more
        # reliably upscaled, matching the same reasoning ANPR write-ups
        # consistently give for this exact preprocessing step.
        scaled = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        _, thresholded = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        config = "--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        data = pytesseract.image_to_data(
            thresholded, config=config, output_type=pytesseract.Output.DICT
        )
    except Exception:
        return None
    text = normalize_plate_text("".join(data.get("text") or []))
    confidences = [float(value) for value in (data.get("conf") or []) if _is_real_confidence(value)]
    if not text or not confidences:
        return None
    mean_confidence = sum(confidences) / len(confidences)
    if mean_confidence < LPR_MIN_CONFIDENCE:
        return None
    if not is_plausible_plate(text):
        return None
    return text, round(mean_confidence, 1)


def _is_real_confidence(value) -> bool:
    try:
        return float(value) >= 0
    except (TypeError, ValueError):
        return False


def recognize_plate(vehicle_crop_bgr, *, camera_number: int | None = None):
    """The one entry point main.py calls: vehicle crop in, plate
    result out (or None). Detects the plate region, then reads it --
    every failure mode (cascade unavailable, no region found, OCR
    failed, read implausible) returns None uniformly, so the caller
    never needs its own branching for "why didn't this work"."""
    if not LPR_ENABLED:
        return None
    if camera_number is not None and not is_camera_enabled(camera_number):
        return None
    region = detect_plate_region(vehicle_crop_bgr)
    if region is None:
        return None
    x, y, w, h = region
    plate_crop = vehicle_crop_bgr[y : y + h, x : x + w]
    result = read_plate_text(plate_crop)
    if result is None:
        return None
    text, confidence = result
    return {"plate_number": text, "confidence": confidence, "region": region}


_LPR_CAMERAS_RAW = os.environ.get("ANYAICAM_LPR_CAMERAS")
LPR_CAMERAS = (
    frozenset(int(value) for value in _LPR_CAMERAS_RAW.split(",") if value.strip())
    if _LPR_CAMERAS_RAW
    else None
)


def is_camera_enabled(camera_number: int) -> bool:
    """None (the default) means unrestricted -- every camera that
    reaches this module already passed ANALYTICS_DETECTION_CAMERAS in
    main.py, so LPR is scoped no further than that unless this env var
    is explicitly set (deployed as 1,2,3,4, excluding Camera 5, same
    as every other analytic this session)."""
    return LPR_CAMERAS is None or camera_number in LPR_CAMERAS
