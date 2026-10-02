"""Smart Rules "Capture frame" (2026-09-28). The drawing canvas covered the
live video with solid black and relied solely on drawing the <video> into
a canvas -- nothing until the relay's first segment decoded, no status,
and a black frame on browsers that cannot draw HLS video. A server-side
still (newest relay/local segment -> one JPEG) now backs it up."""
import subprocess
import time

import pytest

import customer_analytics_rules
from test_live_playlist_cloud_relay_path import (
    _client, _fake_rsa_signer, _owner_cookie, _runtime_role, _seed, connection, live_playlist, override_target, partner_portal,
)

JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "smart_rules_capture.db"


@pytest.fixture()
def fake_ffmpeg(monkeypatch):
    seen = {}
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "ffmpeg" and "mjpeg" in cmd:
            seen["input"] = kwargs.get("input")
            return type("R", (), {"stdout": JPEG if kwargs.get("input") else b""})()
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def _get(client, camera="cam-1"):
    return client.get(f"/api/customer/cameras/{camera}/live/still.jpg", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})


def test_cloud_still_comes_from_the_newest_relay_segment(db_path, monkeypatch, fake_ffmpeg):
    monkeypatch.setenv(live_playlist.CLOUDFRONT_URL_ENV, "https://d123456.cloudfront.net")
    monkeypatch.setenv(live_playlist.CLOUDFRONT_KEY_PAIR_ID_ENV, "APKAFAKE")
    monkeypatch.setattr(live_playlist, "get_configured_signer", lambda: _fake_rsa_signer)
    fetched = []

    class Resp:
        ok = True
        content = b"newest-ts-bytes"

    import requests
    monkeypatch.setattr(requests, "get", lambda url, timeout=None: fetched.append(url) or Resp())
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            _seed(db)
        prefix = live_playlist.live_relay_s3_prefix("cust-1", "site-1", "appl-1", "cam-1")
        monkeypatch.setattr(live_playlist.live_manifest_store, "manifest_for", lambda camera_id: {"segments": [
            {"sequence": 1, "duration": 2.0, "key": f"{prefix}camera1_000001.ts"},
            {"sequence": 2, "duration": 2.0, "key": f"{prefix}camera1_000002.ts"},
        ], "updated_at": time.time()})
        with override_target(sqlite_path=str(db_path)), _runtime_role("cloud"):
            response = _get(_client(hls_folder=None))
    assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg" and response.content == JPEG
    assert fetched and "camera1_000002" in fetched[-1]  # the newest segment
    assert fake_ffmpeg["input"] == b"newest-ts-bytes"


def test_appliance_still_comes_from_a_finished_local_segment_and_never_camera10(db_path, monkeypatch, tmp_path, fake_ffmpeg):
    monkeypatch.delenv(live_playlist.CLOUDFRONT_URL_ENV, raising=False)
    monkeypatch.delenv(live_playlist.CLOUDFRONT_KEY_PAIR_ID_ENV, raising=False)
    monkeypatch.setattr(live_playlist, "get_configured_signer", lambda: None)
    hls = tmp_path / "hls"
    hls.mkdir()
    for i, name in enumerate(("camera1_000001.ts", "camera1_000002.ts", "camera1_000003.ts", "camera10_000009.ts")):
        path = hls / name
        path.write_bytes(name.encode())
        t = time.time() - 10 + i
        import os
        os.utime(path, (t, t))
    identity = {"credential": "cred", "appliance_id": "appl-1", "cloud_id": "AIC-1"}
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            _seed(db)
        with override_target(sqlite_path=str(db_path)), _runtime_role("edge"):
            response = _get(_client(hls_folder=str(hls), local_identity=lambda: identity))
    assert response.status_code == 200 and response.content == JPEG
    assert fake_ffmpeg["input"] == b"camera1_000002.ts"  # the newest FINISHED segment, never camera10's


def test_no_frame_available_fails_closed_with_a_clear_503(db_path, monkeypatch, fake_ffmpeg):
    monkeypatch.setenv(live_playlist.CLOUDFRONT_URL_ENV, "https://d123456.cloudfront.net")
    monkeypatch.setenv(live_playlist.CLOUDFRONT_KEY_PAIR_ID_ENV, "APKAFAKE")
    monkeypatch.setattr(live_playlist, "get_configured_signer", lambda: _fake_rsa_signer)
    monkeypatch.setattr(live_playlist.live_manifest_store, "manifest_for", lambda camera_id: {"segments": [], "updated_at": None})
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            _seed(db)
        with override_target(sqlite_path=str(db_path)), _runtime_role("cloud"):
            response = _get(_client(hls_folder=None))
    assert response.status_code == 503 and "No live frame" in response.json()["detail"]


def test_still_is_tenant_scoped(db_path, monkeypatch, fake_ffmpeg):
    monkeypatch.setattr(live_playlist, "get_configured_signer", lambda: _fake_rsa_signer)
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            _seed(db)
        client = _client(hls_folder=None)
        with override_target(sqlite_path=str(db_path)), _runtime_role("cloud"):
            other = client.get("/api/customer/cameras/cam-1/live/still.jpg",
                               cookies={partner_portal.SESSION_COOKIE: partner_portal._token("x@example.test", "customer_owner", None, "cust-2", None)})
            anonymous = client.get("/api/customer/cameras/cam-1/live/still.jpg")
    assert other.status_code in (403, 404) and anonymous.status_code == 403


def test_rules_page_shows_the_live_preview_and_falls_back_to_the_server_still():
    import inspect
    source = inspect.getsource(customer_analytics_rules)
    assert "/live/still.jpg" in source and "captureFromStill" in source
    assert "id=\"preview-status\"" in source
    assert "ctx.clearRect(0,0,canvas.width,canvas.height)" in source  # canvas no longer paints over the live video
    assert "setTimeout(()=>{if(!hasFrame)captureFrame();},800)" in source  # first frame captured automatically
    assert "alert('Live preview is not ready yet.')" not in source
    assert "frameIsBlank()" in source  # a black browser capture falls back to the server still


def test_a_stalled_preview_falls_back_to_the_server_still_after_a_timeout():
    import inspect
    source = inspect.getsource(customer_analytics_rules)
    assert "setTimeout(()=>{if(!hasFrame)previewUnavailable();},15000)" in source
