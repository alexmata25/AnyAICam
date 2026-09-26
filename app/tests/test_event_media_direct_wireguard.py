"""Event-clip playback/download over WireGuard/direct appliance access
(2026-09-19, redesigned same day after a real live-staging finding): the
real customer-facing integration, extending the existing, already-working
`/api/customer/events/{camera_id}/{event_id}/media/url` route (main.py)
with an opportunistic, AUTHENTICATED direct-fetch path and an automatic,
transparent fallback to the unchanged S3/CloudFront path.

Why "authenticated" matters here, not just "direct": the first live
staging attempt fetched the raw `/recordings/...` path over the tunnel,
which redirected to Ryzen's own local-admin-session login page -- and the
caller wrongly counted that HTTP 200 as a successful video fetch. This
redesign fixes both halves of that:
- The appliance is never asked for a raw path anymore. It's asked for
  `/api/appliance/media-fetch?path=...&expires=...&token=...` -- an
  appliance-authenticated route (falls under the existing
  `/api/appliance/` PUBLIC_PATH_PREFIXES entry, same as every other
  appliance route) that verifies a short-lived, path-scoped HMAC token
  locally (appliance_media_fetch.verify()) and serves ONLY that one file
  -- no directory browsing, no session, no unauthenticated LAN access to
  anything else under /recordings.
- A 200 status is never enough on its own:
  appliance_media_fetch.response_looks_like_real_video() must also agree
  (real MP4 signature, plausible size, never HTML/JSON) before anything
  is served to the browser or logged as `source=wireguard`.

Design under test:
- `.../media/url` does a cheap, no-network eligibility check (flag on,
  local_relative_path present and well-formed, an active WireGuard peer
  WITH a provisioned media_fetch_secret for the camera's appliance) and
  returns either the new portal-relative `.../media/direct` URL or the
  existing presigned S3 URL -- never both, never a tunnel address, never
  a token.
- `.../media/direct` re-checks authorization independently, mints a fresh
  token, attempts the authenticated fetch (mocked here -- no real
  tunnel/gateway/Ryzen involved), validates the response content, and
  falls back to a 302 redirect to the presigned S3 URL on ANY failure:
  ineligible, no secret provisioned, ".." in local_relative_path, no
  active tunnel, GatewayUnavailable, or a response that doesn't look like
  real video (HTML/JSON/wrong size).
- Every resolution writes one `event_media_fetch_log` row so
  bytes-served-by-source is queryable -- the Hybrid-cost measurement the
  whole feature exists for -- and `source=wireguard` is only ever
  recorded for a response that actually passed content validation.
- `/api/appliance/media-fetch` itself is tested directly too: valid
  token against a real local file, expired token, wrong token, path
  traversal, no secret provisioned, nonexistent file.

Reuses test_live_view_wireguard.py's established client/seed/cookie
pattern (real app, real cookie-signing, real DB) rather than duplicating
a second convention.
"""

import pytest

import appliance_media_fetch
import main
import partner_portal
from database_backend import override_target

APPLIANCE_A_SECRET = "test-media-fetch-secret-for-appliance-a"


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
    # Active WireGuard tunnel + a provisioned media_fetch_secret for
    # appliance-a only. Appliance-b has no peer row at all, exercising
    # "no active tunnel/no peer" as one ineligible case; a separate test
    # below covers "peer exists but no secret provisioned yet" -- the
    # realistic state of every appliance today.
    wireguard_remote.enroll_peer(
        conn, appliance_id='appl-a', customer_id='cust-a',
        public_key='A' * 43 + '=', now=datetime.fromisoformat(now),
    )
    conn.execute("UPDATE appliance_wireguard_peers SET media_fetch_secret=? WHERE appliance_id='appl-a'", (APPLIANCE_A_SECRET,))
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


def _real_mp4_bytes(size: int) -> bytes:
    # Real-shaped MP4 header (a genuine ftyp box) padded to size -- the
    # exact minimum response_looks_like_real_video() requires; mirrors
    # test_appliance_media_fetch.py's own identical helper.
    header = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    return header + b"\x00" * max(0, size - len(header))


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
    body = _real_mp4_bytes(5000)
    calls = []

    def _fake_fetch(tunnel_address, port, forward_path):
        calls.append(forward_path)
        return body

    monkeypatch.setattr(live_view_wireguard, "_fetch_via_gateway", _fake_fetch)
    from partner_db import connection
    with connection() as db:
        event_id = _insert_event_media(db, media_id="media-8", camera_id="cam-a", customer_id="cust-a",
                                        site_id="site-a", appliance_id="appl-a",
                                        local_relative_path="/recordings/clips/motion/media-8.mp4",
                                        size_bytes=5000)
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    response = client.get(f"/api/customer/events/cam-a/{event_id}/media/direct", cookies=cookies)
    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-type"].startswith("video/mp4")

    # The appliance is asked for the new authenticated endpoint, with a
    # token -- never a bare fetch of the raw /recordings/... path (the
    # exact bug this redesign fixes). The path itself legitimately
    # appears as this request's own query-string value (that's how the
    # appliance knows which file the token authorizes).
    assert len(calls) == 1
    assert calls[0].startswith("/api/appliance/media-fetch?")
    assert calls[0] != "/recordings/clips/motion/media-8.mp4"
    assert "token=" in calls[0] and "expires=" in calls[0]

    rows = _fetch_log_rows("media-8")
    assert len(rows) == 1
    assert rows[0]["source"] == "wireguard"
    assert rows[0]["outcome"] == "direct_success"
    assert rows[0]["bytes_served"] == len(body)


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


def test_media_direct_no_secret_provisioned_falls_back(client, monkeypatch):
    # The realistic state of every real appliance today: an active
    # WireGuard tunnel exists, but the media-fetch secret enrollment step
    # (separate, later, its own authorization) hasn't happened yet.
    import live_view_wireguard

    def _fail_if_called(*a, **k):
        raise AssertionError("must never attempt a gateway fetch with no media_fetch_secret provisioned")

    monkeypatch.setattr(live_view_wireguard, "_fetch_via_gateway", _fail_if_called)
    from partner_db import connection
    with connection() as db:
        db.execute("UPDATE appliance_wireguard_peers SET media_fetch_secret=NULL WHERE appliance_id='appl-a'")
        event_id = _insert_event_media(db, media_id="media-12", camera_id="cam-a", customer_id="cust-a",
                                        site_id="site-a", appliance_id="appl-a",
                                        local_relative_path="/recordings/clips/motion/media-12.mp4",
                                        s3_key="clips/media-12.mp4", size_bytes=3000)
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}

    url_response = client.get(f"/api/customer/events/cam-a/{event_id}/media/url", cookies=cookies)
    assert "/media/direct" not in url_response.json()["url"]

    response = client.get(f"/api/customer/events/cam-a/{event_id}/media/direct", cookies=cookies, follow_redirects=False)
    assert response.status_code == 302
    assert _fetch_log_rows("media-12")[0]["outcome"] == "not_eligible"


def test_media_direct_html_login_page_response_never_counted_as_success(client, monkeypatch):
    # The exact real bug this redesign fixes: a redirect-to-login page
    # returning HTTP 200 must never be recorded as source=wireguard.
    import live_view_wireguard
    html = (
        b"<!doctype html><html><head><title>Local emergency recovery sign-in</title></head>"
        b"<body>sign in</body></html>"
    ) + b" " * 2000

    monkeypatch.setattr(live_view_wireguard, "_fetch_via_gateway", lambda *a, **k: html)
    from partner_db import connection
    with connection() as db:
        event_id = _insert_event_media(db, media_id="media-13", camera_id="cam-a", customer_id="cust-a",
                                        site_id="site-a", appliance_id="appl-a",
                                        local_relative_path="/recordings/clips/motion/media-13.mp4",
                                        s3_key="clips/media-13.mp4", size_bytes=9999)
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    response = client.get(f"/api/customer/events/cam-a/{event_id}/media/direct", cookies=cookies, follow_redirects=False)

    # Falls back to S3 -- never serves the login page's bytes to the browser.
    assert response.status_code == 302
    assert response.content != html

    rows = _fetch_log_rows("media-13")
    assert len(rows) == 1
    assert rows[0]["source"] == "aws"
    assert rows[0]["outcome"] == "direct_invalid_response_fallback"
    assert rows[0]["bytes_served"] == 9999  # the clip's own known size, not the login page's


def test_media_direct_json_error_response_never_counted_as_success(client, monkeypatch):
    import live_view_wireguard
    json_error = b'{"detail": "Invalid or expired media-fetch token."}' + b" " * 2000

    monkeypatch.setattr(live_view_wireguard, "_fetch_via_gateway", lambda *a, **k: json_error)
    from partner_db import connection
    with connection() as db:
        event_id = _insert_event_media(db, media_id="media-14", camera_id="cam-a", customer_id="cust-a",
                                        site_id="site-a", appliance_id="appl-a",
                                        local_relative_path="/recordings/clips/motion/media-14.mp4",
                                        s3_key="clips/media-14.mp4", size_bytes=4444)
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    response = client.get(f"/api/customer/events/cam-a/{event_id}/media/direct", cookies=cookies, follow_redirects=False)
    assert response.status_code == 302

    rows = _fetch_log_rows("media-14")
    assert rows[0]["source"] == "aws"
    assert rows[0]["outcome"] == "direct_invalid_response_fallback"


# ---------------------------------------------------------------------------
# Measurement: bytes served by source, across a realistic mix
# ---------------------------------------------------------------------------

def test_measurement_tracks_bytes_by_source_across_a_mix(client, monkeypatch):
    import live_view_wireguard
    from partner_db import connection

    with connection() as db:
        success_event = _insert_event_media(db, media_id="mix-success", camera_id="cam-a", customer_id="cust-a",
                                             site_id="site-a", appliance_id="appl-a",
                                             local_relative_path="/recordings/clips/motion/mix-success.mp4",
                                             size_bytes=3000)
        fallback_event = _insert_event_media(db, media_id="mix-fallback", camera_id="cam-a", customer_id="cust-a",
                                              site_id="site-a", appliance_id="appl-a",
                                              local_relative_path="/recordings/clips/motion/mix-fallback.mp4",
                                              size_bytes=7000)
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}

    monkeypatch.setattr(live_view_wireguard, "_fetch_via_gateway", lambda *a, **k: _real_mp4_bytes(3000))
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


# ---------------------------------------------------------------------------
# The appliance-side route itself: /api/appliance/media-fetch
#
# Runs identically whether this process is the cloud portal or a real
# Ryzen appliance -- these tests exercise it directly, standing in for
# "what a real appliance does when the gateway calls it," independent of
# the cloud-side minting tested above. Not gated by the browser-session
# authentication_middleware at all (falls under the existing
# "/api/appliance/" PUBLIC_PATH_PREFIXES entry) -- its own token check is
# the only auth boundary.
# ---------------------------------------------------------------------------

APPLIANCE_SECRET = "test-appliance-local-secret"


@pytest.fixture()
def appliance_recordings_folder(tmp_path, monkeypatch):
    folder = tmp_path / "recordings"
    (folder / "clips" / "motion").mkdir(parents=True)
    real_bytes = _real_mp4_bytes(4096)
    (folder / "clips" / "motion" / "real-clip.mp4").write_bytes(real_bytes)
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", folder)
    monkeypatch.setattr(appliance_media_fetch, "load_local_secret", lambda: APPLIANCE_SECRET)
    return folder, real_bytes


def test_appliance_media_fetch_valid_token_serves_the_exact_file(client, appliance_recordings_folder):
    _folder, real_bytes = appliance_recordings_folder
    expires, token = appliance_media_fetch.mint(APPLIANCE_SECRET, "/recordings/clips/motion/real-clip.mp4")
    response = client.get(
        "/api/appliance/media-fetch",
        params={"path": "/recordings/clips/motion/real-clip.mp4", "expires": expires, "token": token},
    )
    assert response.status_code == 200
    assert response.content == real_bytes


def test_appliance_media_fetch_expired_token_rejected(client, appliance_recordings_folder):
    expires, token = appliance_media_fetch.mint(APPLIANCE_SECRET, "/recordings/clips/motion/real-clip.mp4", now=1000.0)
    response = client.get(
        "/api/appliance/media-fetch",
        params={"path": "/recordings/clips/motion/real-clip.mp4", "expires": expires, "token": token},
    )
    assert response.status_code == 401


def test_appliance_media_fetch_wrong_token_rejected(client, appliance_recordings_folder):
    expires, _ = appliance_media_fetch.mint(APPLIANCE_SECRET, "/recordings/clips/motion/real-clip.mp4")
    response = client.get(
        "/api/appliance/media-fetch",
        params={"path": "/recordings/clips/motion/real-clip.mp4", "expires": expires, "token": "0" * 64},
    )
    assert response.status_code == 401


def test_appliance_media_fetch_token_for_different_path_rejected(client, appliance_recordings_folder):
    # A real, validly-signed, unexpired token -- for a DIFFERENT path.
    expires, token = appliance_media_fetch.mint(APPLIANCE_SECRET, "/recordings/clips/motion/other-clip.mp4")
    response = client.get(
        "/api/appliance/media-fetch",
        params={"path": "/recordings/clips/motion/real-clip.mp4", "expires": expires, "token": token},
    )
    assert response.status_code == 401


def test_appliance_media_fetch_path_traversal_rejected(client, appliance_recordings_folder):
    traversal_path = "/recordings/../../../etc/passwd"
    expires, token = appliance_media_fetch.mint(APPLIANCE_SECRET, traversal_path)
    response = client.get(
        "/api/appliance/media-fetch",
        params={"path": traversal_path, "expires": expires, "token": token},
    )
    assert response.status_code == 400


def test_appliance_media_fetch_no_secret_provisioned_rejected(client, tmp_path, monkeypatch):
    folder = tmp_path / "recordings"
    (folder / "clips" / "motion").mkdir(parents=True)
    (folder / "clips" / "motion" / "real-clip.mp4").write_bytes(_real_mp4_bytes(4096))
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", folder)
    monkeypatch.setattr(appliance_media_fetch, "load_local_secret", lambda: None)  # never provisioned yet
    expires, token = appliance_media_fetch.mint("some-secret-nobody-has", "/recordings/clips/motion/real-clip.mp4")
    response = client.get(
        "/api/appliance/media-fetch",
        params={"path": "/recordings/clips/motion/real-clip.mp4", "expires": expires, "token": token},
    )
    assert response.status_code == 503


def test_appliance_media_fetch_nonexistent_file_404s(client, appliance_recordings_folder):
    expires, token = appliance_media_fetch.mint(APPLIANCE_SECRET, "/recordings/clips/motion/never-existed.mp4")
    response = client.get(
        "/api/appliance/media-fetch",
        params={"path": "/recordings/clips/motion/never-existed.mp4", "expires": expires, "token": token},
    )
    assert response.status_code == 404


def test_appliance_media_fetch_requires_no_customer_session(client, appliance_recordings_folder):
    # Explicitly no cookies at all -- this route's whole point is being
    # reachable by the gateway, which has no customer session and never
    # will. A 401/403 here would mean it accidentally fell under the
    # browser-session authentication_middleware instead of its own token
    # check.
    _folder, real_bytes = appliance_recordings_folder
    expires, token = appliance_media_fetch.mint(APPLIANCE_SECRET, "/recordings/clips/motion/real-clip.mp4")
    response = client.get(
        "/api/appliance/media-fetch",
        params={"path": "/recordings/clips/motion/real-clip.mp4", "expires": expires, "token": token},
        cookies={},
    )
    assert response.status_code == 200
    assert response.content == real_bytes
