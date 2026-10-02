"""Users & household (2026-10-01): an account owner adds household members with
their own sign-in, a single-use expiring invitation link and per-person
permissions -- on the existing customer_viewer identity model."""
import hashlib
import re
import sqlite3
from datetime import datetime, timedelta

import pytest

from database_backend import override_target
from test_pricing_ff_commission import (  # noqa: F401 -- fixtures
    OWNER,
    _cookie,
    _seed,
    db_path,

    portal,
)

PASSWORD = "correct horse battery"


@pytest.fixture()
def mail(monkeypatch):
    sent = []

    class _Mail:
        def send(self, message_type, to, subject, text, html=None, metadata=None, images=None):
            sent.append({"type": message_type, "to": to, "subject": subject, "text": text, "html": html})
            return {"status": "sent", "id": f"m{len(sent)}"}

    import email_service
    monkeypatch.setattr(email_service, "get_email_service", lambda: _Mail())
    monkeypatch.setenv("ANYAICAM_PUBLIC_URL", "https://portal.example.test")
    import household_users
    for limiter in (household_users._invite_limiter, household_users._join_ip_limiter):
        limiter.events.clear()
    return sent


def _home(db_path, *, customer_id="cust-1", email="owner@example.test", door_cameras=("cam-3",)):
    _seed(db_path, customer_id=customer_id, email=email)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
                 "VALUES(?,?,?,?,'customer_owner','x',1,?,'2026-01-01')",
                 (f"owner-{customer_id}", "partner-1", email, "Alex Owner", customer_id))
    for camera_id in door_cameras:
        conn.execute("UPDATE cameras SET door_access_enabled=1,door_relay_channel=1,door_relay_pulse_ms=3000 WHERE id=?", (camera_id,))
    conn.commit()
    conn.close()


def _invite(client, *, name="Maria", email="maria@example.test", permissions=None, owner=OWNER):
    body = {"name": name, "email": email}
    if permissions is not None:
        body["permissions"] = permissions
    return client.post("/api/customer/household/invitations", json=body, cookies=_cookie(*owner))


def _token(mail, index=-1):
    return re.search(r"/customer/join\?token=([A-Za-z0-9_\-]+)", mail[index]["text"]).group(1)


def _join(client, token, password=PASSWORD, confirm=None):
    return client.post("/api/customer/household/join",
                       json={"token": token, "password": password, "confirm_password": confirm or password})


def _login(client, email="maria@example.test", password=PASSWORD):
    return client.post("/api/partner-login", json={"email": email, "password": password, "customer_only": True})


def _q(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _member_id(db_path, email="maria@example.test"):
    return _q(db_path, "SELECT id FROM partner_users WHERE email=?", (email,))[0]["id"]


# ------------------------------------------------------------------ invitations

def test_invitation_is_a_single_use_link_never_a_temporary_password(portal, db_path, mail):
    client, _, _ = portal
    _home(db_path)
    response = _invite(client)
    assert response.status_code == 200, response.text
    assert response.json()["email_status"] == "sent" and "temporary_password" not in response.json()
    assert len(mail) == 1 and mail[0]["to"] == "maria@example.test" and mail[0]["type"] == "invitation"
    raw = _token(mail)
    assert "https://portal.example.test/customer/join?token=" in mail[0]["text"]
    assert "password:" not in mail[0]["text"].lower()
    row = _q(db_path, "SELECT * FROM invitations WHERE email='maria@example.test'")[0]
    assert row["status"] == "pending" and row["token_hash"] == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in str(row) and row["temporary_password_hash"] is None
    # No login exists until the invitee accepts.
    assert _q(db_path, "SELECT * FROM partner_users WHERE email='maria@example.test'") == []


def test_accepting_creates_a_separate_login_in_the_owners_household(portal, db_path, mail):
    client, _, _ = portal
    _home(db_path)
    _invite(client, permissions={"camera_ids": ["cam-1", "cam-2"], "talk": True})
    page = client.get(f"/customer/join?token={_token(mail)}")
    assert page.status_code == 200 and "maria@example.test" in page.text and "Create my account" in page.text
    assert page.headers["cache-control"] == "no-store"
    joined = _join(client, _token(mail))
    assert joined.status_code == 200, joined.text
    user = _q(db_path, "SELECT * FROM partner_users WHERE email='maria@example.test'")[0]
    assert user["role"] == "customer_viewer" and user["customer_id"] == "cust-1" and user["account_status"] == "active"
    assert user["must_change_password"] == 0 and user["camera_access_mode"] == "selected"
    assert _q(db_path, "SELECT * FROM identity_grants WHERE user_id=? AND revoked_at IS NULL", (user["id"],))[0]["scope_id"] == "cust-1"
    rows = {r["camera_id"]: r for r in _q(db_path, "SELECT * FROM customer_camera_permissions WHERE user_id=?", (user["id"],))}
    assert set(rows) == {"cam-1", "cam-2"}
    assert all(r["can_live"] and r["can_playback"] and r["can_alerts"] and r["can_talk"] and not r["can_settings"]
               and not r["can_unlock"] and not r["can_download"] for r in rows.values())
    login = _login(client)
    assert login.status_code == 303, login.text
    # The owner's own login is untouched and still separate.
    assert _q(db_path, "SELECT role FROM partner_users WHERE email='owner@example.test'")[0]["role"] == "customer_owner"


def test_a_link_works_exactly_once(portal, db_path, mail):
    client, _, _ = portal
    _home(db_path)
    _invite(client)
    raw = _token(mail)
    assert _join(client, raw).status_code == 200
    again = _join(client, raw, password="another long password")
    assert again.status_code == 400 and "invalid, expired, or already used" in again.json()["detail"]
    assert "can't be used" in client.get(f"/customer/join?token={raw}").text
    assert _login(client).status_code == 303  # the first password still stands


def test_expired_cancelled_and_unknown_links_are_refused_identically(portal, db_path, mail):
    client, _, _ = portal
    _home(db_path)
    _invite(client, email="a@example.test")
    expired = _token(mail)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE invitations SET expires_at=? WHERE email='a@example.test'", ((datetime.now() - timedelta(minutes=1)).isoformat(),))
    conn.commit(); conn.close()
    _invite(client, email="b@example.test")
    cancelled_id = _q(db_path, "SELECT id FROM invitations WHERE email='b@example.test'")[0]["id"]
    cancelled = _token(mail)
    assert client.post(f"/api/customer/household/invitations/{cancelled_id}/cancel", cookies=_cookie(*OWNER)).status_code == 200
    for raw in (expired, cancelled, "not-a-real-token", ""):
        response = _join(client, raw)
        assert response.status_code == 400 and response.json()["detail"] == "This invitation link is invalid, expired, or already used."
    assert _q(db_path, "SELECT * FROM partner_users WHERE email IN ('a@example.test','b@example.test')") == []


def test_resend_replaces_the_link_and_is_rate_limited(portal, db_path, mail):
    client, _, _ = portal
    _home(db_path)
    _invite(client)
    first = _token(mail)
    invitation_id = _q(db_path, "SELECT id FROM invitations WHERE email='maria@example.test'")[0]["id"]
    too_soon = client.post(f"/api/customer/household/invitations/{invitation_id}/resend", cookies=_cookie(*OWNER))
    assert too_soon.status_code == 400 and "wait a minute" in too_soon.json()["detail"]
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE invitations SET last_sent_at=? WHERE id=?", ((datetime.now() - timedelta(minutes=5)).isoformat(), invitation_id))
    conn.commit(); conn.close()
    assert client.post(f"/api/customer/household/invitations/{invitation_id}/resend", cookies=_cookie(*OWNER)).status_code == 200
    second = _token(mail)
    assert second != first
    assert _join(client, first).status_code == 400
    assert _join(client, second).status_code == 200


def test_mismatched_and_short_passwords_leave_the_link_usable(portal, db_path, mail):
    client, _, _ = portal
    _home(db_path)
    _invite(client)
    raw = _token(mail)
    assert _join(client, raw, password="short").status_code == 400
    assert _join(client, raw, confirm="a different long password").status_code == 400
    assert _join(client, raw).status_code == 200


def test_an_email_that_already_has_an_account_gets_one_generic_refusal(portal, db_path, mail):
    client, _, _ = portal
    _home(db_path)
    _home(db_path, customer_id="cust-2", email="other-owner@example.test", door_cameras=())
    for email in ("other-owner@example.test", "sales@example.test", "owner@example.test"):
        response = _invite(client, email=email)
        assert response.status_code == 400
        assert response.json()["detail"] == "This email can't be invited. Use a different email address."
    assert mail == []


def test_invitation_input_is_validated(portal, db_path, mail):
    client, _, _ = portal
    _home(db_path)
    assert _invite(client, name="").status_code == 400
    assert _invite(client, email="not-an-email").status_code == 400


# ------------------------------------------------------------------ who may manage users

def test_only_the_owner_manages_users(portal, db_path, mail):
    client, _, _ = portal
    _home(db_path)
    viewer = ("maria@example.test", "customer_viewer", "cust-1")
    for method, url in (("get", "/api/customer/household"), ("post", "/api/customer/household/invitations")):
        response = getattr(client, method)(url, cookies=_cookie(*viewer), **({"json": {"name": "x", "email": "x@example.test"}} if method == "post" else {}))
        assert response.status_code == 403
    assert client.get("/api/customer/household").status_code in (401, 403)
    for role in ("partner_owner", "salesperson", "technician"):
        assert client.get("/api/customer/household", cookies=_cookie("p@example.test", role, "cust-1")).status_code == 403
    page = client.get("/customer/household", cookies=_cookie(*viewer))
    assert page.status_code == 403 and "Only the account owner" in page.text
    assert client.get("/customer/household").status_code == 303


def test_another_customers_owner_can_never_reach_this_household(portal, db_path, mail):
    client, _, _ = portal
    _home(db_path)
    _home(db_path, customer_id="cust-2", email="other-owner@example.test", door_cameras=())
    _invite(client)
    _join(client, _token(mail))
    member = _member_id(db_path)
    invitation_id = _q(db_path, "SELECT id FROM invitations WHERE email='maria@example.test'")[0]["id"]
    other = ("other-owner@example.test", "customer_owner", "cust-2")
    overview = client.get("/api/customer/household", cookies=_cookie(*other)).json()
    assert all(u["email"] != "maria@example.test" for u in overview["users"]) and overview["invitations"] == []
    for action in ("disable", "remove", "enable"):
        assert client.post(f"/api/customer/household/users/{member}/{action}", cookies=_cookie(*other)).status_code == 404
    assert client.put(f"/api/customer/household/users/{member}/permissions", json={"camera_ids": ["cam-1"]},
                      cookies=_cookie(*other)).status_code == 404
    assert client.get(f"/api/customer/household/users/{member}/permissions", cookies=_cookie(*other)).status_code == 404
    for action in ("resend", "cancel"):
        assert client.post(f"/api/customer/household/invitations/{invitation_id}/{action}", cookies=_cookie(*other)).status_code == 404
    # ...and the owner cannot be targeted as a "household user" either.
    assert client.post("/api/customer/household/users/owner-cust-1/disable", cookies=_cookie(*OWNER)).status_code == 404


def test_permissions_never_name_another_customers_camera(portal, db_path, mail):
    client, _, _ = portal
    _home(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-9','partner-1','X','x@example.test','active','2026-01-01')")
    conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-9','cust-9','Theirs','2026-01-01')")
    conn.execute("INSERT INTO cameras(id,customer_id,site_id,name,camera_number,created_at,door_access_enabled,door_relay_channel) "
                 "VALUES('foreign-cam','cust-9','site-9','Theirs',1,'2026-01-01',1,1)")
    conn.commit(); conn.close()
    _invite(client, permissions={"camera_ids": ["cam-1", "foreign-cam"], "door_ids": ["foreign-cam", "cam-1"]})
    _join(client, _token(mail))
    rows = _q(db_path, "SELECT camera_id,can_unlock FROM customer_camera_permissions WHERE user_id=?", (_member_id(db_path),))
    # cam-1 is not a door, so no unlock; the foreign camera is dropped entirely.
    assert rows == [{"camera_id": "cam-1", "can_unlock": 0}]


# ------------------------------------------------------------------ permissions in force

def test_door_unlock_follows_the_per_door_grant(portal, db_path, mail):
    client, _, _ = portal
    _home(db_path)
    _invite(client, permissions={"camera_ids": ["cam-1", "cam-3"], "door_ids": []})
    _join(client, _token(mail))
    viewer = ("maria@example.test", "customer_viewer", "cust-1")
    denied = client.post("/api/customer/cameras/cam-3/door/unlock", cookies=_cookie(*viewer))
    assert denied.status_code == 403
    member = _member_id(db_path)
    saved = client.put(f"/api/customer/household/users/{member}/permissions",
                       json={"camera_ids": ["cam-1", "cam-3"], "door_ids": ["cam-3"], "live": True}, cookies=_cookie(*OWNER))
    assert saved.status_code == 200 and saved.json()["permissions"]["door_ids"] == ["cam-3"]
    allowed = client.post("/api/customer/cameras/cam-3/door/unlock", cookies=_cookie(*viewer))
    # Authorized -- and honest that there is no relay hardware behind it.
    assert allowed.status_code == 409 and "nothing was unlocked" in allowed.json()["detail"]


def test_people_and_face_access_are_separate_explicit_grants(portal, db_path, mail):
    client, _, _ = portal
    _home(db_path)
    _invite(client)
    _join(client, _token(mail))
    viewer = ("maria@example.test", "customer_viewer", "cust-1")
    blocked = client.get("/api/aac/people", params={"customer_id": "cust-1"}, cookies=_cookie(*viewer))
    assert blocked.status_code == 403 and blocked.json()["detail"] == "You do not have permission for this action."
    member = _member_id(db_path)
    client.put(f"/api/customer/household/users/{member}/permissions", json={"camera_ids": ["cam-1"], "people": True}, cookies=_cookie(*OWNER))
    seen = client.get("/api/aac/people", params={"customer_id": "cust-1"}, cookies=_cookie(*viewer))
    assert seen.status_code != 403 or seen.json()["detail"] != "You do not have permission for this action."
    # Seeing People never lets them enroll or change anyone.
    manage = client.post("/api/aac/people", json={"customer_id": "cust-1", "display_name": "X"}, cookies=_cookie(*viewer))
    assert manage.status_code == 403
    # Inviting a household member never enrolls them for Face Access.
    assert _q(db_path, "SELECT * FROM facial_people WHERE display_name='Maria'") == []


def test_a_viewer_from_before_household_keeps_people_view_only(db_path):
    _home(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
                 "VALUES('legacy','partner-1','legacy@example.test','L','customer_viewer','x',1,'cust-1','2026-01-01')")
    conn.commit(); conn.close()
    import household_users as hu
    identity = {"email": "legacy@example.test", "role": "customer_viewer", "customer_id": "cust-1"}
    with override_target(sqlite_path=str(db_path)):
        assert hu.facial_permission_allowed(identity, "facial.view") is True
        assert hu.facial_permission_allowed(identity, "facial.manage") is False
        assert hu.account_permission(identity, "backup_access") is False
        assert hu.account_permission({"email": "owner@example.test", "role": "customer_owner", "customer_id": "cust-1"}, "backup_access")


def test_a_face_access_manager_changes_only_doors_they_can_unlock():
    import household_users as hu
    before = {"access_enabled": True, "access_starts_on": "2026-01-01", "access_expires_on": None, "unit": "4B", "site_id": "site-1",
              "doors": [{"camera_id": "front", "allowed": True, "start": None, "end": None, "days": []},
                        {"camera_id": "garage", "allowed": False, "start": None, "end": None, "days": []}]}
    attempt = {"access_enabled": False, "unit": "9Z", "doors": [{"camera_id": "front", "allowed": False},
                                                               {"camera_id": "garage", "allowed": True}]}
    restricted = hu.restrict_face_access_payload(before, attempt, {"garage"})
    doors = {d["camera_id"]: d["allowed"] for d in restricted["doors"]}
    assert doors == {"front": True, "garage": True}  # front kept; garage is theirs to change
    assert restricted["access_enabled"] is True and restricted["unit"] == "4B"  # account-wide switches kept
    everything = hu.restrict_face_access_payload(before, attempt, {"front", "garage"})
    assert everything["access_enabled"] is False and {d["camera_id"]: d["allowed"] for d in everything["doors"]} == {"front": False, "garage": True}
    assert hu.restrict_face_access_payload(before, attempt, None) is attempt  # the owner is never restricted


# ------------------------------------------------------------------ disable / remove

def _session_cookie(client):
    response = _login(client)
    assert response.status_code == 303, response.text
    import partner_portal
    return {partner_portal.SESSION_COOKIE: response.cookies[partner_portal.SESSION_COOKIE]}


def test_disable_signs_the_person_out_everywhere_and_enable_restores_them(portal, db_path, mail):
    client, _, _ = portal
    _home(db_path)
    _invite(client)
    _join(client, _token(mail))
    session = _session_cookie(client)
    assert client.get("/dashboard", cookies=session).status_code == 200
    member = _member_id(db_path)
    assert client.post(f"/api/customer/household/users/{member}/disable", cookies=_cookie(*OWNER)).status_code == 200
    assert client.get("/dashboard", cookies=session).status_code in (303, 401, 403)
    assert _login(client).status_code == 403
    assert _q(db_path, "SELECT * FROM identity_grants WHERE user_id=? AND revoked_at IS NULL", (member,)) == []
    overview = client.get("/api/customer/household", cookies=_cookie(*OWNER)).json()
    assert next(u for u in overview["users"] if u["id"] == member)["status"] == "disabled"
    assert client.post(f"/api/customer/household/users/{member}/enable", cookies=_cookie(*OWNER)).status_code == 200
    assert _login(client).status_code == 303


def test_remove_takes_every_permission_and_the_person_can_be_invited_again(portal, db_path, mail):
    client, _, _ = portal
    _home(db_path)
    _invite(client, permissions={"camera_ids": ["cam-3"], "door_ids": ["cam-3"], "people": True})
    _join(client, _token(mail))
    session = _session_cookie(client)
    member = _member_id(db_path)
    assert client.post(f"/api/customer/household/users/{member}/remove", cookies=_cookie(*OWNER)).status_code == 200
    assert client.get("/dashboard", cookies=session).status_code in (303, 401, 403)
    assert _login(client).status_code == 403
    assert _q(db_path, "SELECT * FROM customer_camera_permissions WHERE user_id=?", (member,)) == []
    assert _q(db_path, "SELECT * FROM customer_user_permissions WHERE user_id=?", (member,)) == []
    assert all(u["id"] != member for u in client.get("/api/customer/household", cookies=_cookie(*OWNER)).json()["users"])
    # Re-inviting the same person reuses their record, with fresh permissions.
    assert _invite(client, permissions={"camera_ids": ["cam-1"]}).status_code == 200
    assert _join(client, _token(mail), password="a brand new long password").status_code == 200
    assert _login(client, password="a brand new long password").status_code == 303
    assert _q(db_path, "SELECT camera_id,can_unlock FROM customer_camera_permissions WHERE user_id=?", (member,)) == [
        {"camera_id": "cam-1", "can_unlock": 0}]


def test_a_disabled_household_owners_customer_link_stops_working(portal, db_path, mail):
    client, _, _ = portal
    _home(db_path)
    _invite(client)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE customers SET status='suspended' WHERE id='cust-1'")
    conn.commit(); conn.close()
    assert _join(client, _token(mail)).status_code == 400


# ------------------------------------------------------------------ page + nav

def test_owner_page_and_nav(portal, db_path, mail):
    client, _, _ = portal
    _home(db_path)
    page = client.get("/customer/household", cookies=_cookie(*OWNER))
    assert page.status_code == 200 and 'id="hh-add"' in page.text and "Users &amp; household" in page.text
    assert 'href="/customer/household"' in client.get("/customer-app-settings", cookies=_cookie(*OWNER)).text
    viewer_nav = client.get("/customer-app-settings", cookies=_cookie("maria@example.test", "customer_viewer", "cust-1")).text
    assert 'href="/customer/household"' not in viewer_nav


def test_the_real_preview_mail_backend_accepts_the_invitation(portal, db_path, tmp_path, monkeypatch):
    """No mocked mail: the email backend the product actually runs."""
    import email_service
    import household_users
    monkeypatch.setattr(email_service, "get_email_service", lambda: email_service.PreviewEmail(tmp_path / "mail"))
    monkeypatch.setenv("ANYAICAM_PUBLIC_URL", "https://portal.example.test")
    household_users._invite_limiter.events.clear()
    client, _, _ = portal
    _home(db_path)
    response = _invite(client)
    assert response.status_code == 200, response.text
    assert response.json()["email_status"] == "preview" and "preview mode" in response.json()["message"]
    previews = list((tmp_path / "mail").glob("*.json"))
    assert len(previews) == 1 and "/customer/join?token=" in previews[0].read_text(encoding="utf-8")


def test_a_mail_failure_keeps_the_invitation_for_resend(portal, db_path, monkeypatch):
    import email_service
    import household_users

    class _Broken:
        def send(self, *args, **kwargs):
            raise RuntimeError("smtp down")

    monkeypatch.setattr(email_service, "get_email_service", lambda: _Broken())
    household_users._invite_limiter.events.clear()
    client, _, _ = portal
    _home(db_path)
    response = _invite(client)
    assert response.status_code == 200 and response.json()["email_status"] == "failed"
    assert "Use Resend" in response.json()["message"]
    assert _q(db_path, "SELECT status FROM invitations WHERE email='maria@example.test'")[0]["status"] == "pending"


def test_the_account_page_offers_users_and_household_to_the_owner_only(portal, db_path, mail):
    """The phone tab bar has no Settings item but has Account (/customer-portal)."""
    client, _, _ = portal
    _home(db_path)
    _invite(client)
    _join(client, _token(mail))
    assert 'href="/customer/household"' in client.get("/customer-portal", cookies=_cookie(*OWNER)).text
    viewer = client.get("/customer-portal", cookies=_cookie("maria@example.test", "customer_viewer", "cust-1"))
    assert viewer.status_code == 200 and 'href="/customer/household"' not in viewer.text


def test_the_face_access_grant_lets_a_member_manage_people_and_shows_edit_controls(portal, db_path, mail):
    """The customer_viewer role never carries facial.manage; the owner's
    Manage Face Access grant is what gives it to this one person."""
    client, _, _ = portal
    _home(db_path)
    _invite(client)
    _join(client, _token(mail))
    viewer = ("maria@example.test", "customer_viewer", "cust-1")
    import facial_recognition_ui as fr
    identity = {"email": "maria@example.test", "role": "customer_viewer", "customer_id": "cust-1"}
    with override_target(sqlite_path=str(db_path)):
        assert fr.facial_allowed(identity, "facial.manage") is False
    member = _member_id(db_path)
    client.put(f"/api/customer/household/users/{member}/permissions", json={"camera_ids": ["cam-1"], "face_access": True},
               cookies=_cookie(*OWNER))
    with override_target(sqlite_path=str(db_path)):
        assert fr.facial_allowed(identity, "facial.manage") is True
        assert fr.facial_allowed(identity, "facial.view") is True
        assert fr.facial_allowed({"role": "salesperson"}, "facial.manage") is False
    created = client.post("/api/aac/people", json={"customer_id": "cust-1", "display_name": "Cleaner"}, cookies=_cookie(*viewer))
    assert created.status_code != 403, created.text


def test_delivery_messages_say_what_actually_happened():
    import household_users as hu
    assert hu._delivery_message("sent", "a@x.test", first=True) == "Invitation sent to a@x.test."
    assert hu._delivery_message("sent", "a@x.test", first=False) == "Invitation sent again to a@x.test."
    assert "created" in hu._delivery_message("preview", "a@x.test", first=True)
    assert hu._delivery_message("preview", "a@x.test", first=False).startswith("New invitation link created")
    assert "could not be sent" in hu._delivery_message("failed", "a@x.test", first=True)
