"""Pure unit tests for the CPU-only, no-network LPR Phase 1 contract."""
from datetime import datetime, timedelta, timezone
import math

import pytest

from lpr import (
    ANALYTIC_KEY, EVENT_TYPE, RELAY_ACTIONS, CameraLocalDeduper, LprConfig,
    LprProcessor, LprValidationError, OcrResult, PlateCandidate, StaticOcrAdapter,
    active_lpr_entitlement, build_event, normalize_plate, safe_media_reference,
    validate_confidence,
)
from customer_analytics_panel import licensed_quantity_exceeded


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
ACTIVE = [{"analytic_key": ANALYTIC_KEY, "status": "active"}]


def processor(*, text=" ab-12 3 ", confidence=0.91, debounce=30):
    return LprProcessor(
        StaticOcrAdapter(OcrResult(text, confidence, "test-ocr", "1.0")),
        config=LprConfig(min_confidence=0.6, debounce_seconds=debounce),
    )


def candidate(**kwargs):
    return PlateCandidate(image=object(), **kwargs)


def test_normalizes_without_character_substitution():
    assert normalize_plate(" ab-12 3 ") == "AB123"
    assert normalize_plate("O-01") == "O01"  # never invent or substitute characters


@pytest.mark.parametrize("value", ["---", "", "AB", "A" * 13, None, 7])
def test_rejects_invalid_plate_values(value):
    with pytest.raises(LprValidationError):
        normalize_plate(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [-0.01, 1.01, math.nan, math.inf, -math.inf, True, "bad"])
def test_rejects_invalid_confidence(value):
    with pytest.raises(LprValidationError):
        validate_confidence(value)


def test_confidence_threshold_blocks_emission():
    with pytest.raises(LprValidationError):
        processor(confidence=0.59).process_candidate(camera_id="cam-1", timestamp=NOW, candidate=candidate(), entitlement_rows=ACTIVE)


def test_entitlement_enabled_emits_complete_event_payload():
    event = processor().process_candidate(
        camera_id="cam-1", timestamp=NOW,
        candidate=candidate(snapshot_reference="snap-1.jpg", plate_crop_reference="crop-1.jpg", metadata={"bbox": [1, 2, 3, 4]}),
        entitlement_rows=ACTIVE,
    )
    assert event is not None
    assert event.event_type == EVENT_TYPE
    assert event.camera_id == "cam-1"
    payload = event.analytics_payload()
    assert payload["event_type"] == EVENT_TYPE
    assert payload["detections"]["plate"] == " ab-12 3 "
    assert payload["detections"]["normalized_plate"] == "AB123"
    assert payload["detections"]["snapshot_reference"] == "snap-1.jpg"
    assert payload["detections"]["plate_crop_reference"] == "crop-1.jpg"


def test_disabled_camera_does_not_call_ocr_or_emit():
    class MustNotRun:
        def recognize(self, item):
            raise AssertionError("OCR must not run without a camera entitlement")

    event = LprProcessor(MustNotRun()).process_candidate(camera_id="cam-1", timestamp=NOW, candidate=candidate(), entitlement_rows=[])
    assert event is None


def test_existing_camera_seat_limit_blocks_a_second_lpr_camera():
    """LPR uses the established subscription-seat guard; it is never site-wide."""
    assert not licensed_quantity_exceeded(licensed_quantity=1, currently_entitled_count=0, already_entitled=False)
    assert licensed_quantity_exceeded(licensed_quantity=1, currently_entitled_count=1, already_entitled=False)
    assert not licensed_quantity_exceeded(licensed_quantity=1, currently_entitled_count=1, already_entitled=True)


def test_camera_local_dedupe_and_expiration():
    deduper = CameraLocalDeduper(30)
    assert not deduper.is_duplicate("cam-1", "AB123", NOW)
    assert deduper.is_duplicate("cam-1", "AB123", NOW + timedelta(seconds=29))
    assert not deduper.is_duplicate("cam-1", "AB123", NOW + timedelta(seconds=30))


def test_same_plate_on_different_camera_is_not_suppressed():
    deduper = CameraLocalDeduper(30)
    assert not deduper.is_duplicate("cam-1", "AB123", NOW)
    assert not deduper.is_duplicate("cam-2", "AB123", NOW + timedelta(seconds=1))


def test_processor_suppresses_duplicate_only_after_a_real_event():
    target = processor()
    assert target.process_candidate(camera_id="cam-1", timestamp=NOW, candidate=candidate(), entitlement_rows=ACTIVE)
    assert target.process_candidate(camera_id="cam-1", timestamp=NOW + timedelta(seconds=1), candidate=candidate(), entitlement_rows=ACTIVE) is None
    assert target.metrics.events_emitted == 1
    assert target.metrics.duplicates_suppressed == 1


def test_event_construction_is_idempotent_for_identical_input():
    ocr = OcrResult("AB-123", 0.9, "test-ocr", "1")
    first = build_event(camera_id="cam-1", timestamp=NOW, ocr=ocr, candidate=candidate())
    second = build_event(camera_id="cam-1", timestamp=NOW, ocr=ocr, candidate=candidate())
    assert first.local_event_id == second.local_event_id
    assert first.analytics_payload() == second.analytics_payload()


def test_malformed_ocr_result_is_rejected():
    with pytest.raises(LprValidationError):
        build_event(camera_id="cam-1", timestamp=NOW, ocr=OcrResult("---", 0.9, "engine"), candidate=candidate())


@pytest.mark.parametrize("reference", ["../escape.jpg", "dir/file.jpg", "C:\\file.jpg", "https://example.test/a.jpg", "..", ""])
def test_snapshot_references_are_opaque_and_path_safe(reference):
    with pytest.raises(LprValidationError):
        safe_media_reference(reference)


def test_safe_reference_and_tenant_camera_boundary_contract():
    assert safe_media_reference("event-abc_1.jpg") == "event-abc_1.jpg"
    event = build_event(camera_id="camera-owned-by-appliance", timestamp=NOW, ocr=OcrResult("AB123", 0.9, "engine"), candidate=candidate())
    payload = event.analytics_payload()
    assert "camera_id" not in payload  # endpoint path is authorized server-side
    assert active_lpr_entitlement(ACTIVE)
    assert not active_lpr_entitlement([{"analytic_key": "lpr", "status": "cancelled"}])


def test_cpu_only_module_has_no_relay_or_access_actions():
    assert RELAY_ACTIONS == ()
    assert "socket" not in __import__("lpr").__dict__
    assert "requests" not in __import__("lpr").__dict__
