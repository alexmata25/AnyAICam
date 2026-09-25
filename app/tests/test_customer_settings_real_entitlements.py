"""Settings (/customer-app-settings) and Account (/customer-portal) show the
REAL per-camera analytics (camera_analytics_entitlements) -- 2026-09-25.
Both read customer_camera_features.json, a legacy store nothing that runs
analytics reads, so a camera with Smart Motion on showed "Upgrade
required", and the page's toggles, per-camera alert form and push button
changed nothing. Alerts now point to the real Notification settings and
Mobile devices pages."""
import re
import shutil
import sqlite3
import subprocess

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def client(tmp_path):
    db_path = tmp_path / "settings.db"
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO partners(id,name,created_at) VALUES('p1','P','x')")
        conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust','p1','C','c@e.test','active','x')")
        conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('s1','cust','Home','x')")
        conn.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES('cam-uuid-7','cust','s1','Porch','configured',7,'x')")
        conn.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES('cam-uuid-8','cust','s1','Yard','configured',8,'x')")
        conn.execute("INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) VALUES('cam-uuid-7','smart_motion','active','x','x')")
        for uid, email, role in (("u-own", "owner@c.test", "customer_owner"), ("u-view", "viewer@c.test", "customer_viewer")):
            conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (uid, "p1", email, email, role, "x", 1, "cust", "x", "active", "all"))
        conn.commit()
        conn.close()
        with TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client


def _cookie(email="owner@c.test", role="customer_owner"):
    return {partner_portal.SESSION_COOKIE: partner_portal._token(email, role, None, "cust", None)}


def test_app_settings_api_reports_real_per_camera_analytics(client):
    body = client.get("/api/customer/cameras/7/app-settings", cookies=_cookie()).json()
    assert body["camera_uuid"] == "cam-uuid-7"
    state = {item["key"]: item["enabled"] for item in body["analytics"]}
    assert state["smart_motion"] is True and state["lpr"] is False and "facial_recognition" in state
    other = client.get("/api/customer/cameras/8/app-settings", cookies=_cookie()).json()
    assert not any(item["enabled"] for item in other["analytics"])


def test_settings_page_has_no_dead_controls(client):
    html = client.get("/customer-app-settings", cookies=_cookie()).text
    for gone in ('id="save-features"', 'id="alert-form"', 'id="enable-push"', "endpoint:'browser-permission'",
                 "Locked features require an active entitlement assigned by ANY AI CAM"):
        assert gone not in html, gone
    assert 'href="/settings/notifications"' in html and 'href="/mobile-devices"' in html
    assert "const isOwner=true;" in html
    assert "const isOwner=false;" in client.get("/customer-app-settings", cookies=_cookie("viewer@c.test", "customer_viewer")).text


def test_account_page_summaries_use_real_analytics_and_working_camera_links(client):
    html = client.get("/customer-portal", cookies=_cookie()).text
    assert "Analytics: Smart Motion" in html and "No analytics enabled on this camera" in html
    assert 'href="/customer/cameras/cam-uuid-7/live"' in html and 'href="/camera/7"' not in html
    assert "ask your installer to add features" not in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_settings_script_is_valid_javascript(client, tmp_path):
    html = client.get("/customer-app-settings", cookies=_cookie()).text
    script = next(s for s in re.findall(r"<script>(.*?)</script>", html, re.S) if "camera-analytics-list" in s)
    path = tmp_path / "settings.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_all_settings_link_takes_customers_to_their_settings(client):
    """'All settings' (Notification settings) linked to /settings, which
    showed customers 'role does not include manage_settings'."""
    response = client.get("/settings", cookies=_cookie())
    assert response.status_code == 303 and response.headers["location"] == "/customer-app-settings"
