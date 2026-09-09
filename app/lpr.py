"""Pure CPU-only License Plate Recognition event foundation.

This module has no camera, network, cloud, GPU, OpenCV, YOLO, or OCR-engine
dependency.  An appliance integration supplies frames to a PlateLocalizer and
an OcrAdapter, then sends the resulting payload through the existing
authenticated appliance analytics endpoint.  Keeping those boundaries here
makes LPR testable and prevents this feature from bypassing tenant controls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import math
from time import perf_counter
from typing import Any, Iterable, Protocol


ANALYTIC_KEY = "lpr"
EVENT_TYPE = "lpr"
RELAY_ACTIONS: tuple[str, ...] = ()  # LPR has no access-control or relay actions.


class LprValidationError(ValueError):
    """Raised when untrusted OCR or candidate data cannot form an LPR event."""


@dataclass(frozen=True)
class LprConfig:
    min_plate_length: int = 3
    max_plate_length: int = 12
    min_confidence: float = 0.60
    debounce_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.min_plate_length < 1 or self.max_plate_length < self.min_plate_length:
            raise LprValidationError("Invalid plate-length bounds.")
        validate_confidence(self.min_confidence)
        if not math.isfinite(self.debounce_seconds) or self.debounce_seconds < 0:
            raise LprValidationError("debounce_seconds must be a finite non-negative number.")


@dataclass(frozen=True)
class PlateCandidate:
    """An opaque plate crop/candidate supplied by a future localizer adapter."""

    image: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    snapshot_reference: str | None = None
    plate_crop_reference: str | None = None


@dataclass(frozen=True)
class OcrResult:
    text: str
    confidence: float
    engine: str
    engine_version: str | None = None


class OcrAdapter(Protocol):
    """Pluggable CPU OCR boundary. Implementations must not require a GPU."""

    def recognize(self, candidate: PlateCandidate) -> OcrResult:
        ...


class PlateLocalizer(Protocol):
    """Optional vehicle/plate localization boundary for edge frame adapters."""

    def localize(self, frame: Any, vehicle_candidates: Iterable[Any] | None = None) -> Iterable[PlateCandidate]:
        ...


class StaticOcrAdapter:
    """Test-only adapter; never performs camera, network, or GPU work."""

    def __init__(self, result: OcrResult):
        self.result = result

    def recognize(self, candidate: PlateCandidate) -> OcrResult:
        return self.result


def normalize_plate(raw_text: str, *, min_length: int = 3, max_length: int = 12) -> str:
    """Return deterministic uppercase plate text without inventing characters.

    Whitespace and non-alphanumeric separators are removed.  Characters are
    never substituted (for example, O is never changed to 0); empty and
    out-of-range values are rejected.
    """
    if not isinstance(raw_text, str):
        raise LprValidationError("Plate text must be a string.")
    if min_length < 1 or max_length < min_length:
        raise LprValidationError("Invalid plate-length bounds.")
    normalized = "".join(character for character in raw_text.strip().upper() if character.isalnum())
    if not normalized:
        raise LprValidationError("Plate text contains no alphanumeric characters.")
    if not min_length <= len(normalized) <= max_length:
        raise LprValidationError("Normalized plate length is outside configured bounds.")
    return normalized


def validate_confidence(value: Any) -> float:
    if isinstance(value, bool):
        raise LprValidationError("Confidence must be numeric, not boolean.")
    try:
        confidence = float(value)
    except (TypeError, ValueError) as error:
        raise LprValidationError("Confidence must be numeric.") from error
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise LprValidationError("Confidence must be finite and between 0 and 1.")
    return confidence


def safe_media_reference(value: str | None) -> str | None:
    """Validate an opaque, single-segment media key; never accept a path/URL."""
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 255:
        raise LprValidationError("Media reference must be a non-empty short string.")
    if value in {".", ".."} or ".." in value or "/" in value or "\\" in value or "\x00" in value:
        raise LprValidationError("Media references must not contain paths or traversal.")
    if not all(character.isalnum() or character in "._-" for character in value):
        raise LprValidationError("Media reference contains unsupported characters.")
    return value


def active_lpr_entitlement(rows: Iterable[dict[str, Any]]) -> bool:
    """Checks a single camera's already-authorized entitlement configuration."""
    return any(row.get("analytic_key") == ANALYTIC_KEY and row.get("status") == "active" for row in rows)


def _timestamp(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise LprValidationError("Timestamp must be ISO-8601.") from error
    else:
        raise LprValidationError("Timestamp must be a datetime or ISO-8601 string.")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _event_id(camera_id: str, normalized_plate: str, timestamp: datetime, engine: str) -> str:
    material = f"{camera_id}\x1f{normalized_plate}\x1f{timestamp.isoformat()}\x1f{engine}".encode()
    return "lpr-" + sha256(material).hexdigest()[:24]


@dataclass(frozen=True)
class LprEvent:
    camera_id: str
    local_event_id: str
    timestamp: str
    plate: str
    normalized_plate: str
    confidence: float
    ocr_engine: str
    ocr_engine_version: str | None
    snapshot_reference: str | None = None
    plate_crop_reference: str | None = None
    candidate_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def event_type(self) -> str:
        return EVENT_TYPE

    def analytics_payload(self) -> dict[str, Any]:
        """Payload accepted by the existing appliance analytics endpoint.

        camera_id remains in the URL path at upload time. It is intentionally
        not trusted from this payload by the cloud endpoint.
        """
        details: dict[str, Any] = {
            "plate": self.plate,
            "normalized_plate": self.normalized_plate,
            "ocr_engine": self.ocr_engine,
            "ocr_engine_version": self.ocr_engine_version,
            "candidate": self.candidate_metadata,
        }
        if self.snapshot_reference:
            details["snapshot_reference"] = self.snapshot_reference
        if self.plate_crop_reference:
            details["plate_crop_reference"] = self.plate_crop_reference
        return {
            "local_event_id": self.local_event_id,
            "event_type": EVENT_TYPE,
            "confidence": self.confidence,
            "object_count": 1,
            "detections": details,
            "event_timestamp": self.timestamp,
        }


def build_event(*, camera_id: str, timestamp: datetime | str, ocr: OcrResult,
                candidate: PlateCandidate, config: LprConfig = LprConfig()) -> LprEvent:
    if not isinstance(camera_id, str) or not camera_id.strip():
        raise LprValidationError("camera_id is required.")
    if not isinstance(ocr, OcrResult):
        raise LprValidationError("OCR adapter returned an invalid result.")
    if not isinstance(ocr.engine, str) or not ocr.engine.strip():
        raise LprValidationError("OCR engine name is required.")
    confidence = validate_confidence(ocr.confidence)
    if confidence < config.min_confidence:
        raise LprValidationError("OCR confidence is below the configured threshold.")
    normalized = normalize_plate(ocr.text, min_length=config.min_plate_length, max_length=config.max_plate_length)
    event_time = _timestamp(timestamp)
    event_time_text = event_time.isoformat()
    return LprEvent(
        camera_id=camera_id,
        local_event_id=_event_id(camera_id, normalized, event_time, ocr.engine),
        timestamp=event_time_text,
        plate=ocr.text,
        normalized_plate=normalized,
        confidence=confidence,
        ocr_engine=ocr.engine.strip(),
        ocr_engine_version=ocr.engine_version,
        snapshot_reference=safe_media_reference(candidate.snapshot_reference),
        plate_crop_reference=safe_media_reference(candidate.plate_crop_reference),
        candidate_metadata=dict(candidate.metadata),
    )


class CameraLocalDeduper:
    """In-memory debounce state keyed by both camera and normalized plate."""

    def __init__(self, debounce_seconds: float):
        if not math.isfinite(debounce_seconds) or debounce_seconds < 0:
            raise LprValidationError("debounce_seconds must be a finite non-negative number.")
        self.debounce_seconds = debounce_seconds
        self._last_seen: dict[tuple[str, str], datetime] = {}

    def is_duplicate(self, camera_id: str, normalized_plate: str, timestamp: datetime | str) -> bool:
        observed = _timestamp(timestamp)
        key = (camera_id, normalized_plate)
        previous = self._last_seen.get(key)
        if previous is not None and 0 <= (observed - previous).total_seconds() < self.debounce_seconds:
            return True
        self._last_seen[key] = observed
        return False


@dataclass
class LprMetrics:
    frames_sampled: int = 0
    events_emitted: int = 0
    duplicates_suppressed: int = 0
    candidate_detection_seconds: float = 0.0
    ocr_seconds: float = 0.0
    pipeline_seconds: float = 0.0


class LprProcessor:
    """Coordinates local adapters without performing network or camera I/O."""

    def __init__(self, ocr: OcrAdapter, *, config: LprConfig = LprConfig(), localizer: PlateLocalizer | None = None):
        self.ocr = ocr
        self.config = config
        self.localizer = localizer
        self.deduper = CameraLocalDeduper(config.debounce_seconds)
        self.metrics = LprMetrics()

    def process_candidate(self, *, camera_id: str, timestamp: datetime | str,
                          candidate: PlateCandidate, entitlement_rows: Iterable[dict[str, Any]]) -> LprEvent | None:
        """Return an event only when this exact camera has active LPR rights."""
        started = perf_counter()
        self.metrics.frames_sampled += 1
        if not active_lpr_entitlement(entitlement_rows):
            self.metrics.pipeline_seconds += perf_counter() - started
            return None
        ocr_started = perf_counter()
        result = self.ocr.recognize(candidate)
        self.metrics.ocr_seconds += perf_counter() - ocr_started
        event = build_event(camera_id=camera_id, timestamp=timestamp, ocr=result, candidate=candidate, config=self.config)
        if self.deduper.is_duplicate(camera_id, event.normalized_plate, event.timestamp):
            self.metrics.duplicates_suppressed += 1
            self.metrics.pipeline_seconds += perf_counter() - started
            return None
        self.metrics.events_emitted += 1
        self.metrics.pipeline_seconds += perf_counter() - started
        return event

    def process_frame(self, *, camera_id: str, timestamp: datetime | str, frame: Any,
                      entitlement_rows: Iterable[dict[str, Any]], vehicle_candidates: Iterable[Any] | None = None) -> list[LprEvent]:
        if self.localizer is None:
            raise LprValidationError("No plate localizer has been configured.")
        started = perf_counter()
        candidates = list(self.localizer.localize(frame, vehicle_candidates))
        self.metrics.candidate_detection_seconds += perf_counter() - started
        events: list[LprEvent] = []
        for candidate in candidates:
            event = self.process_candidate(camera_id=camera_id, timestamp=timestamp, candidate=candidate, entitlement_rows=entitlement_rows)
            if event:
                events.append(event)
        return events
