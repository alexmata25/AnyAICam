"""Owner decisions after Codex review (2026-10-11):
  * analytics stream: smallest stream >= 640 px; a 480-639 px substream only
    as a fallback behind ANYAICAM_AI_ALLOW_480_SUBSTREAM; else the main
    stream; which stream each camera uses is logged and exposed;
  * measured resolution/codec/fps shown in customer, partner and admin
    camera views ("configured (not yet measured)" for ONVIF-only values,
    "Not yet measured" otherwise -- never the sales label)."""
import sqlite3
import types

import pytest

import analytics_frames
import camera_capabilities
import video_profile
from database_backend import override_target
from test_camera_multi_appliance_isolation import (  # noqa: F401  (pytest fixtures)
    _activate, _owner_cookie, _seed_world, client, db_path,
)

SECRET = "pw-480"
MAIN_URL = f"rtsp://admin:{SECRET}@192.0.2.40:554/Streaming/Channels/101"
CLEAN_MAIN = camera_capabilities.split_credentials(MAIN_URL)[0]
SUB_URL = MAIN_URL.replace("/101", "/102")


def _record(*subs, main=(3840, 2160)):
    streams = [{"uri": CLEAN_MAIN, "role": "main", "measured": {"width": main[0], "height": main[1], "codec": "H265"}}]
    for index, (w, h) in enumerate(subs):
        streams.append({"uri": f"rtsp://192.0.2.40:554/sub{index}", "role": "sub", "measured": {"width": w, "height": h, "codec": "H265"}})
    return {"schema": 1, "video_probe": {"status": "ok", "at": "2026-10-11T00:00:00+00:00"}, "streams": streams}


# ------------------------------------------------------------------ 480 policy

@pytest.mark.parametrize("subs,allow,expected_uri,reason", [
    ([(640, 360), (1280, 720)], False, "sub0", "substream_640"),        # smallest >= 640
    ([(480, 270), (1280, 720)], True, "sub1", "substream_640"),         # >= 640 always preferred over 480
    ([(480, 270)], False, None, "main_480_fallback_disabled"),
    ([(480, 270)], True, "sub0", "substream_480_fallback"),
    ([(480, 270), (560, 315)], True, "sub1", "substream_480_fallback"),  # the best (widest) 480-639 stream
    ([(352, 240)], True, None, "main_no_usable_substream"),
    ([], True, None, "main_no_usable_substream"),
])
def test_analytics_choice_prefers_640_then_480_fallback_then_main(subs, allow, expected_uri, reason):
    stream, why = video_profile.analytics_choice(_record(*subs), allow_480=allow)
    assert why == reason
    if expected_uri is None:
        assert stream["role"] == "main"
    else:
        assert stream["uri"].endswith(expected_uri)


def test_480_fallback_is_off_by_default():
    assert video_profile.ALLOW_480_SUBSTREAM is False
    profile = video_profile.build(_record((480, 270)))
    assert profile.analytics_is_main and profile.analytics_reason == "main_480_fallback_disabled"
    assert profile.summary()["analytics_choice"] == "main_480_fallback_disabled"


def test_unmeasured_camera_480_substream_follows_the_flag(monkeypatch):
    import main
    monkeypatch.setattr(main.ai_frames, "mode", "auto")  # opt-in substream policy
    monkeypatch.setattr(main, "camera_url", lambda n: MAIN_URL)
    monkeypatch.setattr(main, "camera_video_profiles", types.SimpleNamespace(
        record=lambda n: None, profile=lambda n: video_profile.build(None)))
    monkeypatch.setattr(main, "motion_substream_url", lambda n: SUB_URL)
    sizes = {SUB_URL: {"width": 480, "height": 270, "codec": "H265", "fps": 15.0},
             MAIN_URL: {"width": 3840, "height": 2160, "codec": "H265", "fps": 20.0}}
    monkeypatch.setattr(main.camera_video_probe, "ffprobe_stream", lambda url: sizes.get(url))
    monkeypatch.setattr(video_profile, "ALLOW_480_SUBSTREAM", False)
    spec = main.analytics_source_spec(1)
    assert (spec.kind, spec.width, spec.reason) == ("main", 1280, "main_480_fallback_disabled")
    monkeypatch.setattr(video_profile, "ALLOW_480_SUBSTREAM", True)
    spec = main.analytics_source_spec(1)
    assert (spec.kind, spec.width, spec.height, spec.reason) == ("substream", 480, 270, "substream_480_fallback")
    sizes[SUB_URL] = {"width": 352, "height": 240, "codec": "H264", "fps": 15.0}
    assert main.analytics_source_spec(1).reason == "main_no_usable_substream"
    sizes[SUB_URL] = {"width": 640, "height": 360, "codec": "H265", "fps": 15.0}
    assert main.analytics_source_spec(1).reason == "substream_640"


def test_reader_logs_and_exposes_why_it_reads_that_stream():
    logs = []

    class Proc:
        stdout = None

        def __init__(self):
            import io
            self.stdout = io.BytesIO(b"\x00" * (8 * 6 * 3))
            self.returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return 0

    spec = analytics_frames.SourceSpec("rtsp://u:p@cam/101", 8, 6, "main", "main_480_fallback_disabled")
    reader = analytics_frames.FrameReader(4, lambda n: spec, popen=lambda *a, **k: Proc(), fps=2.0, log=logs.append)
    reader.ensure_running()
    assert reader.latest(max_age=30, wait=5) is not None
    reader.stop()
    assert any("main stream" in line and "main_480_fallback_disabled" in line for line in logs)
    assert not any("u:p@" in line for line in logs)
    frames = analytics_frames.AnalyticsFrames(lambda n: spec, mode="auto", reader_factory=lambda n: reader, start_housekeeping=False)
    frames._reader(4)
    assert frames.state(4)["reason"] == "main_480_fallback_disabled"


def test_ai_status_api_exposes_the_frame_source(monkeypatch):
    import main
    monkeypatch.setattr(main, "get_camera_numbers", lambda *a, **k: [1])
    monkeypatch.setitem(main.ai_detection_state, 1, {"status": "running"})
    monkeypatch.setattr(main.ai_frames, "state", lambda n: {"mode": "auto", "kind": "substream", "reason": "substream_640"})
    body = main.ai_detection_status()
    assert body["cameras"][0]["frame_source"]["reason"] == "substream_640"


# ------------------------------------------------------------------ display

def test_admin_camera_video_html_lists_measured_configured_and_unmeasured():
    from html import escape
    html = video_profile.admin_camera_video_html([
        {"name": "Driveway", "measured_width": 3840, "measured_height": 2160, "measured_codec": "H265", "measured_fps": 20,
         "measured_source": "measured", "measured_analytics_choice": "substream_640"},
        {"name": "Door", "measured_width": 1920, "measured_height": 1080, "measured_source": "configured"},
        {"name": "<New>", "status": "configured"},
        {"name": "Gone", "status": "removed"},
    ], escape)
    assert "Camera video (3)" in html
    assert "3840x2160 H265 20 fps (8MP) &middot; AI: substream (&gt;= 640 px)" in html
    assert "1920x1080 (2MP) - configured (not yet measured)" in html
    assert "&lt;New&gt; &middot; Not yet measured" in html and "Gone" not in html


def test_admin_and_partner_views_render_the_video_lines():
    import appliance_cloud
    import main
    import partner_workspace
    for module in (main, appliance_cloud):
        source = open(module.__file__, encoding="utf-8").read()
        assert "video_profile.admin_camera_video_html(" in source and "video_profile.ADMIN_CAMERA_VIDEO_SQL" in source
    source = open(partner_workspace.__file__, encoding="utf-8").read()
    assert "<th>Video (measured resolution, codec, fps)</th>" in source and "video_profile.display_video(c)" in source


def _measure(db_path, camera_id, **values):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        sets = ",".join(f"{k}=?" for k in values)
        conn.execute(f"UPDATE cameras SET {sets} WHERE id=?", (*values.values(), camera_id))
        conn.commit()


def test_customer_live_tiles_and_camera_page_show_measured_video(client, db_path):
    _seed_world(db_path)
    _activate(db_path, "appl-ryzen")
    cookie = {"anyaicam_partner_session": _owner_cookie()}
    page = client.get("/customer-live", params={"appliance_id": "appl-ryzen"}, cookies=cookie).text
    assert 'title="Video: Not yet measured"' in page            # never the '4mp' label (tooltip only, 2026-10-16)
    _measure(db_path, "cam-ryzen-1", measured_width=3840, measured_height=2160, measured_codec="H265", measured_fps=20.0,
             measured_resolution_class="8mp", measured_source="measured")
    page = client.get("/customer-live", params={"appliance_id": "appl-ryzen"}, cookies=cookie).text
    assert 'title="Video: 3840x2160 H265 20 fps (8MP)"' in page
    single = client.get("/customer/cameras/cam-ryzen-1/live", cookies=cookie)
    assert single.status_code == 200
    assert "Video: 3840x2160 H265 20 fps (8MP)" in single.text
    _measure(db_path, "cam-ryzen-1", measured_source="configured")
    single = client.get("/customer/cameras/cam-ryzen-1/live", cookies=cookie).text
    assert "Video: 3840x2160 H265 20 fps (8MP) - configured (not yet measured)" in single


def test_setup_wizard_shows_measured_or_not_yet_measured(client, db_path):
    _seed_world(db_path)
    cookie = {"anyaicam_partner_session": _owner_cookie()}
    page = client.get("/customer/setup", params={"appliance": "appl-ryzen"}, cookies=cookie)
    assert page.status_code == 200
    assert "<th>Video (measured)</th>" in page.text and "Not yet measured" in page.text
    assert ">4mp<" not in page.text.lower()
