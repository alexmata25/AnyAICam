"""Customer-portal defects found by the 2026-10-10 local functional acceptance
(local-acceptance-stack/artifacts/functional-results.json) and production.

2  Setup wizard: step 6 markup opened 4 <div>s and closed 3, so step 7
   ("Confirm and open dashboard") and the Back / "Save and continue" bar were
   nested inside step 6 and hidden on every other step; the outer label read
   "Step 7 of 7" while tab 6 was highlighted; the Review's "Local recording
   configuration" read "— · — · retention up to — days" for a customer with
   no legacy plans row; and /customer-account sent a customer whose appliance
   came in through a claim code back to /customer/setup forever (a claim
   leaves activation_status 'pending').
4  Changing the password did not sign out the user's other sessions, and the
   page called a customer's password change "Activate partner account".
6  POST door-config made any camera a working door without Face Access.
7  Playback Download never worked for event clips.
8  /events and /alerts formatted times in a fixed America/Chicago.
9  The floating AACO button covered the two-row phone tab bar's "Playback".
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target
from partner_db import connection, initialize_database, password_hash, row

NODE = shutil.which("node")
_SCRIPT_BLOCK = re.compile(r"<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)


def _node_check(script: str) -> str | None:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        path = handle.name
    try:
        done = subprocess.run([NODE, "--check", path], capture_output=True, text=True, timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    return None if done.returncode == 0 else (done.stderr or done.stdout)[:800]


def _assert_inline_scripts_parse(html: str) -> None:
    if not NODE:
        pytest.skip("node is not installed")
    blocks = _SCRIPT_BLOCK.findall(html)
    assert blocks
    for block in blocks:
        error = _node_check(block)
        assert error is None, error


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "portal_ui_fixes.db"


@pytest.fixture()
def http_client(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, follow_redirects=False) as client:
            yield client


NOW = "2026-10-10T12:00:00"


def _seed_customer(db_path, customer_id="cust-1"):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Partner',?)", (NOW,))
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
                 (customer_id, "partner-1", "Real Customer", "owner@example.test", "active", NOW))
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)",
                 (f"site-{customer_id}", customer_id, "Main", NOW))
    conn.commit()
    return conn


def _owner_cookie(customer_id="cust-1", session_id=None, email="owner@example.test"):
    return {partner_portal.SESSION_COOKIE: partner_portal._token(email, "customer_owner", None, customer_id, session_id)}


# ------------------------------------------------------------ 2a setup wizard markup


class _StepNesting(HTMLParser):
    """Records, for every setup step and the action bar, which setup steps
    enclose it."""

    def __init__(self):
        super().__init__()
        self.stack: list[str | None] = []
        self.found: dict[str, list[str]] = {}

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        enclosing = [item for item in self.stack if item]
        marker = None
        if "customer-setup-step" in classes:
            marker = "step-" + attributes.get("data-step", "?")
        elif "dialog-actions" in classes:
            marker = "actions"
        if marker:
            self.found[marker] = enclosing
        self.stack.append(marker)

    def handle_endtag(self, tag):
        if tag == "div" and self.stack:
            self.stack.pop()


def _setup_html(http_client, customer_id="cust-1"):
    response = http_client.get("/customer/setup", cookies=_owner_cookie(customer_id))
    assert response.status_code == 200
    return response.text


def test_every_setup_step_and_the_action_bar_are_top_level(http_client, db_path):
    _seed_customer(db_path)
    parser = _StepNesting()
    parser.feed(_setup_html(http_client))
    assert set(parser.found) == {f"step-{n}" for n in range(1, 8)} | {"actions"}
    for marker, enclosing in parser.found.items():
        assert enclosing == [], f"{marker} is nested inside {enclosing}"


def test_outer_step_label_matches_the_highlighted_tab(http_client, db_path):
    conn = _seed_customer(db_path)
    html = _setup_html(http_client)
    assert "Customer portion" not in html
    assert 'Step <strong>1</strong> of 7' in html
    assert re.search(r'class="workspace-tab active" data-step="1" aria-current="step"', html)
    # The script keeps the label and the highlighted tab on the same number.
    assert "Step <strong>${setupStep}</strong> of 7" in html
    assert "setupStep<=3?6:7" not in html
    # A saved draft reopens on its own step, label and tab together.
    conn.execute("INSERT INTO customer_setup_drafts(customer_id,current_step,data_json,updated_at) VALUES('cust-1',2,'{}',?)", (NOW,))
    conn.commit()
    html = _setup_html(http_client)
    assert 'Step <strong>2</strong> of 7' in html
    assert re.search(r'class="workspace-tab active" data-step="2" aria-current="step"', html)


def test_setup_page_scripts_parse(http_client, db_path):
    _seed_customer(db_path)
    _assert_inline_scripts_parse(_setup_html(http_client))


# ------------------------------------------------------- 2c local recording summary


def _recording_summary(html):
    match = re.search(r'<p id="local-recording-summary">(.*?)</p>', html)
    assert match, "Local recording configuration summary missing"
    return match.group(1)


def test_review_recording_summary_without_a_legacy_plan_has_no_dashes(http_client, db_path):
    conn = _seed_customer(db_path)
    conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-cust-1','AIC-1',?)", (NOW,))
    conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,resolution,status,local_recording_mode,created_at) "
                 "VALUES('cam-1','cust-1','site-cust-1','appl-1','Front','4mp','configured','event',?)", (NOW,))
    conn.commit()
    summary = _recording_summary(_setup_html(http_client))
    assert "—" not in summary
    assert summary == "4MP &middot; event recording &middot; retention follows your camera plan"


def test_review_recording_summary_uses_the_v2_plan_retention(http_client, db_path, monkeypatch):
    import per_camera_billing
    _seed_customer(db_path)
    monkeypatch.setattr(per_camera_billing, "entitlement_for_customer", lambda customer_id: {"id": "v2"})
    monkeypatch.setattr(per_camera_billing, "entitlement_payload", lambda entitlement: {
        "status": "active", "display_name": "Hybrid", "camera_quantity": 3, "local_retention_days": 7})
    summary = _recording_summary(_setup_html(http_client))
    assert "retention up to 7 days" in summary
    assert "—" not in summary


def test_review_recording_summary_keeps_a_legacy_plan(http_client, db_path):
    conn = _seed_customer(db_path)
    conn.execute("INSERT INTO plans(id,customer_id,resolution,recording_mode,retention_days,camera_quantity,status,created_at) "
                 "VALUES('plan-1','cust-1','4mp','motion',30,1,'quote',?)", (NOW,))
    conn.commit()
    assert _recording_summary(_setup_html(http_client)) == "4MP &middot; motion &middot; retention up to 30 days"


# ------------------------------------------- 2b claimed appliance counts as activated


def _seed_account(db_path, *, activation_status=None, credential_used=True, check_in=True, revoked=False):
    conn = _seed_customer(db_path)
    columns = "id,customer_id,site_id,cloud_id,last_check_in,online_status,created_at"
    values = ["appl-1", "cust-1", "site-cust-1", "AIC-CLAIMED", "2026-10-10T12:05:00" if check_in else None, "online", NOW]
    if activation_status:
        columns += ",activation_status"
        values.append(activation_status)
    conn.execute(f"INSERT INTO appliances({columns}) VALUES({','.join('?' for _ in values)})", values)
    conn.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at,last_used_at,revoked_at,created_by) "
                 "VALUES('cred-1','appl-1','x',?,?,?,'claim')",
                 (NOW, "2026-10-10T12:00:30" if credential_used else None, "2026-10-10T13:00:00" if revoked else None))
    conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,status,device_key,created_at) "
                 "VALUES('cam-1','cust-1','site-cust-1','appl-1',1,'Front door','configured','urn:uuid:front',?)", (NOW,))
    conn.execute("INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,stripe_checkout_session_id,created_at,updated_at) "
                 "VALUES('ent-1','cust-1','camera_slots_hybrid',3,'active','cs_test_1',?,?)", (NOW, NOW))
    conn.execute("INSERT INTO customer_setup_drafts(customer_id,current_step,data_json,updated_at,completed_at) VALUES('cust-1',7,'{}',?,?)", (NOW, NOW))
    conn.commit()


def test_customer_account_opens_for_a_claimed_enrolled_appliance(http_client, db_path):
    _seed_account(db_path)  # activation_status stays at its 'pending' default, as claim/complete leaves it
    with override_target(sqlite_path=db_path):
        assert row("SELECT activation_status FROM appliances WHERE id='appl-1'")["activation_status"] == "pending"
    response = http_client.get("/customer-account", cookies=_owner_cookie())
    assert response.status_code == 200, response.headers.get("location")


@pytest.mark.parametrize("kwargs", [
    {"credential_used": False},            # credential issued, never used: may have rolled back
    {"check_in": False},                   # never sent a heartbeat
    {"revoked": True},                     # credential revoked
    {"activation_status": "released"},     # appliance released from the account
])
def test_customer_account_still_sends_an_unenrolled_appliance_to_setup(http_client, db_path, kwargs):
    _seed_account(db_path, **kwargs)
    response = http_client.get("/customer-account", cookies=_owner_cookie())
    assert response.status_code == 303
    assert response.headers["location"] == "/customer/setup"


def test_token_activated_appliance_is_unchanged(http_client, db_path):
    _seed_account(db_path, activation_status="activated", credential_used=False, check_in=False)
    assert http_client.get("/customer-account", cookies=_owner_cookie()).status_code == 200


def test_setup_needed_banner_check_accepts_the_claimed_appliance(db_path):
    import appliance_readiness
    import per_camera_billing
    with override_target(sqlite_path=db_path):
        initialize_database()
    _seed_account(db_path)
    with override_target(sqlite_path=db_path):
        assert appliance_readiness.customer_has_activated_appliance("cust-1") is True
        assert per_camera_billing._setup_needed("cust-1") is False
        assert appliance_readiness.customer_has_activated_appliance("someone-else") is False
        # Nothing was written: the check is read-only (no backfill of the DB).
        assert row("SELECT activation_status FROM appliances WHERE id='appl-1'")["activation_status"] == "pending"


# ------------------------------------------------------------ 4 change password


OLD_PASSWORD = "original-customer-pass-1"


def _seed_customer_user(db_path):
    conn = _seed_customer(db_path)
    conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
                 "VALUES('user-1','partner-1','owner@example.test','Owner','customer_owner',?,1,'cust-1',?)",
                 (password_hash(OLD_PASSWORD), NOW))
    expires = (datetime.now() + timedelta(days=1)).isoformat()
    for session_id in ("sess-current", "sess-other"):
        conn.execute("INSERT INTO user_sessions(id,user_id,email,role,session_type,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                     (session_id, "user-1", "owner@example.test", "customer_owner", "browser", NOW, expires))
    conn.commit()


def test_changing_the_password_signs_out_other_sessions_but_not_this_one(http_client, db_path):
    _seed_customer_user(db_path)
    current, other = _owner_cookie(session_id="sess-current"), _owner_cookie(session_id="sess-other")
    assert http_client.get("/api/v2/customer/subscription", cookies=other).status_code == 200
    response = http_client.post("/api/partner/activate-account", cookies=current,
                                json={"password": "brand-new-password-22", "current_password": OLD_PASSWORD})
    assert response.status_code == 200, response.text
    assert "partner account" not in response.json()["message"]
    assert "other signed-in sessions were signed out" in response.json()["message"]
    assert http_client.get("/api/v2/customer/subscription", cookies=other).status_code in (401, 403)
    assert http_client.get("/api/v2/customer/subscription", cookies=current).status_code == 200
    with override_target(sqlite_path=db_path):
        assert row("SELECT revoked_at FROM user_sessions WHERE id='sess-other'")["revoked_at"]
        assert row("SELECT revoked_at FROM user_sessions WHERE id='sess-current'")["revoked_at"] is None


def test_a_wrong_current_password_revokes_nothing(http_client, db_path):
    _seed_customer_user(db_path)
    response = http_client.post("/api/partner/activate-account", cookies=_owner_cookie(session_id="sess-current"),
                                json={"password": "brand-new-password-22", "current_password": "wrong-password-000"})
    assert response.status_code == 403
    with override_target(sqlite_path=db_path):
        assert row("SELECT revoked_at FROM user_sessions WHERE id='sess-other'")["revoked_at"] is None


def test_change_password_page_speaks_to_a_customer(http_client, db_path):
    _seed_customer_user(db_path)
    html = http_client.get("/change-password", cookies=_owner_cookie(session_id="sess-current")).text
    assert "<title>Change password" in html
    assert "Activate partner account" not in html
    assert "Account activation" not in html


def test_customer_account_page_links_to_change_password(http_client, db_path):
    _seed_customer_user(db_path)
    response = http_client.get("/customer-portal", cookies=_owner_cookie(session_id="sess-current"))
    assert response.status_code == 200, response.headers.get("location")
    assert 'id="change-password-link" href="/change-password"' in response.text


# ------------------------------------------------------------ 6 Face Access door gate


def _door_route(method):
    for route in main.app.routes:
        if getattr(route, "path", None) == "/api/customer/cameras/{camera_id}/door-config" and method in (route.methods or ()):
            return route.endpoint
    raise AssertionError("door-config route missing")


def _seed_door_customer(db_path, *, face_access):
    conn = _seed_customer(db_path, "customer-a")
    conn.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) "
                 "VALUES('cam-door','customer-a','site-customer-a','Front door','configured',1,?)", (NOW,))
    if face_access:
        conn.execute("INSERT INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,status,created_at) "
                     "VALUES('fa','customer-a',NULL,'facial_recognition','active',?)", (NOW,))
    conn.commit()


def _save_door(monkeypatch, payload):
    import door_access
    monkeypatch.setattr(door_access, "partner_identity",
                        lambda request: {"role": "customer_owner", "customer_id": "customer-a", "email": "owner@example.test"})
    return _door_route("POST")(SimpleNamespace(headers={}, cookies={}), "cam-door", payload)


DOOR = {"door_access_enabled": True, "door_relay_channel": 1, "door_relay_pulse_ms": 3000, "door_feedback_enabled": True}


def test_without_face_access_a_door_is_only_requested(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        _seed_door_customer(db_path, face_access=False)
        result = _save_door(monkeypatch, DOOR)
        camera = row("SELECT door_access_enabled,door_access_requested,door_relay_channel,door_feedback_enabled FROM cameras WHERE id='cam-door'")
        import analytics_entitlements
        door_count = analytics_entitlements.door_count_for_customer("customer-a")
    assert result["door_access_enabled"] is False and result["face_access_required"] is True
    assert "Face Access add-on" in result["message"]
    assert camera["door_access_enabled"] == 0          # no unlock, no appliance door, no live-tile button
    assert camera["door_access_requested"] == 1
    assert camera["door_relay_channel"] == 1
    assert camera["door_feedback_enabled"] == 0
    assert door_count == 1                              # still counted, so Face Access can be bought per door


def test_with_face_access_the_door_is_enabled(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        _seed_door_customer(db_path, face_access=True)
        result = _save_door(monkeypatch, DOOR)
        camera = row("SELECT door_access_enabled,door_access_requested FROM cameras WHERE id='cam-door'")
    assert result["door_access_enabled"] is True
    assert camera["door_access_enabled"] == 1 and camera["door_access_requested"] == 0


def test_buying_face_access_turns_requested_doors_on(db_path, monkeypatch):
    import analytics_entitlements
    with override_target(sqlite_path=db_path):
        initialize_database()
        _seed_door_customer(db_path, face_access=False)
        _save_door(monkeypatch, DOOR)
        analytics_entitlements.upsert_analytics_subscription(customer_id="customer-a", analytic_key="facial_recognition", status="active")
        camera = row("SELECT door_access_enabled,door_access_requested,door_relay_channel FROM cameras WHERE id='cam-door'")
    assert camera["door_access_enabled"] == 1 and camera["door_access_requested"] == 0 and camera["door_relay_channel"] == 1


def test_turning_a_door_off_never_needs_face_access(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        _seed_door_customer(db_path, face_access=False)
        _save_door(monkeypatch, DOOR)
        result = _save_door(monkeypatch, {"door_access_enabled": False})
        camera = row("SELECT door_access_enabled,door_access_requested,door_relay_channel FROM cameras WHERE id='cam-door'")
    assert result["door_access_enabled"] is False and result["face_access_required"] is False
    assert camera["door_access_enabled"] == 0 and camera["door_access_requested"] == 0 and camera["door_relay_channel"] is None


def test_live_page_settings_say_face_access_is_required():
    import live_view_page
    camera = {"id": "cam-door", "door_access_enabled": 0, "door_access_requested": 1, "door_relay_channel": 1}
    html = live_view_page._door_access_settings_panel(camera, [], face_access=False)
    assert 'id="face-access-required"' in html
    assert 'href="/subscription-portal"' in html
    assert 'id="door-access-enabled" type="checkbox" checked' in html   # the requested door is shown as set up
    entitled = live_view_page._door_access_settings_panel({"id": "cam-door", "door_access_enabled": 1, "door_relay_channel": 1}, [], face_access=True)
    assert 'id="face-access-required"' not in entitled
    assert "Enable Face Access for this camera" in entitled


# ------------------------------------------------------------ 7 event-clip download


from test_event_media_direct_wireguard import (  # noqa: E402,F401  (fixtures)
    APPLIANCE_A_SECRET, _enable_event_media_flag, _insert_event_media, _real_mp4_bytes, client,
)
from test_event_media_direct_wireguard import _owner_cookie as _wg_owner_cookie  # noqa: E402


@pytest.fixture()
def presign_calls(monkeypatch):
    calls = []

    def fake_presign(s3_key, download_filename=None):
        calls.append((s3_key, download_filename))
        return f"https://bucket.example.test/{s3_key}?download={download_filename}"

    monkeypatch.setattr(main, "_generate_presigned_recording_url", fake_presign)
    return calls


def _event(media_id, *, local_relative_path=None, size_bytes=5000):
    with connection() as db:
        return _insert_event_media(db, media_id=media_id, camera_id="cam-a", customer_id="cust-a", site_id="site-a",
                                   appliance_id="appl-a", local_relative_path=local_relative_path, size_bytes=size_bytes)


def test_event_download_redirects_to_an_attachment_url(client, presign_calls):
    event_id = _event("dl-1")  # no local copy: not eligible for WireGuard
    response = client.get(f"/api/customer/events/cam-a/{event_id}/media/download",
                          cookies={partner_portal.SESSION_COOKIE: _wg_owner_cookie("cust-a", "owner-a@example.test")},
                          follow_redirects=False)
    assert response.status_code == 302
    assert presign_calls == [("clips/dl-1.mp4", "anyaicam-camera-1-20260919T000000Z-motion.mp4")]
    assert response.headers["location"].startswith("https://bucket.example.test/clips/dl-1.mp4")


def test_event_download_requires_the_customers_own_camera(client, presign_calls):
    event_id = _event("dl-2")
    assert client.get(f"/api/customer/events/cam-a/{event_id}/media/download", follow_redirects=False).status_code == 401
    other = {partner_portal.SESSION_COOKIE: _wg_owner_cookie("cust-b", "owner-b@example.test")}
    assert client.get(f"/api/customer/events/cam-a/{event_id}/media/download", cookies=other, follow_redirects=False).status_code == 403
    own = {partner_portal.SESSION_COOKIE: _wg_owner_cookie("cust-a", "owner-a@example.test")}
    # An event id looked up through the wrong camera is not found.
    assert client.get(f"/api/customer/events/cam-a/evt-unknown/media/download", cookies=own, follow_redirects=False).status_code == 404
    assert presign_calls == []


def test_event_download_over_wireguard_is_served_as_an_attachment(client, presign_calls, monkeypatch):
    import live_view_wireguard
    body = _real_mp4_bytes(5000)
    monkeypatch.setattr(live_view_wireguard, "_fetch_via_gateway", lambda address, port, path: body)
    event_id = _event("dl-3", local_relative_path="/recordings/clips/motion/dl-3.mp4")
    response = client.get(f"/api/customer/events/cam-a/{event_id}/media/download",
                          cookies={partner_portal.SESSION_COOKIE: _wg_owner_cookie("cust-a", "owner-a@example.test")},
                          follow_redirects=False)
    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-disposition"].startswith('attachment; filename="anyaicam-camera-1-')
    assert presign_calls == []


def test_presigned_download_url_carries_content_disposition(monkeypatch):
    import sys
    captured = {}

    class FakeS3:
        def generate_presigned_url(self, operation, Params, ExpiresIn):
            captured.update(Params)
            return "https://bucket.example.test/signed"

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=lambda *args, **kwargs: FakeS3()))
    monkeypatch.setenv("ANYAICAM_RECORDING_READ_ROLE_ARN", "arn:aws:iam::000000000000:role/read")
    monkeypatch.setenv("ANYAICAM_RECORDING_S3_BUCKET", "bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(main, "_recording_read_credentials", lambda role_arn, region: {
        "access_key_id": "a", "secret_access_key": "b", "session_token": "c"})
    assert main._generate_presigned_recording_url("clips/x.mp4", download_filename="clip.mp4") == "https://bucket.example.test/signed"
    assert captured["ResponseContentDisposition"] == 'attachment; filename="clip.mp4"'
    captured.clear()
    main._generate_presigned_recording_url("clips/x.mp4")
    assert "ResponseContentDisposition" not in captured   # playback URLs are unchanged


def test_playback_page_enables_download_for_event_clips(db_path):
    from unittest.mock import MagicMock
    request = MagicMock()
    request.query_params = {}
    with override_target(sqlite_path=db_path):
        initialize_database()
        html = main._render_customer_playback([{"id": "cam-1", "name": "Front Door", "camera_number": 1}], request)
    # The event player's onReady enables Download for the clip it holds; every
    # start/cancel records or forgets that clip; the button asks the
    # same-origin attachment route for it.
    assert "downloadButton.disabled=!selectedEventClip" in html
    assert "eventPlayer.start=(cameraId,eventId,autoplay)=>{selectedEventClip={cameraId,eventId}" in html
    assert "eventPlayer.cancel=()=>{selectedEventClip=null" in html
    assert "/media/download`" in html
    assert "if(!selectedClip&&selectedEventClip)" in html
    _assert_inline_scripts_parse(html)


# ------------------------------------------------------------ 8 viewer-local event times


def test_events_page_sends_utc_instants_for_the_browser_to_format(monkeypatch):
    monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front", "camera_number": 1}])
    monkeypatch.setattr(main, "_customer_detection_events", lambda request, **kwargs: [{
        "id": "evt-1", "camera_id": "cam-1", "camera": 1, "camera_name": "Front", "event_type": "person",
        "timestamp": "2026-10-10T23:25:16", "confidence": 0.88, "has_event_clip": False}])
    html = main._render_customer_events(SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: default)))
    body = html[html.index("<tbody>"):html.index("</tbody>")]
    assert '<time datetime="2026-10-10T23:25:16Z" data-local-time="datetime">' in body
    assert "06:25:16 PM" not in body          # the fixed America/Chicago rendering
    assert "data-local-time" in html and "anyaicamLocalTimes" in html
    _assert_inline_scripts_parse(html)


def test_alerts_send_utc_instants_for_the_browser_to_format():
    from datetime import timezone
    import smart_alerts
    item = {"id": 1, "camera_id": "cam-1", "camera_name": "Front", "event_type": "person",
            "timestamp": "2026-10-10T23:25:16", "event_id": "evt-1", "has_event_clip": False}
    groups = smart_alerts.group_alerts([item])
    content, scripts = smart_alerts.render_inbox(
        groups, view="active", tz=timezone.utc, alert_text=lambda x: ("Person", ""), clip_href=lambda x: None,
        talk_camera_ids=set(), counts={})
    assert '<time datetime="2026-10-10T23:25:16Z" data-local-time="short">' in content
    assert scripts.startswith(smart_alerts.LOCAL_TIME_SCRIPT)
    _assert_inline_scripts_parse(scripts)


# ------------------------------------------------------------ 9 AACO button above the real tab bar


def test_aaco_button_clears_the_measured_tab_bar():
    import aaco_web
    html = aaco_web.render_aaco_floating_widget()
    assert "bottom:84px}" not in html
    assert "body:has(.mobile-nav) .aaco-float-root{bottom:var(--aaco-nav-clearance,84px)}" in html
    assert "--aaco-nav-clearance" in html and "ResizeObserver" in html
    _assert_inline_scripts_parse(html)
