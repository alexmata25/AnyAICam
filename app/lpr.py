"""License Plate Recognition: given a vehicle already detected by the
existing YOLO pipeline (ai_person_detector()/save_yolo_events() in
main.py), locates a candidate plate region inside that vehicle's own
crop and reads its text -- purely local, purely offline, no network
calls and no third-party service.

The OCR dependency is optional and loaded only when enabled LPR is
actually invoked.  Core VMS startup must not depend on either the
``pytesseract`` package or the Tesseract executable.

Every public function is exception-safe by design (returns None on any
failure) -- a plate that can't be found or read must never interrupt
the caller's own detection/recording pipeline.
"""

import os
import re
import time

import cv2

LPR_ENABLED = os.environ.get("ANYAICAM_LPR_ENABLED", "false").strip().lower() == "true"

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
_pytesseract = None
_ocr_load_attempted = False
_ocr_unavailable_reason = None


def _get_pytesseract():
    """Load and validate the optional local OCR adapter on first use."""
    global _pytesseract, _ocr_load_attempted, _ocr_unavailable_reason
    if _ocr_load_attempted:
        return _pytesseract
    _ocr_load_attempted = True
    try:
        import pytesseract as adapter

        adapter.get_tesseract_version()
        _pytesseract = adapter
        _ocr_unavailable_reason = None
    except Exception as error:
        _pytesseract = None
        _ocr_unavailable_reason = str(error) or error.__class__.__name__
    return _pytesseract


def capability() -> dict:
    """Return an explicit, non-throwing LPR capability state."""
    if not LPR_ENABLED:
        return {"enabled": False, "available": False, "reason": "LPR is disabled"}
    available = _get_pytesseract() is not None
    return {
        "enabled": True,
        "available": available,
        "reason": None if available else f"LPR unavailable: {_ocr_unavailable_reason}",
    }


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
    global _cascade, _cascade_load_failed, _pytesseract, _ocr_load_attempted, _ocr_unavailable_reason
    _cascade = None
    _cascade_load_failed = False
    _pytesseract = None
    _ocr_load_attempted = False
    _ocr_unavailable_reason = None


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
    """Returns (x, y, w, h) of the best candidate plate region inside an
    already-cropped vehicle image, or None. The licence-plate detector
    model is used when present (see detect_plate() below); the Haar
    cascade otherwise."""
    detection = detect_plate(vehicle_crop_bgr)
    return detection["region"] if detection else None


def _detect_plate_region_cascade(vehicle_crop_bgr):
    """Haar-cascade fallback: picks the widest match (a plate is
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
    if not LPR_ENABLED or plate_crop_bgr is None or plate_crop_bgr.size == 0:
        return None
    adapter = _get_pytesseract()
    if adapter is None:
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
        data = adapter.image_to_data(
            thresholded, config=config, output_type=adapter.Output.DICT
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
    if vehicle_crop_bgr is None or getattr(vehicle_crop_bgr, "size", 0) == 0:
        return None
    if vehicle_crop_bgr.shape[1] < LPR_MIN_VEHICLE_WIDTH:
        return None  # too few pixels on this vehicle for any plate to be legible
    detection = detect_plate(vehicle_crop_bgr)
    if detection is None:
        return None
    x, y, w, h = detection["region"]
    plate_crop = vehicle_crop_bgr[y : y + h, x : x + w]
    result = read_plate_glyphs(plate_crop) or read_plate_text(plate_crop)
    if result is None:
        return None
    text, confidence = result
    confidence = round(min(100.0, confidence + 20.0 * detection["detector_confidence"]), 1)
    if confidence < LPR_MIN_CONFIDENCE:
        return None
    return {"plate_number": text, "confidence": confidence, "region": detection["region"],
            "detector_confidence": round(detection["detector_confidence"], 3), "detector": detection["source"]}


# ---------------------------------------------------------------- real-world plate pipeline (2026-09-27)
# Validated on this deployment's own cameras: the Haar cascade above
# (trained on Russian plates, ~4.6:1, black on white) found NO plate in
# 20,000+ vehicle detections, including a clearly readable US plate. The
# pipeline below is general -- nothing in it knows any plate's value:
#  1. a YOLO licence-plate detector (pinned model baked into the image,
#     /opt/anyaicam-lpr-model) finds the plate in the vehicle crop; the
#     cascade stays as a fallback when the model file is absent;
#  2. read_plate_glyphs() isolates the row of character-sized blobs, fits a
#     line through their centres and rotates by its exact slope (vehicles
#     are rarely square to the camera), removes the perspective lean
#     (shear), and OCRs an image containing only those glyphs -- no plate
#     frame, state name or bolts;
#  3. confirm_plate() reports a plate only when the same text was read at
#     least LPR_CONFIRM_READS times on that camera within a short window,
#     and not again for the same plate within LPR_REPEAT_COOLDOWN_SECONDS
#     (a parked car is detected every few seconds).
import math
import threading

import numpy as np

LPR_PLATE_MODEL_PATH = os.environ.get("ANYAICAM_LPR_PLATE_MODEL", "/opt/anyaicam-lpr-model/license-plate-finetune-v1n.pt")
LPR_PLATE_MIN_DETECTOR_CONFIDENCE = float(os.environ.get("ANYAICAM_LPR_PLATE_MIN_DETECTOR_CONFIDENCE", "0.35"))
LPR_MIN_VEHICLE_WIDTH = max(0, int(os.environ.get("ANYAICAM_LPR_MIN_VEHICLE_WIDTH", "220")))
LPR_CONFIRM_READS = max(1, int(os.environ.get("ANYAICAM_LPR_CONFIRM_READS", "2")))
LPR_VOTE_WINDOW_SECONDS = max(1.0, float(os.environ.get("ANYAICAM_LPR_VOTE_WINDOW_SECONDS", "90")))
LPR_REPEAT_COOLDOWN_SECONDS = max(0.0, float(os.environ.get("ANYAICAM_LPR_REPEAT_COOLDOWN_SECONDS", "600")))
_OCR_WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

_plate_model = None
_plate_model_failed = False
_plate_model_lock = threading.Lock()
_votes_lock = threading.Lock()
_votes: dict = {}      # camera -> [(monotonic, text)]
_emitted: dict = {}    # (camera, text) -> monotonic time last reported


def reset_pipeline_state() -> None:
    """Test-only: forget the plate model and the voting/cooldown history."""
    global _plate_model, _plate_model_failed
    with _plate_model_lock:
        _plate_model, _plate_model_failed = None, False
    with _votes_lock:
        _votes.clear()
        _emitted.clear()
    with _full_frame_lock:
        _full_frame_cache.clear()
    with _tracks_lock:
        _tracks.clear()


def _get_plate_model():
    global _plate_model, _plate_model_failed
    with _plate_model_lock:
        if _plate_model is not None or _plate_model_failed:
            return _plate_model
        try:
            if not os.path.isfile(LPR_PLATE_MODEL_PATH):
                raise FileNotFoundError(LPR_PLATE_MODEL_PATH)
            from ultralytics import YOLO
            _plate_model = YOLO(LPR_PLATE_MODEL_PATH)
        except Exception:
            _plate_model_failed = True
            _plate_model = None
        return _plate_model


def detect_plate(vehicle_crop_bgr):
    """{'region': (x, y, w, h), 'detector_confidence': float, 'source': 'model'|'cascade'} or None."""
    if vehicle_crop_bgr is None or getattr(vehicle_crop_bgr, "size", 0) == 0:
        return None
    model = _get_plate_model()
    if model is not None:
        try:
            result = model(vehicle_crop_bgr, verbose=False, conf=LPR_PLATE_MIN_DETECTOR_CONFIDENCE)[0]
            boxes = list(zip(result.boxes.xyxy.tolist(), result.boxes.conf.tolist()))
        except Exception:
            boxes = []
        if boxes:
            (x1, y1, x2, y2), confidence = max(boxes, key=lambda item: item[1])
            h, w = vehicle_crop_bgr.shape[:2]
            pad = 6
            x1, y1 = max(0, int(x1) - pad), max(0, int(y1) - pad)
            x2, y2 = min(w, int(x2) + pad), min(h, int(y2) + pad)
            return {"region": (x1, y1, x2 - x1, y2 - y1), "detector_confidence": float(confidence), "source": "model"}
    region = _detect_plate_region_cascade(vehicle_crop_bgr)
    if region is None:
        return None
    return {"region": region, "detector_confidence": 0.0, "source": "cascade"}


def _glyph_blobs(gray):
    enhanced = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4)).apply(gray)
    binary = cv2.adaptiveThreshold(enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 10)
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
    height, _ = gray.shape
    blobs = []
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        if 0.10 * height <= h <= 0.65 * height and 0.12 <= w / float(h) <= 1.2 and area >= 0.15 * w * h and h >= 8:
            blobs.append((index, x, y, w, h, centroids[index][0], centroids[index][1]))
    return labels, blobs


def _character_row(blobs):
    """The largest set of similar-height blobs whose centres lie on one line."""
    best = []
    for a in blobs:
        for b in blobs:
            if b[5] <= a[5] or abs(b[4] - a[4]) > 0.35 * max(a[4], b[4]):
                continue
            slope = (b[6] - a[6]) / (b[5] - a[5])
            if abs(slope) > math.tan(math.radians(40)):
                continue
            height = (a[4] + b[4]) / 2
            row = [c for c in blobs if abs(c[4] - height) <= 0.35 * height and abs((a[6] + slope * (c[5] - a[5])) - c[6]) <= 0.25 * height]
            if len(row) > len(best):
                best = row
    if best:
        median = float(np.median([c[4] for c in best]))
        best = [c for c in best if c[4] >= 0.7 * median]  # short marks (dashes, bolts) are not characters
    return best


def _glyph_image(labels, row, shape):
    mask = np.zeros(shape, dtype=np.uint8)
    for blob in row:
        mask[labels == blob[0]] = 255
    xs = np.array([c[5] for c in row])
    ys = np.array([c[6] for c in row])
    angle = math.degrees(math.atan(np.polyfit(xs, ys, 1)[0]))
    height, width = shape
    rotation = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    rotated = cv2.warpAffine(mask, rotation, (width, height), flags=cv2.INTER_NEAREST)
    ys_, xs_ = np.where(rotated > 0)
    if len(xs_) == 0:
        return None
    crop = rotated[ys_.min():ys_.max() + 1, xs_.min():xs_.max() + 1]
    ch = crop.shape[0]
    padded = cv2.copyMakeBorder(crop, 0, 0, ch, ch, cv2.BORDER_CONSTANT, value=0)
    best_shear, best_score = 0.0, -1.0
    for shear in np.arange(-0.7, 0.71, 0.05):
        matrix = np.float32([[1, shear, -shear * ch / 2], [0, 1, 0]])
        sheared = cv2.warpAffine(padded, matrix, (padded.shape[1], ch), flags=cv2.INTER_NEAREST)
        columns = (sheared > 0).sum(axis=0).astype(float)
        score = float((columns ** 2).sum())
        if score > best_score:
            best_shear, best_score = float(shear), score
    matrix = np.float32([[1, best_shear, -best_shear * ch / 2], [0, 1, 0]])
    crop = cv2.warpAffine(padded, matrix, (padded.shape[1], ch), flags=cv2.INTER_NEAREST)
    _, xs2 = np.where(crop > 0)
    crop = crop[:, xs2.min():xs2.max() + 1]
    crop = cv2.resize(crop, None, fx=48.0 / crop.shape[0], fy=48.0 / crop.shape[0], interpolation=cv2.INTER_AREA)
    _, crop = cv2.threshold(crop, 127, 255, cv2.THRESH_BINARY)
    return cv2.copyMakeBorder(255 - crop, 12, 12, 16, 16, cv2.BORDER_CONSTANT, value=255)


def read_plate_glyphs(plate_crop_bgr):
    """(normalized_text, confidence 0-100) or None. Confidence combines
    agreement between two OCR segmentation modes and whether the text
    length matches the number of character glyphs found (Tesseract's own
    word confidence is not meaningful on these glyph-only images)."""
    if not LPR_ENABLED or plate_crop_bgr is None or getattr(plate_crop_bgr, "size", 0) == 0:
        return None
    adapter = _get_pytesseract()
    if adapter is None:
        return None
    try:
        gray = cv2.cvtColor(plate_crop_bgr, cv2.COLOR_BGR2GRAY)
        scale = 240.0 / max(gray.shape[1], 1)
        if scale > 1:
            gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        labels, blobs = _glyph_blobs(gray)
        row = _character_row(blobs)
        if len(row) < LPR_MIN_PLATE_LENGTH - 1:
            return None
        image = _glyph_image(labels, row, gray.shape)
        if image is None:
            return None
        reads = []
        for psm in (7, 8):
            text = adapter.image_to_string(image, config=f"--psm {psm} -c tessedit_char_whitelist={_OCR_WHITELIST}")
            reads.append(normalize_plate_text(text))
    except Exception:
        return None
    plausible = [text for text in reads if is_plausible_plate(text)]
    if not plausible:
        return None
    text = plausible[0]
    confidence = (45.0 if reads[0] == reads[1] else 20.0) + (35.0 if len(text) == len(row) else 10.0)
    return text, round(confidence, 1)


def confirm_plate(camera_number, result, *, now: float | None = None):
    """Report a per-frame read only once it is corroborated: the same text
    read LPR_CONFIRM_READS times on this camera within
    LPR_VOTE_WINDOW_SECONDS, and not already reported for this camera
    within LPR_REPEAT_COOLDOWN_SECONDS. Returns the result or None."""
    if not result:
        return None
    text = result.get("plate_number")
    if not text:
        return None
    now = time.monotonic() if now is None else now
    key = camera_number if camera_number is not None else "_"
    with _votes_lock:
        recent = [(t, v) for t, v in _votes.get(key, []) if now - t <= LPR_VOTE_WINDOW_SECONDS]
        recent.append((now, text))
        _votes[key] = recent[-50:]
        agreeing = sum(1 for _, v in recent if v == text)
        if agreeing < LPR_CONFIRM_READS:
            return None
        last = _emitted.get((key, text))
        if last is not None and now - last < LPR_REPEAT_COOLDOWN_SECONDS:
            return None
        _emitted[(key, text)] = now
    confirmed = dict(result)
    confirmed["confirmations"] = agreeing
    return confirmed



# ---------------------------------------------------------------- full-resolution frames for LPR (2026-09-27)
# Many cameras are analysed on their 1280x720 substream (live view/HLS)
# while recording keeps the full-resolution main stream in the rolling
# pre-event buffer. On a real 720p substream a plate is a few dozen pixels
# and "6" reads as "G"; the same plate reads correctly at native
# resolution. So when a vehicle is detected on a downscaled analytics
# frame, LPR takes the newest frame from the camera's own recording buffer
# (the file ffmpeg is already writing -- no extra camera session, ~0.8 s,
# about 3 s behind real time), cached briefly per camera.
import subprocess

LPR_FULL_RES_FRAMES = os.environ.get("ANYAICAM_LPR_FULL_RES_FRAMES", "true").strip().lower() == "true"
LPR_FULL_RES_CACHE_SECONDS = max(0.0, float(os.environ.get("ANYAICAM_LPR_FULL_RES_CACHE_SECONDS", "2.5")))
_full_frame_cache: dict = {}  # camera -> (monotonic, frame)
_full_frame_lock = threading.Lock()


def latest_full_resolution_frame(camera_number, buffer_dir, *, now: float | None = None):
    """Newest decoded frame of the camera's recording buffer, or None."""
    if not LPR_FULL_RES_FRAMES:
        return None
    now = time.monotonic() if now is None else now
    with _full_frame_lock:
        cached = _full_frame_cache.get(camera_number)
        if cached and now - cached[0] <= LPR_FULL_RES_CACHE_SECONDS:
            return cached[1]
    try:
        segments = sorted((p for p in os.scandir(buffer_dir) if p.name.endswith(".mkv")), key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    frame = None
    for entry in reversed(segments[-2:]):  # the in-progress segment, else the previous one
        try:
            raw = subprocess.run(
                ["ffmpeg", "-v", "error", "-sseof", "-1.5", "-i", entry.path, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
                capture_output=True, timeout=8,
            ).stdout
            if raw:
                frame = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        except (OSError, subprocess.SubprocessError):
            frame = None
        if frame is not None:
            break
    with _full_frame_lock:
        _full_frame_cache[camera_number] = (now, frame)
    return frame


def full_resolution_vehicle_crop(analytics_frame, box, full_frame, *, margin: float = 0.15):
    """Map a vehicle box (x, y, w, h) on the analytics frame onto the
    full-resolution frame, widened by `margin` for a vehicle still moving.
    Returns the full-resolution crop, or None when it would not add pixels."""
    if analytics_frame is None or full_frame is None:
        return None
    ah, aw = analytics_frame.shape[:2]
    fh, fw = full_frame.shape[:2]
    if fw < aw * 1.3:
        return None  # the analytics frame is already (near) full resolution
    sx, sy = fw / float(aw), fh / float(ah)
    x, y, w, h = box
    mx, my = w * margin, h * margin
    x1, y1 = max(0, int((x - mx) * sx)), max(0, int((y - my) * sy))
    x2, y2 = min(fw, int((x + w + mx) * sx)), min(fh, int((y + h + my) * sy))
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None
    return full_frame[y1:y2, x1:x2]


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

# ---------------------------------------------------------------- parked-vehicle suppression (2026-09-27)
# A parked car is detected every analytics cycle. Each distinct vehicle
# position gets up to LPR_ATTEMPTS_PER_VEHICLE read attempts; after that
# (or once its plate was confirmed) an unmoved vehicle -- box overlapping
# the earlier one by LPR_STATIONARY_IOU -- is skipped for
# LPR_STATIONARY_SKIP_SECONDS. A vehicle that moves is a new position.
LPR_ATTEMPTS_PER_VEHICLE = max(1, int(os.environ.get("ANYAICAM_LPR_ATTEMPTS_PER_VEHICLE", "4")))
LPR_STATIONARY_IOU = float(os.environ.get("ANYAICAM_LPR_STATIONARY_IOU", "0.85"))
LPR_STATIONARY_SKIP_SECONDS = max(0.0, float(os.environ.get("ANYAICAM_LPR_STATIONARY_SKIP_SECONDS", "300")))
_tracks: dict = {}  # camera -> [{"box", "first", "attempts", "done"}]
_tracks_lock = threading.Lock()


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / float(union) if union else 0.0


def should_attempt(camera_number, box, *, now: float | None = None) -> bool:
    """Record an attempt for this vehicle box unless it is a parked vehicle
    already read enough times; True means: run LPR on it."""
    now = time.monotonic() if now is None else now
    with _tracks_lock:
        tracks = [t for t in _tracks.get(camera_number, []) if now - t["first"] <= LPR_STATIONARY_SKIP_SECONDS]
        _tracks[camera_number] = tracks
        for track in tracks:
            if _iou(track["box"], box) >= LPR_STATIONARY_IOU:
                if track["done"] or track["attempts"] >= LPR_ATTEMPTS_PER_VEHICLE:
                    return False
                track["attempts"] += 1
                track["box"] = box
                return True
        tracks.append({"box": box, "first": now, "attempts": 1, "done": False})
        return True


def mark_vehicle_read(camera_number, box) -> None:
    """A plate was confirmed for this vehicle position: stop re-reading it."""
    with _tracks_lock:
        for track in _tracks.get(camera_number, []):
            if _iou(track["box"], box) >= LPR_STATIONARY_IOU:
                track["done"] = True
