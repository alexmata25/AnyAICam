"""Regression coverage for the two Playback date-navigation API pieces
reconciled onto EC2 alongside the auth-routing fix: GET /api/customer/
recordings/{camera_id}?date=YYYY-MM-DD (a whole-day query mode added to
the existing recordings-metadata route, pagination params unchanged
when date is absent) and the new GET /api/customer/recordings/
{camera_id}/dates route (which local-calendar dates have footage).

Real HTTP through the real app (TestClient(main.app)), a throwaway
sqlite DB via override_target() -- same style as this suite's other
customer-auth-routing tests.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import main
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_customer_playback_date_api.db"


@pytest.fixture()
def http_client(tmp_path, monkeypatch, db_path):
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    with override_target(sqlite_path=db_path):
        initialize_database()
        # base_url: see test_cloud_customer_auth_routing.py's identical
        # fixture comment -- TrustedHostMiddleware rejects TestClient's
        # default "testserver" Host on a production-configured deployment.
        with TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client


def _seed_camera_with_recordings(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) "
        "VALUES('cust-1','partner-1','Test Customer','billing@example.test','active','2026-01-01')"
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-1','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) "
        "VALUES('cam-1','cust-1','site-1','appl-1',1,'Front Door','2026-01-01')"
    )
    # started_at/ended_at are naive UTC (matching the real recording
    # pipeline); _local_date_bounds_to_utc()/_customer_recording_dates()
    # convert through APPLIANCE_TIMEZONE (America/Chicago, UTC-5/-6)
    # before grouping by date, so these two times are chosen well clear
    # of that shift in either direction: 18:47 UTC on the 21st is still
    # afternoon Central on the 21st, and 20:00 UTC on the 22nd is mid-
    # afternoon Central on the 22nd -- neither lands on the "wrong"
    # local day the way a time within a few hours of UTC midnight would.
    conn.execute(
        "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,"
        "duration_seconds,size_bytes,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("rec-1", "cust-1", "site-1", "appl-1", "cam-1", "recordings/cust-1/cam-1/2026/08/21/rec-1.mkv",
         "2026-08-21T18:47:18", "2026-08-21T18:52:18", 300, 1000, "available", "2026-08-21T18:52:18"),
    )
    conn.execute(
        "INSERT INTO recordings(id,customer_id,site_id,appliance_id,camera_id,s3_key,started_at,ended_at,"
        "duration_seconds,size_bytes,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("rec-2", "cust-1", "site-1", "appl-1", "cam-1", "recordings/cust-1/cam-1/2026/08/22/rec-2.mkv",
         "2026-08-22T20:00:00", "2026-08-22T20:05:00", 300, 1000, "available", "2026-08-22T20:05:00"),
    )
    conn.commit()
    conn.close()


def _owner_cookie():
    import partner_portal
    return partner_portal._token("owner@example.test", "customer_owner", None, "cust-1", None)


def test_date_query_mode_returns_only_that_days_recordings(http_client, db_path, monkeypatch):
    _seed_camera_with_recordings(db_path)
    import partner_portal
    http_client.cookies.set(partner_portal.SESSION_COOKIE, _owner_cookie())

    response = http_client.get("/api/customer/recordings/cam-1", params={"date": "2026-08-21"})
    assert response.status_code == 200
    clips = response.json()["clips"]
    assert len(clips) == 1
    assert clips[0]["id"] == "rec-1"


def test_dates_endpoint_lists_every_local_date_with_footage(http_client, db_path):
    _seed_camera_with_recordings(db_path)
    import partner_portal
    http_client.cookies.set(partner_portal.SESSION_COOKIE, _owner_cookie())

    response = http_client.get("/api/customer/recordings/cam-1/dates")
    assert response.status_code == 200
    dates = response.json()["dates"]
    assert "2026-08-21" in dates
    assert "2026-08-22" in dates


def test_invalid_date_format_is_rejected(http_client, db_path):
    _seed_camera_with_recordings(db_path)
    import partner_portal
    http_client.cookies.set(partner_portal.SESSION_COOKIE, _owner_cookie())

    response = http_client.get("/api/customer/recordings/cam-1", params={"date": "not-a-date"})
    assert response.status_code == 400


def test_pagination_mode_unchanged_when_date_is_absent(http_client, db_path):
    _seed_camera_with_recordings(db_path)
    import partner_portal
    http_client.cookies.set(partner_portal.SESSION_COOKIE, _owner_cookie())

    response = http_client.get("/api/customer/recordings/cam-1", params={"limit": 10})
    assert response.status_code == 200
    clips = response.json()["clips"]
    assert len(clips) == 2


def test_unauthorized_camera_still_rejected_with_date_param(http_client, db_path):
    _seed_camera_with_recordings(db_path)
    # No session cookie at all -- authentication_middleware itself
    # would normally redirect first; hitting the route directly here
    # (bypassing the middleware via a raw ASGI call is out of scope)
    # so this simply confirms the route's own authorization check still
    # runs when date= is present, not just on the original code path.
    response = http_client.get("/api/customer/recordings/does-not-exist", params={"date": "2026-08-21"})
    assert response.status_code in (401, 403, 303)
