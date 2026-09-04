"""PPE Detection: given a person already detected by the existing YOLO
pipeline (ai_person_detector()/save_yolo_events() in main.py), runs a
second, dedicated local model over that person's own crop to check for
a hard hat and a safety vest.

Model: Tanishjain9/yolov8n-ppe-detection-6classes (Hugging Face),
MIT-licensed weights, YOLOv8-nano, trained from a COCO-pretrained base
via the standard Ultralytics framework -- the same framework
get_yolo_model() in main.py already loads the base person/vehicle
model through. Six classes: Gloves, Vest, goggles, helmet, mask,
safety_shoe -- only helmet/Vest are used for the two required checks
here; the rest are ignored, not deleted, so extending PPE_REQUIRED_CLASSES
later needs no code change.

Deliberately dependency-light in the same sense lpr.py/smart_motion.py
are: no import of `main`. Every public function is exception-safe --
a model that fails to load, or a frame it can't process, returns None,
never raises -- matching the requirement that PPE detection must never
interrupt recording/live view/relay/People Counting/Smart Motion.
"""

import os

from ultralytics import YOLO

PPE_ENABLED = os.environ.get("ANYAICAM_PPE_ENABLED", "true").strip().lower() == "true"

PPE_MODEL_NAME = os.environ.get("ANYAICAM_PPE_MODEL", "yolov8n-ppe.pt")

# Ultralytics confidence is 0-1 (unlike tesseract's 0-100 in lpr.py) --
# kept in that native range rather than rescaling, so it reads
# correctly against the model's own reported box confidences.
PPE_MIN_CONFIDENCE = max(0.0, min(1.0, float(os.environ.get("ANYAICAM_PPE_MIN_CONFIDENCE", "0.4"))))

HARD_HAT_CLASS_NAME = "helmet"
VEST_CLASS_NAME = "vest"

_model = None
_model_load_failed = False


def _get_model():
    """Lazy singleton, matching get_yolo_model()'s own pattern in
    main.py -- the model file is read from disk once, on first real
    use, not at import time, so importing this module never touches
    the filesystem or blocks startup."""
    global _model, _model_load_failed
    if _model is not None or _model_load_failed:
        return _model
    try:
        _model = YOLO(PPE_MODEL_NAME)
    except Exception:
        _model_load_failed = True
        _model = None
    return _model


def reset_state() -> None:
    """Test-only: clears the lazy-loaded model singleton."""
    global _model, _model_load_failed
    _model = None
    _model_load_failed = False


_PPE_CAMERAS_RAW = os.environ.get("ANYAICAM_PPE_CAMERAS")
PPE_CAMERAS = (
    frozenset(int(value) for value in _PPE_CAMERAS_RAW.split(",") if value.strip())
    if _PPE_CAMERAS_RAW
    else None
)


def is_camera_enabled(camera_number: int) -> bool:
    """None (the default) means unrestricted -- deployed as 1,2,3,4,
    excluding Camera 5, same convention as smart_motion/lpr."""
    return PPE_CAMERAS is None or camera_number in PPE_CAMERAS


def _parse_detections(results) -> list[dict]:
    """Pure helper, split out for testability: turns one ultralytics
    Results object into the same {class_name, confidence} shape the
    rest of this module (and main.py's own save_yolo_events()) already
    uses for detections."""
    detections = []
    boxes = getattr(results, "boxes", None)
    if boxes is None:
        return detections
    names = results.names or {}
    for class_index, confidence in zip(boxes.cls.tolist(), boxes.conf.tolist()):
        class_name = str(names.get(int(class_index), int(class_index))).lower()
        detections.append({"class_name": class_name, "confidence": round(float(confidence), 4)})
    return detections


def summarize_ppe(detections: list[dict]) -> dict:
    """Pure function: real detections in, hard_hat_present/
    safety_vest_present/confidence out. Split from detect_ppe() so the
    decision logic (what counts as "present", how confidence is
    summarized) is testable without a real model or image."""
    hard_hat_hits = [d["confidence"] for d in detections if d["class_name"] == HARD_HAT_CLASS_NAME]
    vest_hits = [d["confidence"] for d in detections if d["class_name"] == VEST_CLASS_NAME]
    relevant = hard_hat_hits + vest_hits
    confidence = round(max(relevant), 4) if relevant else 0.0
    return {
        "hard_hat_present": bool(hard_hat_hits),
        "safety_vest_present": bool(vest_hits),
        "confidence": confidence,
        "detections": detections,
    }


def detect_ppe(person_crop_bgr, *, camera_number: int | None = None):
    """The one entry point main.py calls: a person's own crop in, a
    PPE summary dict out (or None on any failure/disabled/out-of-scope
    condition -- uniform so the caller never needs its own branching
    for "why didn't this work")."""
    if not PPE_ENABLED:
        return None
    if camera_number is not None and not is_camera_enabled(camera_number):
        return None
    if person_crop_bgr is None or person_crop_bgr.size == 0:
        return None
    model = _get_model()
    if model is None:
        return None
    try:
        results = model.predict(person_crop_bgr, verbose=False, conf=PPE_MIN_CONFIDENCE)
    except Exception:
        return None
    if not results:
        return None
    return summarize_ppe(_parse_detections(results[0]))
