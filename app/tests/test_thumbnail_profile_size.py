"""video-pipeline P2d (2026-10-11): AI / LPR / People Counting / Smart Rules
thumbnails are saved at the profile size (ANYAICAM_THUMBNAIL_MAX_WIDTH,
1280 by default) instead of the full 3840x2160 frame; boxes are drawn in
thumbnail coordinates. Also: PPE crops come from the on-demand main-stream
frame when the analytics frame is a substream frame (P2a)."""
import sys
import types

import cv2
import numpy as np
import pytest

import main
import video_profile


def test_thumbnail_image_downscales_4k_and_keeps_small_frames():
    big = np.zeros((2160, 3840, 3), np.uint8)
    image, factor = video_profile.thumbnail_image(big, 1280)
    assert image.shape == (720, 1280, 3) and factor == pytest.approx(1 / 3)
    small = np.zeros((360, 640, 3), np.uint8)
    image, factor = video_profile.thumbnail_image(small, 1280)
    assert image.shape == small.shape and factor == 1.0 and image is not small
    fisheye = np.zeros((2880, 2880, 3), np.uint8)
    assert video_profile.thumbnail_image(fisheye, 1280)[0].shape == (1280, 1280, 3)
    odd = np.zeros((1944, 2592, 3), np.uint8)
    assert video_profile.thumbnail_image(odd, 1280)[0].shape == (960, 1280, 3)
    assert video_profile.thumbnail_image(big, 0)[0].shape == big.shape     # 0 = previous behaviour


def test_scale_box():
    assert video_profile.scale_box(300, 600, 900, 1200, 1 / 3) == (100, 200, 300, 400)
    assert video_profile.scale_box(3, 6, 1, 1, 0.1) == (0, 1, 1, 1)
    assert video_profile.scale_box(1, 2, 3, 4, 1.0) == (1, 2, 3, 4)


@pytest.fixture
def save_env(monkeypatch, tmp_path):
    main.ai_event_clip_windows.clear()
    main.ai_activities.reset()
    monkeypatch.setattr(main, "_ai_activity_limits", lambda camera: (0.0, 300.0))
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path)
    monkeypatch.setattr(main, "linked_recording_for", lambda *a, **k: None)
    events = []
    monkeypatch.setattr(main, "append_analytics_event", lambda event: events.append(event))
    module = types.ModuleType("event_media_uploader")
    module.upload_motion_event_media = lambda **kwargs: True
    monkeypatch.setitem(sys.modules, "event_media_uploader", module)
    monkeypatch.setattr(main, "_ai_event_media_loop", None)
    yield tmp_path, events
    main.ai_event_clip_windows.clear()
    main.ai_activities.reset()


def _saved_images(folder):
    return [cv2.imread(str(path)) for path in sorted(folder.rglob("*.jpg"))]


def test_yolo_event_thumbnail_is_saved_at_profile_size_with_boxes_in_place(save_env):
    folder, events = save_env
    frame = np.zeros((2160, 3840, 3), np.uint8)
    detection = {"x": 1500, "y": 900, "width": 600, "height": 900, "class_name": "person", "confidence": 0.9}
    main.save_yolo_events(1, {"detections": [detection], "frame": frame, "frame_source": "hls"})
    images = _saved_images(folder)
    assert len(images) == 1 and images[0].shape == (720, 1280, 3)
    thumb = images[0]
    # The box outline is drawn at the scaled position (500,300)-(700,600).
    assert thumb[450, 499:502].max() > 100 and thumb[450, 600].max() < 30
    assert events and events[0]["detections"][0]["x"] == 1500      # stored detections keep frame coordinates


def test_substream_frame_thumbnail_is_not_upscaled(save_env):
    folder, _ = save_env
    frame = np.zeros((360, 640, 3), np.uint8)
    detection = {"x": 100, "y": 100, "width": 50, "height": 100, "class_name": "person", "confidence": 0.9}
    main.save_yolo_events(2, {"detections": [detection], "frame": frame, "frame_source": "substream"})
    assert _saved_images(folder)[0].shape == (360, 640, 3)


def test_people_counting_and_rule_thumbnails_use_the_profile_size(tmp_path, monkeypatch):
    import customer_analytics_rule_worker as worker
    from datetime import datetime
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path)
    url = worker.save_rule_event_thumbnail(3, np.zeros((2160, 3840, 3), np.uint8), datetime(2026, 10, 11, 12), "analytics_rule")
    assert url and _saved_images(tmp_path)[0].shape == (720, 1280, 3)
    source = open(main.__file__, encoding="utf-8").read()
    assert "cv2.imwrite(str(thumbnail_path), video_profile.thumbnail_image(frame)[0])" in source  # People Counting


def test_ppe_crop_comes_from_the_main_stream_frame(save_env, monkeypatch):
    import ppe
    crops = []
    monkeypatch.setattr(ppe, "is_camera_enabled", lambda n: True)
    monkeypatch.setattr(ppe, "detect_ppe", lambda crop, camera_number=None: crops.append(crop.shape) or None)
    monkeypatch.setattr(main.recording_uploader, "_camera_identity", lambda n: {"ppe_enabled": True})
    monkeypatch.setattr(main.facial_recognition, "is_camera_enabled", lambda n: False)
    full = np.zeros((2160, 3840, 3), np.uint8)
    grabs = []
    monkeypatch.setattr(main, "ai_full_resolution_frame", lambda n, result: grabs.append(n) or full)
    frame = np.zeros((360, 640, 3), np.uint8)
    detection = {"x": 200, "y": 100, "width": 60, "height": 120, "class_name": "person", "confidence": 0.9}
    main.save_yolo_events(4, {"detections": [detection], "frame": frame, "frame_source": "substream"})
    assert grabs == [4]
    assert crops and crops[0][0] > 600 and crops[0][1] > 300           # ~6x the 120x60 analytics crop


def test_ppe_crop_on_hls_frame_is_the_frame_crop_as_before(save_env, monkeypatch):
    import ppe
    crops = []
    monkeypatch.setattr(ppe, "is_camera_enabled", lambda n: True)
    monkeypatch.setattr(ppe, "detect_ppe", lambda crop, camera_number=None: crops.append(crop.shape) or None)
    monkeypatch.setattr(main.recording_uploader, "_camera_identity", lambda n: {"ppe_enabled": True})
    monkeypatch.setattr(main.facial_recognition, "is_camera_enabled", lambda n: False)
    monkeypatch.setattr(main.main_frame_grabber, "grab", lambda n: pytest.fail("no grab for an HLS frame"))
    frame = np.zeros((2160, 3840, 3), np.uint8)
    detection = {"x": 1200, "y": 600, "width": 360, "height": 720, "class_name": "person", "confidence": 0.9}
    main.save_yolo_events(5, {"detections": [detection], "frame": frame, "frame_source": "hls"})
    assert crops == [(720, 360, 3)]
