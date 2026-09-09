"""Safe stdout contract for explicitly authorized local LPR validation.

This module deliberately contains no camera, OpenCV, PaddleOCR, network, or
cloud dependency.  A disposable edge runner may populate ``LprValidationResult``
after it has processed an in-memory frame, then use ``stdout_json`` as its one
machine-readable final line.  Keeping the contract independent makes OCR
engines replaceable and prevents accidental RTSP or credential logging.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from typing import Any


class ValidationResultError(ValueError):
    """Raised when a runner attempts to publish malformed or unsafe metrics."""


def _finite_nonnegative(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValidationResultError(f"{field} must be numeric.")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValidationResultError(f"{field} must be numeric.") from error
    if not math.isfinite(number) or number < 0:
        raise ValidationResultError(f"{field} must be finite and non-negative.")
    return number


def _safe_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64 or "\x00" in value:
        raise ValidationResultError(f"{field} is invalid.")
    # A result line must never become a vehicle-frame, path, URL, or credential
    # transport.  Plates are compact alphanumeric OCR output only.
    if any(marker in value.lower() for marker in ("://", "@", "/", "\\")):
        raise ValidationResultError(f"{field} must not contain a URL, path, or credential.")
    return value


@dataclass(frozen=True)
class LprValidationResult:
    """The sole final stdout payload for one local static/moving observation."""

    label: str
    detector_candidate_count: int
    crop_width: int | None
    crop_height: int | None
    raw_ocr_text: str | None
    normalized_plate: str | None
    ocr_confidence: float | None
    detector_latency_ms: float
    ocr_latency_ms: float
    total_pipeline_latency_ms: float

    def __post_init__(self) -> None:
        if self.label not in {"STATIC_SANITY_CHECK", "MOVING_VEHICLE_OBSERVATION"}:
            raise ValidationResultError("Unknown validation label.")
        if isinstance(self.detector_candidate_count, bool) or self.detector_candidate_count < 0:
            raise ValidationResultError("detector_candidate_count must be non-negative.")
        for field in ("crop_width", "crop_height"):
            value = getattr(self, field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise ValidationResultError(f"{field} must be a positive integer or null.")
        if (self.crop_width is None) != (self.crop_height is None):
            raise ValidationResultError("Crop dimensions must both be present or null.")
        _safe_text(self.raw_ocr_text, "raw_ocr_text")
        _safe_text(self.normalized_plate, "normalized_plate")
        if self.ocr_confidence is not None:
            confidence = _finite_nonnegative(self.ocr_confidence, "ocr_confidence")
            if confidence > 1:
                raise ValidationResultError("ocr_confidence must be between zero and one.")
        detector = _finite_nonnegative(self.detector_latency_ms, "detector_latency_ms")
        ocr = _finite_nonnegative(self.ocr_latency_ms, "ocr_latency_ms")
        total = _finite_nonnegative(self.total_pipeline_latency_ms, "total_pipeline_latency_ms")
        if total + 0.001 < detector + ocr:
            raise ValidationResultError("total_pipeline_latency_ms cannot be below detector plus OCR latency.")

    def stdout_json(self) -> str:
        """Return a stable one-line JSON record safe for SSH stdout capture."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
