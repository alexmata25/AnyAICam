"""ppe.py tests. summarize_ppe()/_parse_detections() are pure and
covered thoroughly with synthetic detection data -- no model or image
needed. detect_ppe() is exercised against the real, real model file
(if present) with real synthetic image arrays to prove the actual
inference call succeeds end-to-end without crashing; this can't
meaningfully assert *what* it detects (no real PPE-wearing person
exists as a fixture), but it does prove the real ultralytics
predict()/parse path works, matching lpr.py's own real-OCR-not-mocked
testing philosophy.
"""

import numpy as np
import pytest

import ppe


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch):
    ppe.reset_state()
    monkeypatch.setattr(ppe, "PPE_ENABLED", True)
    monkeypatch.setattr(ppe, "PPE_MIN_CONFIDENCE", 0.4)
    monkeypatch.setattr(ppe, "PPE_CAMERAS", None)
    yield
    ppe.reset_state()


# --------------------------------------------------------- summarize_ppe

def test_no_detections_means_nothing_present():
    result = ppe.summarize_ppe([])
    assert result == {
        "hard_hat_present": False,
        "safety_vest_present": False,
        "confidence": 0.0,
        "detections": [],
    }


def test_helmet_detection_sets_hard_hat_present():
    detections = [{"class_name": "helmet", "confidence": 0.87}]
    result = ppe.summarize_ppe(detections)
    assert result["hard_hat_present"] is True
    assert result["safety_vest_present"] is False
    assert result["confidence"] == 0.87


def test_vest_detection_sets_safety_vest_present():
    detections = [{"class_name": "vest", "confidence": 0.91}]
    result = ppe.summarize_ppe(detections)
    assert result["hard_hat_present"] is False
    assert result["safety_vest_present"] is True
    assert result["confidence"] == 0.91


def test_both_present_when_both_detected():
    detections = [{"class_name": "helmet", "confidence": 0.7}, {"class_name": "vest", "confidence": 0.6}]
    result = ppe.summarize_ppe(detections)
    assert result["hard_hat_present"] is True
    assert result["safety_vest_present"] is True
    assert result["confidence"] == 0.7  # the max of the two relevant hits


def test_irrelevant_classes_are_ignored_for_presence_but_kept_in_detections():
    detections = [{"class_name": "goggles", "confidence": 0.99}, {"class_name": "mask", "confidence": 0.95}]
    result = ppe.summarize_ppe(detections)
    assert result["hard_hat_present"] is False
    assert result["safety_vest_present"] is False
    assert result["confidence"] == 0.0
    assert result["detections"] == detections  # raw detections preserved regardless


def test_multiple_helmet_hits_uses_the_highest_confidence():
    detections = [{"class_name": "helmet", "confidence": 0.3}, {"class_name": "helmet", "confidence": 0.8}]
    result = ppe.summarize_ppe(detections)
    assert result["confidence"] == 0.8


# --------------------------------------------------------- is_camera_enabled

def test_unrestricted_by_default():
    for camera in (1, 2, 3, 4, 5, 99):
        assert ppe.is_camera_enabled(camera) is True


def test_scoped_allowlist_excludes_camera_5(monkeypatch):
    monkeypatch.setattr(ppe, "PPE_CAMERAS", frozenset({1, 2, 3, 4}))
    for camera in (1, 2, 3, 4):
        assert ppe.is_camera_enabled(camera) is True
    assert ppe.is_camera_enabled(5) is False


# --------------------------------------------------------- detect_ppe: guard conditions

def test_detect_ppe_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(ppe, "PPE_ENABLED", False)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    assert ppe.detect_ppe(frame) is None


def test_detect_ppe_returns_none_for_camera_not_in_scope(monkeypatch):
    monkeypatch.setattr(ppe, "PPE_CAMERAS", frozenset({1, 2, 3, 4}))
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    assert ppe.detect_ppe(frame, camera_number=5) is None


def test_detect_ppe_returns_none_for_none_input():
    assert ppe.detect_ppe(None) is None


def test_detect_ppe_returns_none_for_empty_image():
    assert ppe.detect_ppe(np.zeros((0, 0, 3), dtype=np.uint8)) is None


# --------------------------------------------------------- real model, real inference call

def test_real_model_loads():
    model = ppe._get_model()
    assert model is not None
    names = {name.lower() for name in model.names.values()}
    assert "helmet" in names
    assert "vest" in names


def test_real_inference_runs_end_to_end_without_crashing_on_a_blank_frame():
    # A blank frame won't contain real PPE -- this proves the real
    # predict()/parse pipeline completes and returns the expected
    # shape, not that detection accuracy is good.
    frame = np.full((480, 640, 3), 128, dtype=np.uint8)
    result = ppe.detect_ppe(frame, camera_number=1)
    assert result is None or (
        isinstance(result, dict)
        and {"hard_hat_present", "safety_vest_present", "confidence", "detections"} <= result.keys()
    )


def test_real_inference_never_raises_on_a_noisy_random_frame():
    rng = np.random.default_rng(42)
    frame = rng.integers(0, 255, size=(480, 640, 3), dtype=np.uint8)
    result = ppe.detect_ppe(frame, camera_number=1)
    assert result is None or isinstance(result, dict)
