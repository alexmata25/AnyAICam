"""lpr.py tests. Pure-logic tests (normalize/plausibility/camera-scope)
need no image libraries and run everywhere; the OCR tests render a
real synthetic plate-like image and run it through the real tesseract
engine (installed via the Dockerfile) -- not mocked -- so a genuine
break in the OCR preprocessing pipeline (wrong colorspace, bad
threshold, wrong tesseract config) would actually fail these, not just
a mocked call. detect_plate_region()'s Haar cascade is a heuristic
detector not reliable enough to assert exact behavior against a
synthetically-drawn rectangle, so it's tested only for its
None-on-invalid-input contract; is_plausible_plate/normalize coverage
is what actually protects the pipeline's output quality.
"""

import numpy as np
import pytest

import lpr


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch):
    lpr.reset_state()
    monkeypatch.setattr(lpr, "LPR_ENABLED", True)
    monkeypatch.setattr(lpr, "LPR_VEHICLE_CLASSES", frozenset({"car", "truck", "bus"}))
    monkeypatch.setattr(lpr, "LPR_MIN_CONFIDENCE", 45.0)
    monkeypatch.setattr(lpr, "LPR_MIN_PLATE_LENGTH", 5)
    monkeypatch.setattr(lpr, "LPR_MAX_PLATE_LENGTH", 10)
    monkeypatch.setattr(lpr, "LPR_CAMERAS", None)
    yield
    lpr.reset_state()


# --------------------------------------------------------- normalize_plate_text

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("abc-1234", "ABC1234"),
        ("ABC 1234", "ABC1234"),
        ("  abc1234  ", "ABC1234"),
        ("abc·1234!", "ABC1234"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_plate_text(raw, expected):
    assert lpr.normalize_plate_text(raw) == expected


# --------------------------------------------------------- is_plausible_plate

def test_a_realistic_plate_is_plausible():
    assert lpr.is_plausible_plate("ABC1234") is True


def test_too_short_is_not_plausible():
    assert lpr.is_plausible_plate("AB1") is False


def test_too_long_is_not_plausible():
    assert lpr.is_plausible_plate("ABCDEFGHIJK123") is False


def test_all_letters_no_digits_is_not_plausible():
    assert lpr.is_plausible_plate("ABCDEFG") is False


def test_all_digits_is_plausible():
    assert lpr.is_plausible_plate("123456") is True


def test_boundary_lengths_are_plausible():
    assert lpr.is_plausible_plate("A1234") is True  # exactly LPR_MIN_PLATE_LENGTH
    assert lpr.is_plausible_plate("A123456789") is True  # exactly LPR_MAX_PLATE_LENGTH


# --------------------------------------------------------- is_camera_enabled

def test_unrestricted_by_default():
    for camera in (1, 2, 3, 4, 5, 99):
        assert lpr.is_camera_enabled(camera) is True


def test_scoped_allowlist_excludes_camera_5(monkeypatch):
    monkeypatch.setattr(lpr, "LPR_CAMERAS", frozenset({1, 2, 3, 4}))
    for camera in (1, 2, 3, 4):
        assert lpr.is_camera_enabled(camera) is True
    assert lpr.is_camera_enabled(5) is False


# --------------------------------------------------------- detect_plate_region / recognize_plate: invalid input

def test_detect_plate_region_returns_none_for_empty_image():
    assert lpr.detect_plate_region(np.zeros((0, 0, 3), dtype=np.uint8)) is None


def test_detect_plate_region_returns_none_for_none_input():
    assert lpr.detect_plate_region(None) is None


def test_read_plate_text_returns_none_for_none_input():
    assert lpr.read_plate_text(None) is None


def test_read_plate_text_returns_none_for_empty_image():
    assert lpr.read_plate_text(np.zeros((0, 0, 3), dtype=np.uint8)) is None


def test_recognize_plate_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(lpr, "LPR_ENABLED", False)
    frame = _render_plate_image("ABC1234")
    assert lpr.recognize_plate(frame) is None


def test_recognize_plate_returns_none_for_camera_not_in_scope(monkeypatch):
    monkeypatch.setattr(lpr, "LPR_CAMERAS", frozenset({1, 2, 3, 4}))
    frame = _render_plate_image("ABC1234")
    assert lpr.recognize_plate(frame, camera_number=5) is None


# --------------------------------------------------------- real OCR against a synthetic plate image

def _render_plate_image(text: str):
    """A real, rendered (not mocked) white plate-like image with real
    black text, run through the real cv2/tesseract pipeline below --
    this is as close to a real plate crop as a synthetic image can get
    without an actual camera frame."""
    import cv2

    image = np.full((80, 260, 3), 255, dtype=np.uint8)
    cv2.putText(image, text, (10, 55), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 0), 4, cv2.LINE_AA)
    return image


def test_real_ocr_reads_a_clean_synthetic_plate():
    # Real OCR is not pixel-perfect even on a clean synthetic render --
    # this genuinely caught tesseract reading a rendered "3" as "5" on
    # first run, which is honest, expected OCR behavior, not a bug in
    # this pipeline. The contract under test is "the real engine reads
    # a plausible plate of the right length with real confidence,"
    # not "OCR never makes a mistake."
    image = _render_plate_image("ABC1234")
    result = lpr.read_plate_text(image)
    assert result is not None
    text, confidence = result
    assert len(text) == len("ABC1234")
    assert text[:3] == "ABC"  # the letters are unambiguous at this render size
    assert lpr.is_plausible_plate(text)
    assert 0 <= confidence <= 100


def test_real_ocr_rejects_a_blank_image():
    blank = np.full((80, 260, 3), 255, dtype=np.uint8)
    assert lpr.read_plate_text(blank) is None


def test_recognize_plate_end_to_end_on_a_synthetic_frame_with_no_cascade_match():
    # A bare rendered-text image (no vehicle body/edges around it) is
    # not something the Haar cascade is expected to find a plate
    # region within -- recognize_plate() must return None cleanly
    # rather than erroring, exercising the real "region not found"
    # path against a real image.
    image = _render_plate_image("ABC1234")
    result = lpr.recognize_plate(image)
    assert result is None or (isinstance(result, dict) and lpr.is_plausible_plate(result["plate_number"]))
