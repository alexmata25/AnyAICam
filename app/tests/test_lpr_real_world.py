"""Real-world LPR pipeline (2026-09-27): plate detector with cascade
fallback, glyph-row reader for tilted/slanted plates, multi-read voting,
parked-vehicle suppression and full-resolution crops. No plate values are
special-cased anywhere; the synthetic plates below are random strings."""
import shutil

import cv2
import numpy as np
import pytest

import lpr


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(lpr, "LPR_ENABLED", True)
    lpr.reset_pipeline_state()
    yield
    lpr.reset_pipeline_state()


# ---------------------------------------------------------------- voting / cooldown

def test_a_single_read_is_never_reported():
    assert lpr.confirm_plate(2, {"plate_number": "ABC1234", "confidence": 90}, now=100.0) is None


def test_two_matching_reads_confirm_once_then_cooldown():
    assert lpr.confirm_plate(2, {"plate_number": "ABC1234", "confidence": 90}, now=100.0) is None
    confirmed = lpr.confirm_plate(2, {"plate_number": "ABC1234", "confidence": 92}, now=105.0)
    assert confirmed["plate_number"] == "ABC1234" and confirmed["confirmations"] == 2
    assert lpr.confirm_plate(2, {"plate_number": "ABC1234", "confidence": 92}, now=110.0) is None  # parked: not again
    later = 105.0 + lpr.LPR_REPEAT_COOLDOWN_SECONDS + 1
    lpr.confirm_plate(2, {"plate_number": "ABC1234", "confidence": 92}, now=later)
    assert lpr.confirm_plate(2, {"plate_number": "ABC1234", "confidence": 92}, now=later + 1) is not None


def test_disagreeing_misreads_are_never_confirmed():
    for i, text in enumerate(["ABC1234", "A8C1234", "ABC1Z34", "ABG1234"]):
        assert lpr.confirm_plate(2, {"plate_number": text, "confidence": 70}, now=100.0 + i) is None


def test_votes_expire_and_cameras_are_independent():
    lpr.confirm_plate(2, {"plate_number": "ABC1234"}, now=100.0)
    assert lpr.confirm_plate(3, {"plate_number": "ABC1234"}, now=101.0) is None  # other camera
    assert lpr.confirm_plate(2, {"plate_number": "ABC1234"}, now=100.0 + lpr.LPR_VOTE_WINDOW_SECONDS + 5) is None  # expired


def test_empty_results_are_ignored():
    assert lpr.confirm_plate(2, None) is None and lpr.confirm_plate(2, {"plate_number": ""}) is None


# ---------------------------------------------------------------- parked-vehicle suppression

def test_parked_vehicle_gets_limited_attempts_and_moving_vehicle_is_new():
    box = (100, 100, 400, 300)
    attempts = [lpr.should_attempt(2, box, now=10.0 + i) for i in range(lpr.LPR_ATTEMPTS_PER_VEHICLE + 2)]
    assert attempts[: lpr.LPR_ATTEMPTS_PER_VEHICLE] == [True] * lpr.LPR_ATTEMPTS_PER_VEHICLE
    assert attempts[lpr.LPR_ATTEMPTS_PER_VEHICLE:] == [False, False]
    assert lpr.should_attempt(2, (700, 100, 400, 300), now=20.0)  # a different position
    assert lpr.should_attempt(2, box, now=10.0 + lpr.LPR_STATIONARY_SKIP_SECONDS + 1)  # track expired


def test_a_confirmed_vehicle_is_not_reread():
    box = (100, 100, 400, 300)
    assert lpr.should_attempt(2, box, now=1.0)
    lpr.mark_vehicle_read(2, box)
    assert not lpr.should_attempt(2, (102, 101, 400, 300), now=2.0)


# ---------------------------------------------------------------- full-resolution crop mapping

def test_full_resolution_crop_maps_and_widens_the_box():
    analytics = np.zeros((720, 1280, 3), np.uint8)
    full = np.zeros((1440, 2560, 3), np.uint8)
    full[400:800, 800:1600] = 255
    crop = lpr.full_resolution_vehicle_crop(analytics, (420, 220, 360, 160), full, margin=0.0)
    assert crop.shape[:2] == (320, 720) and crop.mean() == 255


def test_no_full_resolution_crop_when_it_adds_no_pixels():
    same = np.zeros((2160, 3840, 3), np.uint8)
    assert lpr.full_resolution_vehicle_crop(same, (10, 10, 100, 100), same) is None
    assert lpr.full_resolution_vehicle_crop(same, (10, 10, 100, 100), None) is None


def test_full_frame_fetch_is_cached_and_fails_soft(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(lpr.subprocess, "run", lambda *a, **k: calls.append(1) or type("R", (), {"stdout": b""})())
    (tmp_path / "buf2_x.mkv").write_bytes(b"x")
    assert lpr.latest_full_resolution_frame(2, str(tmp_path), now=1.0) is None
    assert lpr.latest_full_resolution_frame(2, str(tmp_path), now=2.0) is None
    assert len(calls) == 1  # second call served from the per-camera cache
    assert lpr.latest_full_resolution_frame(3, str(tmp_path / "missing"), now=1.0) is None


# ---------------------------------------------------------------- detector fallback and pixel rule

def test_detector_falls_back_to_the_cascade_without_the_model(monkeypatch):
    monkeypatch.setattr(lpr, "LPR_PLATE_MODEL_PATH", "/nonexistent/plate.pt")
    monkeypatch.setattr(lpr, "_detect_plate_region_cascade", lambda crop: (1, 2, 30, 10))
    detection = lpr.detect_plate(np.zeros((200, 400, 3), np.uint8))
    assert detection == {"region": (1, 2, 30, 10), "detector_confidence": 0.0, "source": "cascade"}


def test_tiny_vehicle_crops_are_skipped(monkeypatch):
    called = []
    monkeypatch.setattr(lpr, "detect_plate", lambda crop: called.append(1))
    assert lpr.recognize_plate(np.zeros((60, lpr.LPR_MIN_VEHICLE_WIDTH - 1, 3), np.uint8)) is None
    assert called == []


def test_recognize_plate_combines_detector_and_reader_confidence(monkeypatch):
    monkeypatch.setattr(lpr, "detect_plate", lambda crop: {"region": (0, 0, 50, 20), "detector_confidence": 0.8, "source": "model"})
    monkeypatch.setattr(lpr, "read_plate_glyphs", lambda crop: ("XY12345", 80.0))
    result = lpr.recognize_plate(np.zeros((300, 600, 3), np.uint8))
    assert result["plate_number"] == "XY12345" and result["confidence"] == 96.0 and result["detector"] == "model"


# ---------------------------------------------------------------- glyph reader on a tilted, slanted synthetic plate

def _synthetic_plate(text, angle, shear):
    plate = np.full((110, 300, 3), 235, np.uint8)
    cv2.rectangle(plate, (4, 4), (295, 105), (40, 40, 40), 3)
    cv2.putText(plate, "STATE", (110, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (90, 90, 90), 1)
    cv2.putText(plate, text, (22, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.8, (20, 20, 20), 5)
    h, w = plate.shape[:2]
    canvas = cv2.copyMakeBorder(plate, 60, 60, 60, 60, cv2.BORDER_CONSTANT, value=(120, 120, 120))
    ch, cw = canvas.shape[:2]
    m = cv2.getRotationMatrix2D((cw / 2, ch / 2), angle, 1.0)
    canvas = cv2.warpAffine(canvas, m, (cw, ch), borderValue=(120, 120, 120))
    s = np.float32([[1, shear, -shear * ch / 2], [0, 1, 0]])
    return cv2.warpAffine(canvas, s, (cw, ch), borderValue=(120, 120, 120))


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed (it is in the release image)")
@pytest.mark.parametrize("angle,shear", [(0, 0.0), (18, 0.0), (-22, 0.25), (25, -0.3), (-15, -0.2)])
def test_glyph_reader_handles_tilted_and_slanted_plates(angle, shear):
    rng = np.random.default_rng(abs(int(angle * 10)) + 7)
    text = "".join(rng.choice(list("ABCDEFHJKLMNPRSTUVWXYZ"), 3)) + "".join(rng.choice(list("0123456789"), 4))
    result = lpr.read_plate_glyphs(_synthetic_plate(text, angle, shear))
    assert result is not None and result[0] == text, (text, result)
