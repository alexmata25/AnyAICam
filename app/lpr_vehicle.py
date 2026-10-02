"""Vehicle details for a license-plate read (2026-09-30).

The LPR table shows, per plate: the vehicle's type, color, make and model.
Only what is actually known is reported; everything else is None and the
customer sees "Unknown" -- never a guess:
- type: the YOLO class of the vehicle box the plate was read in (car,
  truck, bus, motorcycle) -- the detector's own answer;
- color: estimate_vehicle_color() below, which answers only when one color
  clearly dominates the vehicle body in a real-color frame (IR/night frames
  are monochrome and always Unknown);
- make / model: no make/model classifier is installed, so always None. The
  fields and VEHICLE_ATTRIBUTE_MIN_CONFIDENCE are in place for one.

Pure functions over numpy images; OpenCV is imported lazily."""
from __future__ import annotations

from typing import Optional

# A vehicle attribute below this confidence is shown as Unknown (edge and
# cloud apply the same threshold).
VEHICLE_ATTRIBUTE_MIN_CONFIDENCE = 0.6
VEHICLE_CLASSES = ("car", "truck", "bus", "motorcycle")
# Frames whose colour channels (almost) agree everywhere are IR/night or
# otherwise monochrome: no colour can be read from them.
MONOCHROME_CHANNEL_SPREAD = 6.0
MIN_BODY_PIXELS = 400


def vehicle_type_for(vehicle_box, detections) -> Optional[str]:
    """The class of the detection whose box the plate was read in."""
    if not vehicle_box:
        return None
    box = tuple(int(v) for v in vehicle_box)
    for detection in detections or []:
        try:
            candidate = (int(detection["x"]), int(detection["y"]), int(detection["width"]), int(detection["height"]))
        except (KeyError, TypeError, ValueError):
            continue
        name = str(detection.get("class_name") or "").lower()
        if candidate == box and name in VEHICLE_CLASSES:
            return name
    return None


def _bucket(h: int, s: int, v: int) -> str:
    """OpenCV HSV (H 0-179, S/V 0-255) -> a customer colour name."""
    if v < 50:
        return "black"
    if s < 45:
        return "white" if v > 200 else "gray"
    if h < 8 or h >= 165:
        return "red"
    if h < 20:
        return "orange"
    if h < 33:
        return "yellow"
    if h < 85:
        return "green"
    if h < 130:
        return "blue"
    return "purple"


COLOR_LABELS = {"black": "Black", "white": "White", "gray": "Gray/Silver", "red": "Red", "orange": "Orange",
                "yellow": "Yellow", "green": "Green", "blue": "Blue", "purple": "Purple"}


def estimate_vehicle_color(vehicle_crop_bgr) -> tuple[Optional[str], float]:
    """(colour label, share of body pixels) or (None, share) when no colour
    clearly dominates. Samples the body band of the box (between the roof /
    windows and the road) so glass, sky and tarmac weigh less."""
    try:
        import numpy as np
        import cv2
    except ImportError:
        return None, 0.0
    if vehicle_crop_bgr is None or getattr(vehicle_crop_bgr, "ndim", 0) != 3:
        return None, 0.0
    height, width = vehicle_crop_bgr.shape[:2]
    body = vehicle_crop_bgr[int(height * 0.35):int(height * 0.8), int(width * 0.15):int(width * 0.85)]
    if body.size == 0 or body.shape[0] * body.shape[1] < MIN_BODY_PIXELS:
        return None, 0.0
    pixels = body.reshape(-1, 3).astype(np.int16)
    spread = float(np.mean(np.abs(pixels[:, 0] - pixels[:, 1]) + np.abs(pixels[:, 1] - pixels[:, 2])))
    if spread < MONOCHROME_CHANNEL_SPREAD:
        return None, 0.0
    hsv = cv2.cvtColor(body, cv2.COLOR_BGR2HSV).reshape(-1, 3)
    counts: dict[str, int] = {}
    for h, s, v in hsv[:: max(1, len(hsv) // 4000)]:
        name = _bucket(int(h), int(s), int(v))
        counts[name] = counts.get(name, 0) + 1
    total = sum(counts.values())
    name, top = max(counts.items(), key=lambda item: item[1])
    share = round(top / total, 2) if total else 0.0
    if share < VEHICLE_ATTRIBUTE_MIN_CONFIDENCE:
        return None, share
    return COLOR_LABELS[name], share


def reliable(value, confidence) -> Optional[str]:
    """value only when its confidence meets the threshold, else None."""
    try:
        return value if value and float(confidence) >= VEHICLE_ATTRIBUTE_MIN_CONFIDENCE else None
    except (TypeError, ValueError):
        return None
