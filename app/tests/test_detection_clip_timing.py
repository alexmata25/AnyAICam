"""AI event clips must show the object arriving (2026-09-30).

Vehicle clips began with the vehicle already halfway through the frame or
leaving. Cause, traced end to end:
- the detector's frame comes from the live HLS playlist, which FFmpeg opens
  three segments behind live, so it is several seconds old;
- the event was stamped datetime.now() only after YOLO + the LPR scan;
- the 5 s pre-roll was counted back from that late stamp, so the clip could
  begin after the very frame that showed the vehicle;
- in Event mode the recording cut used an output-side -ss with stream copy,
  losing the frames before the next keyframe.

Fixed by timing the event from the detection frame, starting it at the
previous scanned frame (the object entered after it), keeping Event-mode
buffer footage long enough to reach back that far, and seeking on the
input side. The clip is extended at the START, not just the tail."""
import asyncio
import os
import sys
import threading
import time
import types
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

import detection_timing as dt
from event_clips import compute_clip_window
from local_recording_policy import BufferSegment, is_buffer_segment_still_needed

T = datetime(2026, 9, 30, 15, 0, 0)

MANIFEST = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:1041
#EXT-X-INDEPENDENT-SEGMENTS
#EXTINF:2.000000,
camera3_1041.ts
#EXTINF:2.000000,
camera3_1042.ts
#EXTINF:2.000000,
camera3_1043.ts
#EXTINF:2.000000,
camera3_1044.ts
#EXTINF:2.000000,
camera3_1045.ts
"""


# ------------------------------------------------------------ frame time

def test_playlist_segments_are_parsed_in_order():
    assert dt.parse_hls_segments(MANIFEST) == [(f"camera3_{n}.ts", 2.0) for n in range(1041, 1046)]


def test_frame_ffmpeg_reads_is_three_segments_behind_the_newest():
    segments = dt.parse_hls_segments(MANIFEST)
    assert dt.estimate_hls_frame_time(segments, T) == T - timedelta(seconds=6)


def test_long_keyframe_interval_segments_make_the_frame_older():
    segments = [(f"s{n}.ts", 4.0) for n in range(5)]
    assert dt.estimate_hls_frame_time(segments, T) == T - timedelta(seconds=12)


def test_short_playlist_reads_from_its_first_segment():
    assert dt.estimate_hls_frame_time([("a.ts", 2.0), ("b.ts", 2.5)], T) == T - timedelta(seconds=4.5)
    assert dt.estimate_hls_frame_time([], T) is None


def test_frame_time_is_read_from_the_real_playlist_and_segment_file(tmp_path):
    import main
    manifest = tmp_path / "camera3.m3u8"
    manifest.write_text(MANIFEST)
    newest = tmp_path / "camera3_1045.ts"
    newest.write_bytes(b"ts")
    os.utime(newest, (T.timestamp(), T.timestamp()))
    assert main._hls_read_frame_time(manifest) == T - timedelta(seconds=6)
    newest.unlink()
    assert main._hls_read_frame_time(manifest) is None  # unknown -> caller falls back to now


# ------------------------------------------------------------ event span

def test_event_starts_at_the_previous_scanned_frame():
    start, moment = dt.detection_event_span(T, T - timedelta(seconds=6), now=T + timedelta(seconds=9), scan_interval_seconds=5)
    assert (start, moment) == (T - timedelta(seconds=6), T)


def test_without_a_previous_frame_the_event_starts_one_scan_interval_earlier():
    start, moment = dt.detection_event_span(T, None, now=T + timedelta(seconds=9), scan_interval_seconds=5)
    assert (start, moment) == (T - timedelta(seconds=5), T)


def test_a_stale_previous_frame_is_capped():
    start, _ = dt.detection_event_span(T, T - timedelta(minutes=10), now=T, scan_interval_seconds=5)
    assert start == T - timedelta(seconds=5)
    start, _ = dt.detection_event_span(T, T - timedelta(seconds=45), now=T, scan_interval_seconds=60)
    assert start == T - timedelta(seconds=dt.MAX_EVENT_LEAD_SECONDS)


def test_an_implausible_frame_time_falls_back_to_now():
    now = T
    assert dt.detection_event_span(T + timedelta(seconds=5), None, now=now, scan_interval_seconds=5)[1] == now
    assert dt.detection_event_span(T - timedelta(minutes=5), None, now=now, scan_interval_seconds=5)[1] == now


def test_the_reported_bug_the_clip_now_includes_the_vehicle_entering():
    """A car enters at E, is first caught by the scan frame F (mid-frame),
    and the event is processed 9 s later (HLS lag + YOLO + LPR)."""
    previous_scan = T - timedelta(seconds=6)       # empty street
    entered = T - timedelta(seconds=2)             # the car appears
    detection_frame = T                            # car mid-frame
    processed = T + timedelta(seconds=9)           # when save_yolo_events runs

    old = compute_clip_window(processed, processed)
    assert old.start > detection_frame             # old: clip began after the detection frame

    start, moment = dt.detection_event_span(detection_frame, previous_scan, now=processed, scan_interval_seconds=5)
    new = compute_clip_window(start, moment)
    assert new.start <= entered - timedelta(seconds=5)   # a full pre-roll before it arrives
    assert new.end == detection_frame + timedelta(seconds=5)  # not just a longer tail


# ------------------------------------------------------------ save_yolo_events wiring

@pytest.fixture
def wired(monkeypatch, tmp_path):
    import main
    main.ai_event_clip_windows.clear()
    builds, uploads = [], []

    async def fake_build(event_id, camera_number, start, end):
        builds.append((start, end))
        return f"/recordings/clips/motion/motion_{event_id}.mp4"

    def fake_upload(**kwargs):
        uploads.append(kwargs)
        return True

    module = types.ModuleType("event_media_uploader")
    module.upload_motion_event_media = fake_upload
    monkeypatch.setitem(sys.modules, "event_media_uploader", module)
    monkeypatch.setattr(main, "build_motion_event_clip", fake_build)
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path)
    saved = []
    monkeypatch.setattr(main, "append_analytics_event", lambda event: saved.append(event))
    linked = []
    monkeypatch.setattr(main, "linked_recording_for", lambda camera, at, *a, **k: linked.append(at))

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(main, "_ai_event_media_loop", loop)
    yield main, builds, uploads, saved, linked
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)
    loop.close()
    main.ai_event_clip_windows.clear()


def _car(frame_time, previous):
    return {"detections": [{"x": 10, "y": 10, "width": 40, "height": 40, "class_name": "car", "confidence": 0.9}],
            "frame": np.zeros((120, 160, 3), dtype=np.uint8),
            "frame_captured_at": frame_time, "previous_frame_captured_at": previous}


def test_ai_event_is_timed_from_its_frame_and_its_clip_starts_before_the_previous_scan(wired):
    main, builds, uploads, saved, linked = wired
    frame_time = datetime.now() - timedelta(seconds=8)
    previous = frame_time - timedelta(seconds=6)
    events = main.save_yolo_events(7, _car(frame_time, previous))
    assert events and events[0]["timestamp"] == frame_time.isoformat()
    assert linked[0] == frame_time
    assert main.ai_event_clip_windows[7].start == previous - timedelta(seconds=5)
    deadline = time.time() + 5
    while time.time() < deadline and not (builds and uploads):
        time.sleep(0.02)
    assert builds[0] == (previous, frame_time)
    assert (uploads[0]["event_start"], uploads[0]["event_end"]) == (previous, frame_time)


def test_detector_loop_hands_the_previous_frame_time_to_the_event():
    import inspect
    import main
    source = inspect.getsource(main.ai_person_detector)
    assert 'result["previous_frame_captured_at"] = ai_last_frame_time.get(camera_number)' in source
    assert '"frame_captured_at": frame_captured_at' in inspect.getsource(main.detect_objects_frame)


# ------------------------------------------------------------ Event mode

def test_buffer_footage_is_kept_long_enough_to_reach_the_earlier_start():
    now = T
    segment = BufferSegment(start=now - timedelta(seconds=50), end=now - timedelta(seconds=20))
    assert not is_buffer_segment_still_needed(segment, now=now, pre_roll_seconds=5)  # old retention
    assert is_buffer_segment_still_needed(segment, now=now, pre_roll_seconds=5, lookback_seconds=65)


def test_janitor_applies_the_detection_lookback():
    import inspect
    import main
    assert 'lookback_seconds=DETECTION_LOOKBACK_SECONDS + settings["post_roll_seconds"]' in inspect.getsource(main.event_buffer_janitor)


def test_event_recording_cut_seeks_on_the_input_so_it_starts_on_a_keyframe(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    commands = []

    def fake_run(args, **kwargs):
        commands.append(list(args))
        if "concat" in args:
            Path(args[-1]).write_bytes(b"clip")
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    buffer_folder = tmp_path / "recordings" / "camera1" / main.EVENT_BUFFER_SUBFOLDER_NAME
    buffer_folder.mkdir(parents=True)
    (buffer_folder / f"buf1_{T - timedelta(seconds=30):%Y-%m-%d_%H-%M-%S}.mkv").write_bytes(b"a")
    main._open_event_recordings.pop(1, None)
    main._event_recording_locks.pop(1, None)
    asyncio.run(main.persist_event_recording(1, T - timedelta(seconds=6), T))
    command = next(c for c in commands if "concat" in c)
    assert command.index("-ss") < command.index("-i")
    assert float(command[command.index("-ss") + 1]) == 19.0  # (T-6-5) - (T-30)
    assert float(command[command.index("-t") + 1]) == 16.0   # (T+5) - (T-11)
    main._open_event_recordings.pop(1, None)
