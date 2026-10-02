"""Event-mode recordings must be labelled with the time of their real first
frame, and the event clips cut from them must contain what was requested
(2026-10-01, Ryzen Living Room, event 698b02643c4f).

The thumbnail showed the owner walking in at 20:32:09 (appliance time), but
the playable clip showed him already seated. Traced with the camera's own
on-screen clock: the Event-mode recording the clip was cut from began 6-8 s
EARLIER than its filename said, because an input-side -ss on the concat
demuxer is ignored with stream copy (the cut began at the first buffer
file's start). Every clip cut from it -- by filename time -- was shifted
early and could end before the detection. The release before had the mirror
problem: an output-side -ss began on the NEXT keyframe (content late).

The fix cuts on the last keyframe at or before the requested start (concat
inpoint) and labels the recording with that keyframe's true time.

The real-media tests encode each frame's number in its pixels, so they check
what the files actually show, not just their timestamps. They need ffmpeg
and ffprobe (skipped otherwise; run them where ffmpeg is installed)."""
import asyncio
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from local_recording_policy import keyframe_cut, recording_name_time

T0 = datetime(2026, 10, 1, 1, 30, 0)
FPS = 30
W, H = 160, 120


# ------------------------------------------------------------ pure rules

def test_cut_starts_on_the_last_keyframe_at_or_before_the_request():
    start = T0
    assert keyframe_cut(start + timedelta(seconds=13.3), start, [0, 4, 8, 12, 16]) == (12, start + timedelta(seconds=12))
    assert keyframe_cut(start + timedelta(seconds=12), start, [0, 4, 8, 12, 16]) == (12, start + timedelta(seconds=12))
    assert keyframe_cut(start + timedelta(seconds=3), start, []) == (0.0, start)       # unknown: segment start
    assert keyframe_cut(start - timedelta(seconds=2), start, [0, 4]) == (0, start)      # footage begins later


def test_recording_label_rounds_up_so_clips_err_early_never_late():
    assert recording_name_time(T0) == T0
    assert recording_name_time(T0 + timedelta(seconds=44.25)) == T0 + timedelta(seconds=45)


# ------------------------------------------------------------ real media

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                  reason="needs ffmpeg/ffprobe")


def _frame_number(path: Path, *, from_end: bool = False) -> int:
    args = ["ffmpeg", "-v", "error"] + (["-sseof", "-0.05"] if from_end else []) + [
        "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "yuv444p", "-"]
    raw = subprocess.run(args, check=True, capture_output=True).stdout
    centre = (H // 2) * W + W // 2
    return round((raw[W * H + centre] - 16) / 12) * 200 + (raw[centre] - 16)


@pytest.fixture()
def buffer(tmp_path, monkeypatch):
    """100 s of camera-like video (30 fps, keyframe every 4 s), frame number
    in the pixels, split into 30 s Event-mode buffer segments named by their
    real start time -- exactly how start_event_recording_buffer() writes."""
    import main
    source = tmp_path / "camera.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"nullsrc=s={W}x{H}:r={FPS},format=yuv444p,geq=lum='16+mod(N,200)':cb='16+12*floor(N/200)':cr=128",
                    "-t", "100", "-c:v", "libx264", "-pix_fmt", "yuv444p", "-g", "120", "-keyint_min", "120",
                    "-sc_threshold", "0", "-bf", "0", "-qp", "0", str(source)], check=True)
    staging = tmp_path / "seg"
    staging.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-map", "0:v:0", "-c:v", "copy", "-f", "segment",
                    "-segment_time", "30", "-reset_timestamps", "1", str(staging / "s_%03d.mkv")], check=True)
    folder = tmp_path / "recordings" / "camera1" / main.EVENT_BUFFER_SUBFOLDER_NAME
    folder.mkdir(parents=True)
    for segment in sorted(staging.iterdir()):
        start = T0 + timedelta(seconds=_frame_number(segment) / FPS)
        segment.rename(folder / f"buf1_{start:%Y-%m-%d_%H-%M-%S}.mkv")
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    monkeypatch.setattr(main, "CLIPS_FOLDER", tmp_path / "clips")

    async def no_wait(seconds):
        return None

    monkeypatch.setattr(main.asyncio, "sleep", no_wait)
    main._open_event_recordings.pop(1, None)
    main._event_recording_locks.pop(1, None)
    yield main
    main._open_event_recordings.pop(1, None)


def _at(frame: int) -> datetime:
    return T0 + timedelta(seconds=frame / FPS)


@needs_ffmpeg
@pytest.mark.parametrize("request_offset", [45.3, 52.9, 66.1])
def test_recording_first_frame_is_the_time_its_name_says(buffer, request_offset):
    main = buffer
    event_start = T0 + timedelta(seconds=request_offset + 5)   # pre-roll 5 s -> requested start = request_offset
    asyncio.run(main.persist_event_recording(1, event_start, event_start + timedelta(seconds=5)))
    recording = main._open_event_recordings[1]["path"]
    true_start = main._open_event_recordings[1]["start"]
    first = _at(_frame_number(recording))
    label = main.recording_start(recording, 1)
    assert abs((first - true_start).total_seconds()) < 0.05            # the stored start is the real first frame
    assert first <= T0 + timedelta(seconds=request_offset)             # nothing requested is lost at the start
    assert 0 <= (label - first).total_seconds() < 1.0                  # label = first frame, rounded up < 1 s
    last = _at(_frame_number(recording, from_end=True))
    assert abs((last - (event_start + timedelta(seconds=10))).total_seconds()) < 0.2   # ends at the window end


@needs_ffmpeg
def test_event_clip_cut_from_the_recording_contains_the_arrival_and_the_detection(buffer):
    """The reported case: the object arrives between two scans, is detected
    on the second; the clip must start before the arrival and include the
    detection frame."""
    main = buffer
    previous_scan, arrival, detection = (T0 + timedelta(seconds=s) for s in (58.0, 61.0, 64.0))
    asyncio.run(main.persist_event_recording(1, previous_scan, detection))
    clip_url = asyncio.run(main.build_motion_event_clip("evt-true-start", 1, previous_scan, detection))
    assert clip_url
    clip = main.CLIPS_FOLDER / "motion" / "motion_evt-true-start.mp4"
    first, last = _at(_frame_number(clip)), _at(_frame_number(clip, from_end=True))
    requested_start = previous_scan - timedelta(seconds=5)
    assert requested_start - timedelta(seconds=1) <= first <= requested_start + timedelta(seconds=0.1)
    assert first < arrival                                  # empty scene first, then the arrival
    assert last >= detection                                # and through the detection
