"""Windows 1.3.2 (2026-10-21, Blue): the AI frame reader never falls back to the live HLS playlist on Windows -- the
same defect class as the motion reader (functional Sandbox D2: a reader of camera<N>.m3u8 freezes the live playlist
there). Separate from test_windows_live_readers_132.py so this guard can be reviewed or dropped on its own."""
import analytics_frames


def _frames(windows, mode="main"):
    return analytics_frames.AnalyticsFrames(lambda n: None, mode=mode, windows=windows, start_housekeeping=False,
                                            log=lambda message: logged.append(message))


logged: list = []


def test_windows_ai_frames_never_use_the_hls_playlist():
    logged.clear()
    frames = _frames(True, mode="hls")
    assert frames.mode == "main" and frames.uses_hls(1) is False
    frames = _frames(True)
    for _ in range(analytics_frames.MAX_FAILURES):
        frames._note_run(1, 0, "no frames")
    assert frames.uses_hls(1) is False
    assert logged and "never read" in logged[-1]


def test_linux_ai_frames_still_fall_back_to_hls_after_failures():
    logged.clear()
    frames = _frames(False)
    for _ in range(analytics_frames.MAX_FAILURES):
        frames._note_run(1, 0, "no frames")
    assert frames.uses_hls(1) is True
    assert logged and "using the live HLS stream" in logged[-1]


