"""Tests for the dependency-free, redacted Phase 2 LPR stdout contract."""
import json

import pytest

from lpr_validation import LprValidationResult, ValidationResultError


def result(**overrides):
    values = {
        "label": "STATIC_SANITY_CHECK",
        "detector_candidate_count": 1,
        "crop_width": 120,
        "crop_height": 36,
        "raw_ocr_text": "AB-123",
        "normalized_plate": "AB123",
        "ocr_confidence": 0.91,
        "detector_latency_ms": 17.5,
        "ocr_latency_ms": 64.0,
        "total_pipeline_latency_ms": 83.0,
    }
    values.update(overrides)
    return LprValidationResult(**values)


def test_stdout_record_has_each_required_metric_and_is_one_json_line():
    payload = json.loads(result().stdout_json())
    assert payload == {
        "crop_height": 36,
        "crop_width": 120,
        "detector_candidate_count": 1,
        "detector_latency_ms": 17.5,
        "label": "STATIC_SANITY_CHECK",
        "normalized_plate": "AB123",
        "ocr_confidence": 0.91,
        "ocr_latency_ms": 64.0,
        "raw_ocr_text": "AB-123",
        "total_pipeline_latency_ms": 83.0,
    }
    assert "\n" not in result().stdout_json()


def test_no_candidate_is_reportable_without_inventing_a_crop_or_plate():
    payload = json.loads(result(detector_candidate_count=0, crop_width=None, crop_height=None,
                                raw_ocr_text=None, normalized_plate=None, ocr_confidence=None).stdout_json())
    assert payload["detector_candidate_count"] == 0
    assert payload["normalized_plate"] is None


@pytest.mark.parametrize("unsafe", ["rtsp://user:password@camera", "C:\\secret.jpg", "folder/crop.jpg", "https://example.test"])
def test_result_contract_rejects_urls_paths_and_credential_like_values(unsafe):
    with pytest.raises(ValidationResultError):
        result(raw_ocr_text=unsafe)


def test_result_contract_rejects_impossible_latency_accounting():
    with pytest.raises(ValidationResultError):
        result(detector_latency_ms=20, ocr_latency_ms=30, total_pipeline_latency_ms=49)
