"""WireGuard-carried live view (2026-09-18): security/regression coverage
for live_view_wireguard.py -- the fourth, opportunistic live-view
transport that fetches the appliance's own local HLS over the cloud
gateway's internal WireGuard proxy instead of via S3/CloudFront.

These tests exercise the real customer-facing routes through the real
app (main.app), matching test_live_view_p2p_signaling.py's established
pattern -- partner_identity()'s cookie auth is easiest exercised end to
end, and a real POST .../live/start call exercises the real
authorization/camera-resolution path this module's own routes depend on,
rather than hand-seeding a live_view_sessions row that could silently
drift from the real schema.

No real WireGuard tunnel, gateway process, or appliance is involved --
_fetch_via_gateway() is monkeypatched per test to return canned bytes or
raise GatewayUnavailable, exactly like relay/P2P tests already stub their
own network boundary (see test_live_relay_session_endpoint.py's own
_control_plane_post stubbing for the established precedent).
"""

import pytest
from fastapi.testclient import TestClient

import main
import live_view_wireguard
import partner_portal
import wireguard_remote
from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_live_view_wireguard.db"


def _seed(conn):
    now = "2026-09-18T00:00:00"
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','partner-1','Customer A','a@example.test','active',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-b','partner-1','Customer B','b@example.test','active',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-a','Main',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-b','cust-b','Main',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-a','cust-a','site-a','AIC-A',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-b','cust-b','site-b','AIC-B',?)", (now,))
    # Two cameras on appliance-a: cam-a (camera_number=1, the controlled
    # allow-listed camera) and cam-a2 (camera_number=2, same tenant, NOT
    # on the allow-list -- the "wrong camera" case).
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,device_key,status,name,created_at) "
        "VALUES('cam-a','cust-a','site-a','appl-a',1,'urn:uuid:fake-a','configured','Camera 1',?)", (now,)
    )
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,device_key,status,name,created_at) "
        "VALUES('cam-a2','cust-a','site-a','appl-a',2,'urn:uuid:fake-a2','configured','Camera 2',?)", (now,)
    )
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,device_key,status,name,created_at) "
        "VALUES('cam-b','cust-b','site-b','appl-b',1,'urn:uuid:fake-b','configured','Camera B1',?)", (now,)
    )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
        "VALUES('user-a','owner-a@example.test','customer_owner','cust-a','x','all',?)", (now,)
    )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
        "VALUES('user-b','owner-b@example.test','customer_owner','cust-b','x','all',?)", (now,)
    )
    conn.commit()
    # Active WireGuard tunnel for appliance-a only -- appliance-b has no
    # peer row at all, exercising the "no active tunnel" case.
    from datetime import datetime
    wireguard_remote.enroll_peer(
        conn, appliance_id='appl-a', customer_id='cust-a',
        public_key='A' * 43 + '=', now=datetime.fromisoformat(now),
    )
    conn.commit()


def _owner_cookie(customer_id, email):
    return partner_portal._token(email, "customer_owner", None, customer_id, None)


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with override_target(sqlite_path=str(db_path)):
            from partner_db import connection
            with connection() as conn:
                _seed(conn)
        from cloud_config import settings
        trusted = settings.effective_trusted_hosts or []
        allowed_host = "testserver" if ("*" in trusted or "testserver" in trusted or not trusted) else trusted[0]
        with TestClient(main.app, base_url=f"http://{allowed_host}") as test_client:
            yield test_client


def _start_session(client, customer_id, email, camera_id):
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie(customer_id, email)}
    response = client.post(f"/api/customer/cameras/{camera_id}/live/start", cookies=cookies)
    assert response.status_code == 200, response.text
    return response.json()["session_id"], cookies


@pytest.fixture(autouse=True)
def _enable_flags(monkeypatch):
    # Every test explicitly opts into the flags/allow-list/gateway URL it
    # needs -- the true default (everything off/empty) is itself tested
    # by test_disabled_by_default_even_with_valid_session below.
    monkeypatch.setattr(live_view_wireguard, "GATEWAY_INTERNAL_URL", "http://gateway.invalid:8098")
    yield


# ---------------------------------------------------------------------------
# Config endpoint + disabled-by-default
# ---------------------------------------------------------------------------

def test_config_requires_customer_auth(client):
    # No cookie at all -- caught by the platform's own session middleware
    # before this route's _customer_identity() ever runs (see
    # test_live_view_p2p_signaling.py's own identical 401 expectation for
    # a totally-unauthenticated request).
    response = client.get("/api/customer/live/wireguard/config")
    assert response.status_code == 401


def test_config_reflects_the_feature_flag(client, monkeypatch):
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    assert client.get("/api/customer/live/wireguard/config", cookies=cookies).json() == {"enabled": False}
    monkeypatch.setattr(live_view_wireguard, "LIVE_WIREGUARD_ENABLED", True)
    assert client.get("/api/customer/live/wireguard/config", cookies=cookies).json() == {"enabled": True}


def test_disabled_by_default_even_with_valid_session(client):
    # No monkeypatching of LIVE_WIREGUARD_ENABLED/allow-list at all --
    # this is the real, shipped default. A completely valid, authorized
    # session for the eventual controlled camera must still 404.
    session_id, cookies = _start_session(client, "cust-a", "owner-a@example.test", "cam-a")
    response = client.get(f"/api/customer/live/sessions/{session_id}/wireguard/playlist.m3u8", cookies=cookies)
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Authentication / tenant isolation / camera allow-list
# ---------------------------------------------------------------------------

def test_playlist_unauthenticated_denied(client, monkeypatch):
    monkeypatch.setattr(live_view_wireguard, "LIVE_WIREGUARD_ENABLED", True)
    monkeypatch.setattr(live_view_wireguard, "_ALLOWED_LIVE_CAMERA_IDS", {"cam-a"})
    response = client.get("/api/customer/live/sessions/does-not-matter/wireguard/playlist.m3u8")
    assert response.status_code == 401


def test_playlist_cross_tenant_denied(client, monkeypatch):
    monkeypatch.setattr(live_view_wireguard, "LIVE_WIREGUARD_ENABLED", True)
    monkeypatch.setattr(live_view_wireguard, "_ALLOWED_LIVE_CAMERA_IDS", {"cam-a"})
    session_id, _ = _start_session(client, "cust-a", "owner-a@example.test", "cam-a")
    # Customer B's own valid cookie, but this session belongs to customer A.
    other_cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-b", "owner-b@example.test")}
    response = client.get(f"/api/customer/live/sessions/{session_id}/wireguard/playlist.m3u8", cookies=other_cookies)
    assert response.status_code == 404


def test_playlist_wrong_camera_not_on_allowlist(client, monkeypatch):
    monkeypatch.setattr(live_view_wireguard, "LIVE_WIREGUARD_ENABLED", True)
    monkeypatch.setattr(live_view_wireguard, "_ALLOWED_LIVE_CAMERA_IDS", {"cam-a"})  # cam-a2 is NOT allow-listed
    session_id, cookies = _start_session(client, "cust-a", "owner-a@example.test", "cam-a2")
    response = client.get(f"/api/customer/live/sessions/{session_id}/wireguard/playlist.m3u8", cookies=cookies)
    assert response.status_code == 404


def test_playlist_no_active_tunnel_returns_503(client, monkeypatch):
    monkeypatch.setattr(live_view_wireguard, "LIVE_WIREGUARD_ENABLED", True)
    monkeypatch.setattr(live_view_wireguard, "_ALLOWED_LIVE_CAMERA_IDS", {"cam-b"})
    # appliance-b has no appliance_wireguard_peers row at all (see _seed).
    session_id, cookies = _start_session(client, "cust-b", "owner-b@example.test", "cam-b")
    response = client.get(f"/api/customer/live/sessions/{session_id}/wireguard/playlist.m3u8", cookies=cookies)
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# Gateway reachability
# ---------------------------------------------------------------------------

def test_playlist_gateway_unavailable_is_a_clean_503_not_500(client, monkeypatch):
    monkeypatch.setattr(live_view_wireguard, "LIVE_WIREGUARD_ENABLED", True)
    monkeypatch.setattr(live_view_wireguard, "_ALLOWED_LIVE_CAMERA_IDS", {"cam-a"})

    def _raise(*args, **kwargs):
        raise live_view_wireguard.GatewayUnavailable("simulated: gateway unreachable")

    monkeypatch.setattr(live_view_wireguard, "_fetch_via_gateway", _raise)
    session_id, cookies = _start_session(client, "cust-a", "owner-a@example.test", "cam-a")
    response = client.get(f"/api/customer/live/sessions/{session_id}/wireguard/playlist.m3u8", cookies=cookies)
    assert response.status_code == 503
    # Fail-safe means the *session itself* is completely untouched --
    # this failure must never be able to affect the relay/P2P transports
    # racing in parallel for the same session.
    from partner_db import connection
    with connection() as db:
        row = dict(db.execute("SELECT state,transport,failed_at FROM live_view_sessions WHERE id=?", (session_id,)).fetchone())
    assert row["state"] == "requested"
    assert row["transport"] == "not_configured"
    assert row["failed_at"] is None


# ---------------------------------------------------------------------------
# Playlist sanitization / rewriting (never trust the appliance's own text)
# ---------------------------------------------------------------------------

def test_playlist_sanitizes_untrusted_content_and_rewrites_segment_urls(client, monkeypatch):
    monkeypatch.setattr(live_view_wireguard, "LIVE_WIREGUARD_ENABLED", True)
    monkeypatch.setattr(live_view_wireguard, "_ALLOWED_LIVE_CAMERA_IDS", {"cam-a"})

    malicious_manifest = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:6\n"
        "#EXT-X-KEY:METHOD=AES-128,URI=\"https://evil.example/key\"\n"  # must be dropped
        "#EXTINF:5.600000,\n"
        "camera1_000000001.ts\n"
        "#EXTINF:5.600000,\n"
        "https://evil.example/steal.ts\n"  # not a bare segment name -- must be dropped
        "#EXTINF:5.600000,\n"
        "camera2_000000002.ts\n"  # wrong camera number for this session -- must be dropped
        "#EXTINF:5.600000,\n"
        "camera1_000000003.ts\n"
    ).encode()

    monkeypatch.setattr(live_view_wireguard, "_fetch_via_gateway", lambda *a, **k: malicious_manifest)
    session_id, cookies = _start_session(client, "cust-a", "owner-a@example.test", "cam-a")
    response = client.get(f"/api/customer/live/sessions/{session_id}/wireguard/playlist.m3u8", cookies=cookies)
    assert response.status_code == 200
    body = response.text
    assert "evil.example" not in body
    assert "EXT-X-KEY" not in body
    assert f"/api/customer/live/sessions/{session_id}/wireguard/camera1_000000001.ts" in body
    assert f"/api/customer/live/sessions/{session_id}/wireguard/camera1_000000003.ts" in body
    assert "camera2_000000002.ts" not in body


# ---------------------------------------------------------------------------
# Segment fetch: filename validation
# ---------------------------------------------------------------------------

def test_segment_malformed_filename_rejected(client, monkeypatch):
    monkeypatch.setattr(live_view_wireguard, "LIVE_WIREGUARD_ENABLED", True)
    monkeypatch.setattr(live_view_wireguard, "_ALLOWED_LIVE_CAMERA_IDS", {"cam-a"})
    session_id, cookies = _start_session(client, "cust-a", "owner-a@example.test", "cam-a")
    for bad_name in ("not-a-segment.ts", "camera1_abc.ts", "camera1_1.mp4", "..%2f..%2fetc%2fpasswd"):
        response = client.get(f"/api/customer/live/sessions/{session_id}/wireguard/{bad_name}", cookies=cookies)
        assert response.status_code == 404, bad_name


def test_segment_wrong_camera_number_rejected(client, monkeypatch):
    monkeypatch.setattr(live_view_wireguard, "LIVE_WIREGUARD_ENABLED", True)
    monkeypatch.setattr(live_view_wireguard, "_ALLOWED_LIVE_CAMERA_IDS", {"cam-a"})
    session_id, cookies = _start_session(client, "cust-a", "owner-a@example.test", "cam-a")  # camera_number=1
    response = client.get(f"/api/customer/live/sessions/{session_id}/wireguard/camera2_000000001.ts", cookies=cookies)
    assert response.status_code == 404


def test_segment_fetch_success(client, monkeypatch):
    monkeypatch.setattr(live_view_wireguard, "LIVE_WIREGUARD_ENABLED", True)
    monkeypatch.setattr(live_view_wireguard, "_ALLOWED_LIVE_CAMERA_IDS", {"cam-a"})
    monkeypatch.setattr(live_view_wireguard, "_fetch_via_gateway", lambda *a, **k: b"\x47" * 188)  # one fake TS packet
    session_id, cookies = _start_session(client, "cust-a", "owner-a@example.test", "cam-a")
    response = client.get(f"/api/customer/live/sessions/{session_id}/wireguard/camera1_000000001.ts", cookies=cookies)
    assert response.status_code == 200
    assert response.content == b"\x47" * 188
    assert response.headers["content-type"].startswith("video/mp2t")


# ---------------------------------------------------------------------------
# Event-clip support (prepared for the later Hybrid-cost measurement)
# ---------------------------------------------------------------------------

_APPLIANCE_AND_SITE_BY_CAMERA = {
    "cam-a": ("appl-a", "site-a", "cust-a"),
    "cam-a2": ("appl-a", "site-a", "cust-a"),
    "cam-b": ("appl-b", "site-b", "cust-b"),
}


def _insert_event_media(conn, *, media_id, customer_id, camera_id, local_relative_path):
    now = "2026-09-18T00:00:00"
    appliance_id, site_id, _ = _APPLIANCE_AND_SITE_BY_CAMERA[camera_id]
    event_id = f"evt-{media_id}"
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,"
        "event_type,confidence,object_count,detections_json,event_timestamp,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, customer_id, site_id, appliance_id, camera_id, f"local-{media_id}",
         "motion", 0.9, 1, "[]", now, now),
    )
    conn.execute(
        "INSERT INTO detection_event_media(id,detection_event_id,customer_id,camera_id,s3_key,"
        "thumbnail_s3_key,started_at,ended_at,duration_seconds,size_bytes,local_relative_path,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (media_id, event_id, customer_id, camera_id, f"clips/{media_id}.mp4",
         None, now, now, 10.0, 12345, local_relative_path, now),
    )
    conn.commit()


def test_event_media_disabled_by_default(client):
    from partner_db import connection
    with connection() as db:
        _insert_event_media(db, media_id="media-1", customer_id="cust-a", camera_id="cam-a",
                             local_relative_path="/recordings/clips/motion/media-1.mp4")
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    response = client.get("/api/customer/event-media/media-1/wireguard", cookies=cookies)
    assert response.status_code == 404


def test_event_media_cross_tenant_denied(client, monkeypatch):
    monkeypatch.setattr(live_view_wireguard, "EVENT_MEDIA_WIREGUARD_ENABLED", True)
    from partner_db import connection
    with connection() as db:
        _insert_event_media(db, media_id="media-2", customer_id="cust-a", camera_id="cam-a",
                             local_relative_path="/recordings/clips/motion/media-2.mp4")
    other_cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-b", "owner-b@example.test")}
    response = client.get("/api/customer/event-media/media-2/wireguard", cookies=other_cookies)
    assert response.status_code == 404


def test_event_media_missing_local_path_is_a_clean_404(client, monkeypatch):
    # The realistic case for every clip uploaded before this feature
    # existed: local_relative_path is NULL, never a guessed/derived value.
    monkeypatch.setattr(live_view_wireguard, "EVENT_MEDIA_WIREGUARD_ENABLED", True)
    from partner_db import connection
    with connection() as db:
        _insert_event_media(db, media_id="media-3", customer_id="cust-a", camera_id="cam-a",
                             local_relative_path=None)
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    response = client.get("/api/customer/event-media/media-3/wireguard", cookies=cookies)
    assert response.status_code == 404


def test_event_media_malformed_local_path_rejected(client, monkeypatch):
    monkeypatch.setattr(live_view_wireguard, "EVENT_MEDIA_WIREGUARD_ENABLED", True)
    from partner_db import connection
    with connection() as db:
        _insert_event_media(db, media_id="media-4", customer_id="cust-a", camera_id="cam-a",
                             local_relative_path="/recordings/../../etc/passwd")
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    response = client.get("/api/customer/event-media/media-4/wireguard", cookies=cookies)
    assert response.status_code == 404


def test_event_media_success(client, monkeypatch):
    monkeypatch.setattr(live_view_wireguard, "EVENT_MEDIA_WIREGUARD_ENABLED", True)
    monkeypatch.setattr(live_view_wireguard, "_fetch_via_gateway", lambda *a, **k: b"fake-mp4-bytes")
    from partner_db import connection
    with connection() as db:
        _insert_event_media(db, media_id="media-5", customer_id="cust-a", camera_id="cam-a",
                             local_relative_path="/recordings/clips/motion/media-5.mp4")
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    response = client.get("/api/customer/event-media/media-5/wireguard", cookies=cookies)
    assert response.status_code == 200
    assert response.content == b"fake-mp4-bytes"


def test_event_media_no_active_tunnel_returns_503(client, monkeypatch):
    monkeypatch.setattr(live_view_wireguard, "EVENT_MEDIA_WIREGUARD_ENABLED", True)
    from partner_db import connection
    with connection() as db:
        _insert_event_media(db, media_id="media-6", customer_id="cust-b", camera_id="cam-b",
                             local_relative_path="/recordings/clips/motion/media-6.mp4")
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-b", "owner-b@example.test")}
    response = client.get("/api/customer/event-media/media-6/wireguard", cookies=cookies)
    assert response.status_code == 503
