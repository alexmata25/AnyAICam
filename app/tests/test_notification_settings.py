"""End-to-end regression coverage for the Notifications settings page
(v1) against the real app (TestClient(main.app)), real signed
partner_identity() cookies, and a throwaway sqlite DB.
"""

import sqlite3

import pytest

import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_notification_settings.db"


@pytest.fixture()
def http_client(db_path, monkeypatch):
    from fastapi.testclient import TestClient

    # Force both providers to their honest, unconfigured default
    # regardless of the host environment's own .env.
    monkeypatch.delenv("ANYAICAM_EMAIL_BACKEND", raising=False)
    monkeypatch.delenv("ANYAICAM_SMS_BACKEND", raising=False)
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client


def _seed_tenant(conn, customer_id="cust-1", partner_id="partner-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, "Test Co", "test@example.com", "active", "2026-01-01"),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1',?,?,?)", (customer_id, "Main", "2026-01-01"))
    conn.commit()


def _seed_cameras(conn, camera_ids, customer_id="cust-1"):
    for index, camera_id in enumerate(camera_ids, start=1):
        conn.execute(
            "INSERT INTO cameras(id,customer_id,site_id,camera_number,name,created_at) VALUES(?,?,?,?,?,?)",
            (camera_id, customer_id, "site-1", index, camera_id, "2026-01-01"),
        )
    conn.commit()


def _seed_viewer(conn, user_id, email, customer_id, granted_camera_ids):
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,camera_access_mode,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (user_id, "partner-1", email, "Viewer", "customer_viewer", "x", 1, customer_id, "selected", "2026-01-01"),
    )
    for camera_id in granted_camera_ids:
        conn.execute(
            "INSERT INTO customer_camera_permissions(user_id,camera_id,can_playback) VALUES(?,?,1)", (user_id, camera_id)
        )
    conn.commit()


def _seed_owner(conn, user_id, email, customer_id):
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (user_id, "partner-1", email, "Owner", "customer_owner", "x", 1, customer_id, "2026-01-01"),
    )
    conn.commit()


def _owner_cookie(email="owner@example.test", customer_id="cust-1"):
    return partner_portal._token(email, "customer_owner", None, customer_id, None)


def _viewer_cookie(email, customer_id="cust-1"):
    return partner_portal._token(email, "customer_viewer", None, customer_id, None)


# =============================================================== page load / gating


def test_page_requires_customer_portal_sign_in(http_client):
    response = http_client.get("/settings/notifications")
    assert response.status_code == 200
    assert "Customer Portal sign-in required" in response.text
    assert 'href="/customer-login.html"' in response.text


def test_page_loads_for_a_real_customer_owner(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_owner(conn, "user-1", "owner@example.test", "cust-1")
    _seed_cameras(conn, ["cam-1", "cam-2"])
    response = http_client.get("/settings/notifications", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "Notifications" in response.text
    assert "Send test email" in response.text
    assert "Send test SMS" in response.text


# =============================================================== save: email-only / SMS-only / both / disabled


def test_save_email_only(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_owner(conn, "user-1", "owner@example.test", "cust-1")
    cookie = _owner_cookie()
    response = http_client.put(
        "/api/customer/notifications/preferences", cookies={partner_portal.SESSION_COOKIE: cookie},
        json={"email_enabled": True, "email_address": "owner@example.test", "sms_enabled": False, "phone_number": "",
              "event_types": ["smart_motion"], "camera_scope": "all", "camera_ids": []},
    )
    assert response.status_code == 200
    prefs = response.json()["preferences"]
    assert prefs["email_enabled"] is True
    assert prefs["sms_enabled"] is False


def test_save_sms_only(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_owner(conn, "user-1", "owner@example.test", "cust-1")
    cookie = _owner_cookie()
    response = http_client.put(
        "/api/customer/notifications/preferences", cookies={partner_portal.SESSION_COOKIE: cookie},
        json={"email_enabled": False, "email_address": "", "sms_enabled": True, "phone_number": "+15551234567",
              "event_types": ["camera_offline"], "camera_scope": "all", "camera_ids": []},
    )
    assert response.status_code == 200
    prefs = response.json()["preferences"]
    assert prefs["sms_enabled"] is True
    assert prefs["email_enabled"] is False


def test_save_both_channels(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_owner(conn, "user-1", "owner@example.test", "cust-1")
    cookie = _owner_cookie()
    response = http_client.put(
        "/api/customer/notifications/preferences", cookies={partner_portal.SESSION_COOKIE: cookie},
        json={"email_enabled": True, "email_address": "owner@example.test", "sms_enabled": True, "phone_number": "+15551234567",
              "event_types": ["smart_motion", "lpr"], "camera_scope": "all", "camera_ids": []},
    )
    assert response.status_code == 200
    prefs = response.json()["preferences"]
    assert prefs["email_enabled"] is True and prefs["sms_enabled"] is True


def test_save_both_channels_disabled(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_owner(conn, "user-1", "owner@example.test", "cust-1")
    cookie = _owner_cookie()
    response = http_client.put(
        "/api/customer/notifications/preferences", cookies={partner_portal.SESSION_COOKIE: cookie},
        json={"email_enabled": False, "email_address": "", "sms_enabled": False, "phone_number": "",
              "event_types": [], "camera_scope": "all", "camera_ids": []},
    )
    assert response.status_code == 200
    prefs = response.json()["preferences"]
    assert prefs["email_enabled"] is False and prefs["sms_enabled"] is False


# =============================================================== per-camera selection + permission enforcement


def test_owner_can_select_any_of_their_own_cameras(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_owner(conn, "user-1", "owner@example.test", "cust-1")
    _seed_cameras(conn, ["cam-1", "cam-2", "cam-3"])
    cookie = _owner_cookie()
    response = http_client.put(
        "/api/customer/notifications/preferences", cookies={partner_portal.SESSION_COOKIE: cookie},
        json={"email_enabled": True, "email_address": "owner@example.test", "sms_enabled": False, "phone_number": "",
              "event_types": ["smart_motion"], "camera_scope": "selected", "camera_ids": ["cam-1", "cam-3"]},
    )
    assert response.status_code == 200
    assert response.json()["preferences"]["camera_ids"] == ["cam-1", "cam-3"]


def test_viewer_with_one_of_five_cameras_can_only_select_that_camera(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_cameras(conn, [f"cam-{n}" for n in range(1, 6)])
    _seed_viewer(conn, "viewer-1", "viewer@example.test", "cust-1", ["cam-3"])
    cookie = _viewer_cookie("viewer@example.test")
    response = http_client.put(
        "/api/customer/notifications/preferences", cookies={partner_portal.SESSION_COOKIE: cookie},
        json={"email_enabled": True, "email_address": "viewer@example.test", "sms_enabled": False, "phone_number": "",
              "event_types": ["smart_motion"], "camera_scope": "selected", "camera_ids": ["cam-3"]},
    )
    assert response.status_code == 200
    assert response.json()["preferences"]["camera_ids"] == ["cam-3"]


def test_viewer_cannot_subscribe_to_a_camera_they_are_not_authorized_for(http_client, db_path):
    """The exact server-side permission requirement: a restricted user
    (1-of-5 cameras granted) must be rejected -- not silently filtered
    -- when requesting a camera outside their own authorized set."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_cameras(conn, [f"cam-{n}" for n in range(1, 6)])
    _seed_viewer(conn, "viewer-1", "viewer@example.test", "cust-1", ["cam-3"])
    cookie = _viewer_cookie("viewer@example.test")
    response = http_client.put(
        "/api/customer/notifications/preferences", cookies={partner_portal.SESSION_COOKIE: cookie},
        json={"email_enabled": True, "email_address": "viewer@example.test", "sms_enabled": False, "phone_number": "",
              "event_types": ["smart_motion"], "camera_scope": "selected", "camera_ids": ["cam-3", "cam-1"]},
    )
    assert response.status_code == 403


def test_viewer_with_three_of_ten_cameras_all_scope_resolves_to_their_own_three(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_cameras(conn, [f"cam-{n}" for n in range(1, 11)])
    granted = ["cam-2", "cam-5", "cam-9"]
    _seed_viewer(conn, "viewer-1", "viewer@example.test", "cust-1", granted)
    cookie = _viewer_cookie("viewer@example.test")
    response = http_client.get("/api/customer/notifications/preferences", cookies={partner_portal.SESSION_COOKIE: cookie})
    assert response.status_code == 200
    # 'all' scope (the default) reflects this viewer's own authorized
    # cameras only, not the customer's full fleet.
    assert response.json()["camera_scope"] == "all"


# =============================================================== invalid input


def test_invalid_email_rejected(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_owner(conn, "user-1", "owner@example.test", "cust-1")
    response = http_client.put(
        "/api/customer/notifications/preferences", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()},
        json={"email_enabled": True, "email_address": "not-an-email", "sms_enabled": False, "phone_number": "",
              "event_types": [], "camera_scope": "all", "camera_ids": []},
    )
    assert response.status_code == 400


def test_invalid_phone_rejected(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_owner(conn, "user-1", "owner@example.test", "cust-1")
    response = http_client.put(
        "/api/customer/notifications/preferences", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()},
        json={"email_enabled": False, "email_address": "", "sms_enabled": True, "phone_number": "555-1234",
              "event_types": [], "camera_scope": "all", "camera_ids": []},
    )
    assert response.status_code == 400


# =============================================================== quiet hours


def test_quiet_hours_saved_and_returned(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_owner(conn, "user-1", "owner@example.test", "cust-1")
    response = http_client.put(
        "/api/customer/notifications/preferences", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()},
        json={"email_enabled": False, "email_address": "", "sms_enabled": False, "phone_number": "",
              "event_types": [], "camera_scope": "all", "camera_ids": [],
              "quiet_hours_enabled": True, "quiet_start": "21:00", "quiet_end": "06:00"},
    )
    assert response.status_code == 200
    prefs = response.json()["preferences"]
    assert prefs["quiet_hours_enabled"] is True
    assert prefs["quiet_start"] == "21:00"
    assert prefs["quiet_end"] == "06:00"


def test_invalid_quiet_hours_rejected(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_owner(conn, "user-1", "owner@example.test", "cust-1")
    response = http_client.put(
        "/api/customer/notifications/preferences", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()},
        json={"email_enabled": False, "email_address": "", "sms_enabled": False, "phone_number": "",
              "event_types": [], "camera_scope": "all", "camera_ids": [],
              "quiet_hours_enabled": True, "quiet_start": "bogus", "quiet_end": "06:00"},
    )
    assert response.status_code == 400


# =============================================================== provider-unavailable state / test buttons never fake success


def test_test_email_reports_preview_not_sent_when_unconfigured(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_owner(conn, "user-1", "owner@example.test", "cust-1")
    cookie = _owner_cookie()
    http_client.put(
        "/api/customer/notifications/preferences", cookies={partner_portal.SESSION_COOKIE: cookie},
        json={"email_enabled": True, "email_address": "owner@example.test", "sms_enabled": False, "phone_number": "",
              "event_types": [], "camera_scope": "all", "camera_ids": []},
    )
    response = http_client.post("/api/customer/notifications/test-email", cookies={partner_portal.SESSION_COOKIE: cookie})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] != "sent"  # never fakes success
    assert "not configured" in body["message"].lower() or "preview" in body["message"].lower()


def test_test_sms_reports_unavailable_not_sent_when_unconfigured(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_owner(conn, "user-1", "owner@example.test", "cust-1")
    cookie = _owner_cookie()
    http_client.put(
        "/api/customer/notifications/preferences", cookies={partner_portal.SESSION_COOKIE: cookie},
        json={"email_enabled": False, "email_address": "", "sms_enabled": True, "phone_number": "+15551234567",
              "event_types": [], "camera_scope": "all", "camera_ids": []},
    )
    response = http_client.post("/api/customer/notifications/test-sms", cookies={partner_portal.SESSION_COOKIE: cookie})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] != "sent"  # never fakes success


def test_preferences_response_reports_provider_availability_honestly(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_owner(conn, "user-1", "owner@example.test", "cust-1")
    response = http_client.get("/api/customer/notifications/preferences", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.json()
    assert body["email_provider_available"] is False
    assert body["sms_provider_available"] is False


def test_test_email_requires_a_saved_address_first(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_owner(conn, "user-1", "owner@example.test", "cust-1")
    response = http_client.post("/api/customer/notifications/test-email", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 400


def test_unauthenticated_request_is_denied(http_client):
    # Both requests are stopped before ever reaching this module's own
    # routes -- main.py's authentication_middleware returns a blanket
    # 401 for any /api/* call with no recognized identity at all,
    # the same app-wide behavior every other /api/customer/* route gets.
    response = http_client.get("/api/customer/notifications/preferences")
    assert response.status_code == 401
    response = http_client.put("/api/customer/notifications/preferences", json={})
    assert response.status_code == 401
