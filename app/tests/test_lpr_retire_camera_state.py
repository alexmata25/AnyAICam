"""Bound LPR camera state and release it when a camera is removed."""

import numpy as np
import pytest

import lpr
import main


@pytest.fixture(autouse=True)
def clean_lpr_state():
    lpr.reset_pipeline_state()
    yield
    lpr.reset_pipeline_state()


def test_retiring_camera_releases_frame_and_all_per_camera_history():
    camera, other = 12, 13
    lpr._votes[camera] = [(1.0, "ABC1234")]
    lpr._votes[other] = [(2.0, "XYZ9876")]
    lpr._emitted[(camera, "ABC1234")] = 1.0
    lpr._emitted[(other, "XYZ9876")] = 2.0
    lpr._full_frame_cache[camera] = (3.0, np.zeros((1440, 2560, 3), dtype=np.uint8))
    lpr._full_frame_cache[other] = (4.0, np.zeros((4, 4, 3), dtype=np.uint8))
    lpr._tracks[camera] = [{"box": (1, 2, 3, 4), "first": 1.0, "attempts": 1, "done": False}]
    lpr._last_scan[camera] = 5.0
    lpr._last_full_frame[camera] = 6.0

    lpr.retire_camera_state(camera)

    assert camera not in lpr._votes
    assert (camera, "ABC1234") not in lpr._emitted
    assert camera not in lpr._full_frame_cache
    assert camera not in lpr._tracks
    assert camera not in lpr._last_scan
    assert camera not in lpr._last_full_frame
    assert lpr._votes[other] == [(2.0, "XYZ9876")]
    assert (other, "XYZ9876") in lpr._emitted
    assert other in lpr._full_frame_cache


def test_camera_pipeline_retirement_invokes_lpr_cleanup(monkeypatch):
    class _Forget:
        def forget(self, _camera):
            pass

    monkeypatch.setattr(main.live_hls_demand, "forget", lambda _camera: None)
    monkeypatch.setattr(main, "_motion_source_selector", _Forget())
    monkeypatch.setattr(main, "ai_frames", _Forget())
    monkeypatch.setattr(main, "main_frame_grabber", _Forget())
    monkeypatch.setattr(main.camera_video_profiles, "invalidate", lambda _camera: None)
    lpr._full_frame_cache[12] = (3.0, np.zeros((1440, 2560, 3), dtype=np.uint8))
    lpr._votes[12] = [(1.0, "ABC1234")]

    main.retire_camera_pipeline(12)

    assert 12 not in lpr._full_frame_cache
    assert 12 not in lpr._votes


def test_plate_cooldown_history_expires_after_it_can_no_longer_suppress_reads(monkeypatch):
    monkeypatch.setattr(lpr, "LPR_REPEAT_COOLDOWN_SECONDS", 10.0)
    monkeypatch.setattr(lpr, "LPR_CONFIRM_READS", 1)
    monkeypatch.setattr(lpr, "LPR_CONFIRM_MIN_CONFIDENCE", 0.0)
    lpr._emitted[(1, "OLD1234")] = 1.0
    lpr._emitted[(2, "KEEP1234")] = 95.0

    confirmed = lpr.confirm_plate(1, {"plate_number": "NEW1234", "confidence": 90.0}, now=100.0)

    assert confirmed and confirmed["plate_number"] == "NEW1234"
    assert (1, "OLD1234") not in lpr._emitted
    assert (2, "KEEP1234") in lpr._emitted
