"""Event-clip playback/download over WireGuard/direct appliance access
(2026-09-19): the real customer-facing integration, extending the
existing, already-working `/api/customer/events/{camera_id}/{event_id}/
media/url` route (main.py) with an opportunistic direct-fetch path and an
automatic, transparent fallback to the unchanged S3/CloudFront path.

Design under test:
- `.../media/url` does a cheap, no-network eligibility check (flag on,
  local_relative_path present and well-formed, an active WireGuard peer
  for the camera's appliance) and returns either the new portal-relative
  `.../media/direct` URL or the existing presigned S3 URL -- never both,
  never a tunnel address.
- `.../media/direct` re-checks authorization independently, then actually
  attempts the WireGuard fetch (mocked here -- no real tunnel/gateway
  involved) and falls back to a 302 redirect to the presigned S3 URL on
  ANY failure: ineligible, ".." in local_relative_path, no active tunnel,
  or GatewayUnavailable.
- Every resolution (direct success, fallback, or ineligible) writes one
  `event_media_fetch_log` row so bytes-served-by-source is queryable --
  this is the Hybrid-cost measurement the whole feature exists for.

Reuses test_live_view_wireguard.py's established client/seed/cookie
pattern (real app, real cookie-signing, real DB) rather than duplicating
a second convention.
"""

import pytest

import main
import partner_portal
from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_event_media_direct_wireguard.db"


def _seed(conn):
    now = "2026-09-19T00:00:00"
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','partner-1','Customer A','a@example.test','active',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-b','partner-1','Customer B','b@example.test','active',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-a','Main',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-b','cust-b','Main',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-a','cust-a','site-a','AIC-A',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-b','cust-b','site-b','AIC-B',?)", (now,))
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,device_key,status,name,created_at) "
        "VALUES('cam-a','cust-a','site-a','appl-a',1,'urn:uuid:fake-a','configured','Camera 1',?)", (now,)
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
    from datetime import datetime
    import wireguard_remote
    # Active WireGuard tunnel for appliance-a only -- appliance-b has no
    # peer row at all, exercising "no active tunnel" as an ineligible case.
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
        from fastapi.testclient import TestClient
        with TestClient(main.app, base_url=f"http://{allowed_host}") as test_client:
            yield test_client


def _insert_event_media(conn, *, media_id, camera_id, customer_id, site_id, appliance_id,
                         s3_key=None, local_relative_path=None, size_bytes=999):
    now = "2026-09-19T00:00:00"
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
        (media_id, event_id, customer_id, camera_id, s3_key or f"clips/{media_id}.mp4",
         None, now, now, 10.0, size_bytes, local_relative_path, now),
    )
    conn.commit()
    return event_id


@pytest.fixture(autouse=True)
def _enable_event_media_flag(monkeypatch):
    import live_view_wireguard
    monkeypatch.setattr(live_view_wireguard, "EVENT_MEDIA_WIREGUARD_ENABLED", True)
    monkeypatch.setattr(live_view_wireguard, "GATEWAY_INTERNAL_URL", "http://gateway.invalid:8098")
    # Real S3 presigning needs real AWS config this test environment has
    # none of -- stub the one function _presigned_recording_url() wraps,
    # matching test_events_thumbnail_fix.py's own established pattern for
    # this exact function, so the AWS *fallback* path has something real
    # to redirect to instead of legitimately 404ing on a signing failure.
    monkeypatch.setattr(main, "_presigned_recording_url_and_ttl", lambda s3_key: (f"https://cloudfront.example.test/{s3_key}", 300))
    yield


def _fetch_log_rows(media_id):
    from partner_db import connection
    with connection() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM event_media_fetch_log WHERE media_id=? ORDER BY created_at", (media_id,)
        ).fetchall()]


# ---------------------------------------------------------------------------
# Authentication / authorization -- both routes independently
# ---------------------------------------------------------------------------

def test_media_url_unauthenticated(client):
    response = client.get("/api/customer/events/cam-a/evt-x/media/url")
    assert response.status_code == 401


def test_media_direct_unauthenticated(client):
    response = client.get("/api/customer/events/cam-a/evt-x/media/direct")
    assert response.status_code == 401


def test_media_url_cross_tenant_denied(client):
    from partner_db import connection
    with connection() as db:
        event_id = _insert_event_media(db, media_id="media-1", camera_id="cam-a", customer_id="cust-a",
                                        site_id="site-a", appliance_id="appl-a",
                                        local_relative_path="/recordings/clips/motion/media-1.mp4")
    other_cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-b", "owner-b@example.test")}
    response = client.get(f"/api/customer/events/cam-a/{event_id}/media/url", cookies=other_cookies)
    assert response.status_code == 403


def test_media_direct_cross_tenant_denied(client):
    from partner_db import connection
    with connection() as db:
        event_id = _insert_event_media(db, media_id="media-2", camera_id="cam-a", customer_id="cust-a",
                                        site_id="site-a", appliance_id="appl-a",
                                        local_relative_path="/recordings/clips/motion/media-2.mp4")
    other_cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-b", "owner-b@example.test")}
    response = client.get(f"/api/customer/events/cam-a/{event_id}/media/direct", cookies=other_cookies)
    assert response.status_code == 403


def test_media_url_wrong_camera_denied(client):
    # A real event on cam-a, requested through cam-b's URL slot -- the
    # join on (event_id, camera_id) together must find nothing.
    from partner_db import connection
    with connection() as db:
        event_id = _insert_event_media(db, media_id="media-3", camera_id="cam-a", customer_id="cust-a",
                                        site_id="site-a", appliance_id="appl-a",
                                        local_relative_path="/recordings/clips/motion/media-3.mp4")
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    # cust-a doesn't own cam-b at all, so this is also a 403 (not authorized
    # for the camera in the URL) -- confirms the camera_id in the URL is
    # load-bearing, not decorative.
    response = client.get(f"/api/customer/events/cam-b/{event_id}/media/url", cookies=cookies)
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Eligibility decision (.../media/url)
# ---------------------------------------------------------------------------

def test_media_url_not_eligible_when_flag_off(client, monkeypatch):
    import live_view_wireguard
    monkeypatch.setattr(live_view_wireguard, "EVENT_MEDIA_WIREGUARD_ENABLED", False)
    from partner_db import connection
    with connection() as db:
        event_id = _insert_event_media(db, media_id="media-4", camera_id="cam-a", customer_id="cust-a",
                                        site_id="site-a", appliance_id="appl-a",
                                        local_relative_path="/recordings/clips/motion/media-4.mp4")
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    response = client.get(f"/api/customer/events/cam-a/{event_id}/media/url", cookies=cookies)
    assert response.status_code == 200
    assert "/media/direct" not in response.json()["url"]


def test_media_url_not_eligible_when_no_local_path(client):
    from partner_db import connection
    with connection() as db:
        event_id = _insert_event_media(db, media_id="media-5", camera_id="cam-a", customer_id="cust-a",
                                        site_id="site-a", appliance_id="appl-a",
                                        local_relative_path=None)
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    response = client.get(f"/api/customer/events/cam-a/{event_id}/media/url", cookies=cookies)
    assert response.status_code == 200
    assert "/media/direct" not in response.json()["url"]


def test_media_url_not_eligible_when_no_active_tunnel(client):
    # appliance-b has no wireguard peer row at all (see _seed()).
    from partner_db import connection
    with connection() as db:
        event_id = _insert_event_media(db, media_id="media-6", camera_id="cam-b", customer_id="cust-b",
                                        site_id="site-b", appliance_id="appl-b",
                                        local_relative_path="/recordings/clips/motion/media-6.mp4")
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-b", "owner-b@example.test")}
    response = client.get(f"/api/customer/events/cam-b/{event_id}/media/url", cookies=cookies)
    assert response.status_code == 200
    assert "/media/direct" not in response.json()["url"]


def test_media_url_eligible_returns_direct_path(client):
    from partner_db import connection
    with connection() as db:
        event_id = _insert_event_media(db, media_id="media-7", camera_id="cam-a", customer_id="cust-a",
                                        site_id="site-a", appliance_id="appl-a",
                                        local_relative_path="/recordings/clips/motion/media-7.mp4")
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    response = client.get(f"/api/customer/events/cam-a/{event_id}/media/url", cookies=cookies)
    assert response.status_code == 200
    assert response.json()["url"] == f"/api/customer/events/cam-a/{event_id}/media/direct"


# ---------------------------------------------------------------------------
# Direct fetch: success, failure+fallback, malformed path, measurement
# ---------------------------------------------------------------------------

def test_media_direct_success(client, monkeypatch):
    import live_view_wireguard
    monkeypatch.setattr(live_view_wireguard, "_fetch_via_gateway", lambda *a, **k: b"real-clip-bytes")
    from partner_db import connection
    with connection() as db:
        event_id = _insert_event_media(db, media_id="media-8", camera_id="cam-a", customer_id="cust-a",
                                        site_id="site-a", appliance_id="appl-a",
                                        local_relative_path="/recordings/clips/motion/media-8.mp4")
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    response = client.get(f"/api/customer/events/cam-a/{event_id}/media/direct", cookies=cookies)
    assert response.status_code == 200
    assert response.content == b"real-clip-bytes"
    assert response.headers["content-type"].startswith("video/mp4")

    rows = _fetch_log_rows("media-8")
    assert len(rows) == 1
    assert rows[0]["source"] == "wireguard"
    assert rows[0]["outcome"] == "direct_success"
    assert rows[0]["bytes_served"] == len(b"real-clip-bytes")


def test_media_direct_gateway_outage_falls_back_to_aws(client, monkeypatch):
    import live_view_wireguard

    def _raise(*a, **k):
        raise live_view_wireguard.GatewayUnavailable("simulated: gateway down")

    monkeypatch.setattr(live_view_wireguard, "_fetch_via_gateway", _raise)
    from partner_db import connection
    with connection() as db:
        event_id = _insert_event_media(db, media_id="media-9", camera_id="cam-a", customer_id="cust-a",
                                        site_id="site-a", appliance_id="appl-a",
                                        local_relative_path="/recordings/clips/motion/media-9.mp4",
                                        s3_key="clips/media-9.mp4", size_bytes=54321)
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    response = client.get(f"/api/customer/events/cam-a/{event_id}/media/direct", cookies=cookies, follow_redirects=False)
    assert response.status_code == 302
    assert "location" in response.headers
    # Never the appliance's tunnel address or the gateway's own internal
    # URL -- the browser is redirected to the ordinary presigned S3 flow.
    assert "10.70.0" not in response.headers["location"]
    assert "gateway.invalid" not in response.headers["location"]

    rows = _fetch_log_rows("media-9")
    assert len(rows) == 1
    assert rows[0]["source"] == "aws"
    assert rows[0]["outcome"] == "direct_failed_fallback"
    assert rows[0]["bytes_served"] == 54321


def test_media_direct_not_eligible_falls_back_to_aws_without_ever_calling_gateway(client, monkeypatch):
    import live_view_wireguard

    def _fail_if_called(*a, **k):
        raise AssertionError("must never attempt a gateway fetch for a NULL local_relative_path")

    monkeypatch.setattr(live_view_wireguard, "_fetch_via_gateway", _fail_if_called)
    from partner_db import connection
    with connection() as db:
        event_id = _insert_event_media(db, media_id="media-10", camera_id="cam-a", customer_id="cust-a",
                                        site_id="site-a", appliance_id="appl-a",
                                        local_relative_path=None, s3_key="clips/media-10.mp4", size_bytes=1000)
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    response = client.get(f"/api/customer/events/cam-a/{event_id}/media/direct", cookies=cookies, follow_redirects=False)
    assert response.status_code == 302

    rows = _fetch_log_rows("media-10")
    assert rows[0]["source"] == "aws"
    assert rows[0]["outcome"] == "not_eligible"


def test_media_direct_malformed_local_path_falls_back_without_calling_gateway(client, monkeypatch):
    import live_view_wireguard

    def _fail_if_called(*a, **k):
        raise AssertionError("must never attempt a gateway fetch for a path-traversal local_relative_path")

    monkeypatch.setattr(live_view_wireguard, "_fetch_via_gateway", _fail_if_called)
    from partner_db import connection
    with connection() as db:
        event_id = _insert_event_media(db, media_id="media-11", camera_id="cam-a", customer_id="cust-a",
                                        site_id="site-a", appliance_id="appl-a",
                                        local_relative_path="/recordings/../../etc/passwd",
                                        s3_key="clips/media-11.mp4", size_bytes=2000)
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    response = client.get(f"/api/customer/events/cam-a/{event_id}/media/direct", cookies=cookies, follow_redirects=False)
    assert response.status_code == 302

    rows = _fetch_log_rows("media-11")
    assert rows[0]["outcome"] == "not_eligible"


def test_media_direct_missing_clip_404s(client):
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    response = client.get("/api/customer/events/cam-a/does-not-exist/media/direct", cookies=cookies)
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Measurement: bytes served by source, across a realistic mix
# ---------------------------------------------------------------------------

def test_measurement_tracks_bytes_by_source_across_a_mix(client, monkeypatch):
    import live_view_wireguard
    from partner_db import connection

    with connection() as db:
        success_event = _insert_event_media(db, media_id="mix-success", camera_id="cam-a", customer_id="cust-a",
                                             site_id="site-a", appliance_id="appl-a",
                                             local_relative_path="/recordings/clips/motion/mix-success.mp4")
        fallback_event = _insert_event_media(db, media_id="mix-fallback", camera_id="cam-a", customer_id="cust-a",
                                              site_id="site-a", appliance_id="appl-a",
                                              local_relative_path="/recordings/clips/motion/mix-fallback.mp4",
                                              size_bytes=7000)
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}

    monkeypatch.setattr(live_view_wireguard, "_fetch_via_gateway", lambda *a, **k: b"x" * 3000)
    r1 = client.get(f"/api/customer/events/cam-a/{success_event}/media/direct", cookies=cookies)
    assert r1.status_code == 200

    def _raise(*a, **k):
        raise live_view_wireguard.GatewayUnavailable("down")

    monkeypatch.setattr(live_view_wireguard, "_fetch_via_gateway", _raise)
    r2 = client.get(f"/api/customer/events/cam-a/{fallback_event}/media/direct", cookies=cookies, follow_redirects=False)
    assert r2.status_code == 302

    with connection() as db:
        by_source = {
            row["source"]: row["total"]
            for row in db.execute(
                "SELECT source, SUM(bytes_served) AS total FROM event_media_fetch_log GROUP BY source"
            ).fetchall()
        }
    assert by_source["wireguard"] == 3000
    assert by_source["aws"] == 7000
