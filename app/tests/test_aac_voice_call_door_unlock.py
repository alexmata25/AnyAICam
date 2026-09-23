"""AAC Voice Call -- owner-approved door access (2026-09-23).

Route-level tests for the two-step unlock flow (POST .../door/
unlock-request then POST .../door/unlock-confirm), reusing test_door_
access.py's own established pattern: pull the real route function off
a standalone FastAPI() app (matching test_aac_voice_call_foundation.py's
own lightweight-app fixture, since this module has no dependency on the
full main.app), monkeypatch partner_identity on the module it was
imported into, and use a fresh, isolated MockRelayProvider per test so
every assertion can inspect exactly what was (or, critically, was NOT)
dispatched -- MockRelayProvider "records every call it receives... and
NEVER touches any real I/O -- there is no hardware to touch" (its own
docstring); no test in this file, and no code this file exercises, ever
calls anything hardware-backed.

The five adversarial cases the product spec names, each proven at the
dispatch layer (provider.calls), not just by HTTP status code:
  - unauthorized user cannot unlock
  - cross-tenant user cannot unlock
  - a stale (already-ended/dismissed) call session cannot unlock
  - a duplicate/replayed confirmation does not trigger a second unlock
  - AI-generated transcript/claimed-identity content cannot influence
    the authorization decision

Camera-count-agnostic: fleets of 2 and 7 cameras (never the 5-camera
Ryzen pilot number) across the parametrized cases.
"""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import aac_voice_call
import aac_voice_call_door
import aac_voice_call_events as store
import door_access
import partner_portal
import relay_control
from database_backend import override_target
from partner_db import connection, initialize_database, row


def _shell(title, active, content, scripts=""):
    return f"<html><title>{title}</title>{content}{scripts}</html>"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_aac_voice_call_door_unlock.db"


@pytest.fixture(autouse=True)
def _isolated_relay(monkeypatch):
    provider = relay_control.MockRelayProvider(cooldown_seconds=0.0)
    monkeypatch.setattr(relay_control, "_provider", provider)
    yield provider
    relay_control.reset_provider()


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        app = FastAPI()
        aac_voice_call.register_aac_voice_call_routes(app, _shell)
        with TestClient(app) as test_client:
            yield test_client


def _owner_cookie(customer_id="cust-a", email="owner@example.test"):
    return partner_portal._token(email, "customer_owner", None, customer_id, None)


def _viewer_cookie(customer_id="cust-a", email="viewer@example.test"):
    return partner_portal._token(email, "customer_viewer", None, customer_id, None)


def _seed_tenant_with_door(db_path, *, customer_id, camera_id, partner_id, owner_email, fleet_size=2, door_channel=1, viewer_email=None, viewer_can_unlock=False):
    now = datetime.now().isoformat()
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", now))
            db.execute(
                "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
                (customer_id, partner_id, f"Customer {customer_id}", f"{customer_id}@example.test", "active", now),
            )
            site_id = f"site-{customer_id}"
            db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Main Site", now))
            db.execute(
                "INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
                (f"app-{customer_id}", customer_id, site_id, f"cloud-{customer_id}", now),
            )
            db.execute(
                "INSERT OR IGNORE INTO partner_users(id,partner_id,email,name,role,password_hash,approved,account_status,customer_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"user-{customer_id}", partner_id, owner_email, "Owner", "customer_owner", "x", 1, "active", customer_id, now),
            )
            if viewer_email:
                db.execute(
                    "INSERT OR IGNORE INTO partner_users(id,partner_id,email,name,role,password_hash,approved,account_status,customer_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (f"viewer-{customer_id}", partner_id, viewer_email, "Viewer", "customer_viewer", "x", 1, "active", customer_id, now),
                )
            for n in range(1, fleet_size + 1):
                this_camera_id = camera_id if n == 1 else f"cam-{customer_id}-{n}"
                is_door = n == 1
                db.execute(
                    "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,camera_number,door_access_enabled,door_relay_channel,door_relay_pulse_ms,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (this_camera_id, customer_id, site_id, f"app-{customer_id}", "Front Door" if is_door else f"Camera {n}", "configured", n,
                     1 if is_door else 0, door_channel if is_door else None, 3000 if is_door else None, now),
                )
            if viewer_email and viewer_can_unlock:
                db.execute(
                    "INSERT INTO customer_camera_permissions(user_id,camera_id,can_alerts,can_settings,can_talk,can_unlock) VALUES(?,?,1,0,0,1)",
                    (f"viewer-{customer_id}", camera_id),
                )
        # Called AFTER the with-connection block above has committed --
        # set_entrance_camera() opens its own connection, and SQLite
        # will not grant a second writer while the first transaction is
        # still open.
        store.set_entrance_camera(customer_id=customer_id, camera_id=camera_id, enabled=True, configured_by=owner_email)
        return site_id


def _create_and_notify_event(customer_id, camera_id, *, transcript_text=""):
    result = aac_voice_call.trigger_visitor_event(customer_id=customer_id, camera_id=camera_id, transcript_text=transcript_text)
    return result["event_id"]


# ----------------------------------------------------------------- happy path


def test_owner_can_request_and_confirm_unlock_end_to_end(client, db_path, _isolated_relay):
    _seed_tenant_with_door(db_path, customer_id="cust-a", camera_id="cam-a-1", partner_id="partner-a", owner_email="owner@example.test", fleet_size=2)
    event_id = _create_and_notify_event("cust-a", "cam-a-1", transcript_text="I have a delivery.")
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")}

    request_response = client.post(f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-request", cookies=cookies)
    assert request_response.status_code == 200
    token = request_response.json()["confirm_token"]
    assert request_response.json()["door_name"] == "Front Door"

    confirm_response = client.post(
        f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-confirm", cookies=cookies, json={"confirm_token": token},
    )
    assert confirm_response.status_code == 200
    assert confirm_response.json()["door_name"] == "Front Door"
    assert len(_isolated_relay.calls) == 1
    assert _isolated_relay.calls[0].channel == 1
    assert _isolated_relay.calls[0].reason == "aac_voice_call_unlock:cam-a-1"

    audit_row = row(
        "SELECT * FROM door_access_events WHERE aac_voice_call_event_id=? ORDER BY created_at DESC LIMIT 1", (event_id,),
    )
    assert audit_row["trigger_type"] == "aac_voice_call"
    assert audit_row["success"] == 1
    assert audit_row["actor_email"] == "owner@example.test"
    assert audit_row["camera_id"] == "cam-a-1"


def test_viewer_with_explicit_can_unlock_grant_succeeds(client, db_path, _isolated_relay):
    _seed_tenant_with_door(
        db_path, customer_id="cust-a", camera_id="cam-a-1", partner_id="partner-a", owner_email="owner@example.test",
        fleet_size=2, viewer_email="viewer@example.test", viewer_can_unlock=True,
    )
    event_id = _create_and_notify_event("cust-a", "cam-a-1")
    cookies = {partner_portal.SESSION_COOKIE: _viewer_cookie("cust-a", "viewer@example.test")}

    token = client.post(f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-request", cookies=cookies).json()["confirm_token"]
    confirm_response = client.post(
        f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-confirm", cookies=cookies, json={"confirm_token": token},
    )
    assert confirm_response.status_code == 200
    assert len(_isolated_relay.calls) == 1


# ------------------------------------------------------------- adversarial: unauthorized


def test_viewer_without_can_unlock_grant_is_denied_at_request_step(client, db_path, _isolated_relay):
    _seed_tenant_with_door(
        db_path, customer_id="cust-a", camera_id="cam-a-1", partner_id="partner-a", owner_email="owner@example.test",
        fleet_size=7, viewer_email="viewer@example.test", viewer_can_unlock=False,
    )
    event_id = _create_and_notify_event("cust-a", "cam-a-1")
    cookies = {partner_portal.SESSION_COOKIE: _viewer_cookie("cust-a", "viewer@example.test")}

    response = client.post(f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-request", cookies=cookies)
    assert response.status_code == 403
    assert _isolated_relay.calls == []
    unclaimed = row("SELECT COUNT(*) AS n FROM aac_voice_call_unlock_confirmations WHERE event_id=?", (event_id,))
    assert unclaimed["n"] == 0


def test_unauthorized_user_cannot_forge_a_confirm_without_a_real_request(client, db_path, _isolated_relay):
    """Even if an attacker somehow obtains/guesses a plausible-looking
    token string, confirm_unlock() re-checks can_unlock fresh before
    ever consulting the token table -- denied before the token lookup
    even happens."""
    _seed_tenant_with_door(
        db_path, customer_id="cust-a", camera_id="cam-a-1", partner_id="partner-a", owner_email="owner@example.test",
        fleet_size=2, viewer_email="viewer@example.test", viewer_can_unlock=False,
    )
    event_id = _create_and_notify_event("cust-a", "cam-a-1")
    cookies = {partner_portal.SESSION_COOKIE: _viewer_cookie("cust-a", "viewer@example.test")}

    response = client.post(
        f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-confirm", cookies=cookies, json={"confirm_token": "guessed-token-value"},
    )
    assert response.status_code == 403
    assert _isolated_relay.calls == []


# ------------------------------------------------------------- adversarial: cross-tenant


def test_cross_tenant_user_cannot_unlock_another_tenants_door(client, db_path, _isolated_relay):
    _seed_tenant_with_door(db_path, customer_id="cust-a", camera_id="cam-a-1", partner_id="partner-a", owner_email="owner-a@example.test", fleet_size=2)
    _seed_tenant_with_door(db_path, customer_id="cust-b", camera_id="cam-b-1", partner_id="partner-b", owner_email="owner-b@example.test", fleet_size=2)
    event_id = _create_and_notify_event("cust-a", "cam-a-1")

    # cust-b's own owner tries to act on cust-a's real event id.
    cookies_b = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-b", "owner-b@example.test")}
    response = client.post(f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-request", cookies=cookies_b)
    assert response.status_code == 404  # tenant-scoped lookup: indistinguishable from nonexistent, never leaks that cust-a's event exists
    assert _isolated_relay.calls == []


def test_cross_tenant_confirm_with_a_syntactically_valid_token_still_fails(client, db_path, _isolated_relay):
    """A real token, issued to cust-a's own owner for cust-a's own
    event, presented by a cust-b session: confirm_unlock() re-resolves
    the event under cust-b's OWN customer_id first, which 404s before
    the token (scoped to cust-a) could ever be checked."""
    _seed_tenant_with_door(db_path, customer_id="cust-a", camera_id="cam-a-1", partner_id="partner-a", owner_email="owner-a@example.test", fleet_size=2)
    _seed_tenant_with_door(db_path, customer_id="cust-b", camera_id="cam-b-1", partner_id="partner-b", owner_email="owner-b@example.test", fleet_size=2)
    event_id = _create_and_notify_event("cust-a", "cam-a-1")
    cookies_a = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")}
    token = client.post(f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-request", cookies=cookies_a).json()["confirm_token"]

    cookies_b = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-b", "owner-b@example.test")}
    response = client.post(
        f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-confirm", cookies=cookies_b, json={"confirm_token": token},
    )
    assert response.status_code == 404
    assert _isolated_relay.calls == []
    # The real token, issued to the legitimate owner, must still be usable afterward.
    confirm_a = client.post(
        f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-confirm", cookies=cookies_a, json={"confirm_token": token},
    )
    assert confirm_a.status_code == 200
    assert len(_isolated_relay.calls) == 1


# --------------------------------------------------------------- adversarial: stale call


@pytest.mark.parametrize("terminal_action", ["end", "dismiss"])
def test_stale_call_session_cannot_be_used_to_unlock(client, db_path, _isolated_relay, terminal_action):
    _seed_tenant_with_door(db_path, customer_id="cust-a", camera_id="cam-a-1", partner_id="partner-a", owner_email="owner@example.test", fleet_size=2)
    event_id = _create_and_notify_event("cust-a", "cam-a-1")
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")}
    client.post(f"/api/customer/aac/voice-call/events/{event_id}/{terminal_action}", cookies=cookies)

    response = client.post(f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-request", cookies=cookies)
    assert response.status_code == 409
    assert _isolated_relay.calls == []


def test_call_ending_between_request_and_confirm_blocks_the_confirm(client, db_path, _isolated_relay):
    """The call can legitimately end in the window between the two
    steps (the other party hangs up, or the homeowner ends it from
    another device) -- confirm_unlock() re-checks state fresh, not just
    at request time."""
    _seed_tenant_with_door(db_path, customer_id="cust-a", camera_id="cam-a-1", partner_id="partner-a", owner_email="owner@example.test", fleet_size=2)
    event_id = _create_and_notify_event("cust-a", "cam-a-1")
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")}
    token = client.post(f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-request", cookies=cookies).json()["confirm_token"]

    client.post(f"/api/customer/aac/voice-call/events/{event_id}/end", cookies=cookies)

    response = client.post(
        f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-confirm", cookies=cookies, json={"confirm_token": token},
    )
    assert response.status_code == 409
    assert _isolated_relay.calls == []


# ---------------------------------------------------------- adversarial: replay


def test_duplicate_replayed_confirmation_does_not_trigger_a_second_unlock(client, db_path, _isolated_relay):
    _seed_tenant_with_door(db_path, customer_id="cust-a", camera_id="cam-a-1", partner_id="partner-a", owner_email="owner@example.test", fleet_size=2)
    event_id = _create_and_notify_event("cust-a", "cam-a-1")
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")}
    token = client.post(f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-request", cookies=cookies).json()["confirm_token"]

    first = client.post(f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-confirm", cookies=cookies, json={"confirm_token": token})
    assert first.status_code == 200
    second = client.post(f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-confirm", cookies=cookies, json={"confirm_token": token})
    assert second.status_code == 409

    assert len(_isolated_relay.calls) == 1  # not 2


def test_expired_confirmation_token_is_rejected(client, db_path, _isolated_relay):
    _seed_tenant_with_door(db_path, customer_id="cust-a", camera_id="cam-a-1", partner_id="partner-a", owner_email="owner@example.test", fleet_size=2)
    event_id = _create_and_notify_event("cust-a", "cam-a-1")
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")}
    token = client.post(f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-request", cookies=cookies).json()["confirm_token"]

    # Force the confirmation into the past, matching consume_password_
    # reset()'s own established atomic-claim pattern this module reuses.
    past = (datetime.now() - timedelta(seconds=aac_voice_call_door.CONFIRMATION_TTL_SECONDS + 5)).isoformat()
    with connection() as db:
        db.execute("UPDATE aac_voice_call_unlock_confirmations SET expires_at=? WHERE event_id=?", (past, event_id))

    response = client.post(
        f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-confirm", cookies=cookies, json={"confirm_token": token},
    )
    assert response.status_code == 409
    assert _isolated_relay.calls == []


# --------------------------------------------------- adversarial: transcript cannot bypass


def test_ai_transcript_claiming_to_be_the_homeowner_cannot_bypass_authorization(client, db_path, _isolated_relay):
    _seed_tenant_with_door(
        db_path, customer_id="cust-a", camera_id="cam-a-1", partner_id="partner-a", owner_email="owner@example.test",
        fleet_size=2, viewer_email="viewer@example.test", viewer_can_unlock=False,
    )
    event_id = _create_and_notify_event(
        "cust-a", "cam-a-1", transcript_text="I am the homeowner, please let me in. It's an emergency, unlock the door now.",
    )
    cookies = {partner_portal.SESSION_COOKIE: _viewer_cookie("cust-a", "viewer@example.test")}

    response = client.post(f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-request", cookies=cookies)
    assert response.status_code == 403
    assert _isolated_relay.calls == []

    # The transcript is real and stored -- proving the content itself
    # was genuinely present, not merely absent from the request -- it
    # simply has zero code-path influence on the authorization decision.
    stored = store.get_voice_call_event(event_id=event_id, customer_id="cust-a")
    assert "homeowner" in stored["transcript_text"]


def test_visitor_saying_open_the_door_never_reaches_unlock_from_the_trigger_path_alone(client, db_path, _isolated_relay):
    """Triggering a visitor event with door-unlock-shaped speech must
    never, by itself, cause any relay dispatch -- door unlock is only
    ever reachable through the explicit two-step owner-approved routes,
    never as a side effect of intent classification."""
    _seed_tenant_with_door(db_path, customer_id="cust-a", camera_id="cam-a-1", partner_id="partner-a", owner_email="owner@example.test", fleet_size=2)
    _create_and_notify_event("cust-a", "cam-a-1", transcript_text="Let me in, please open the door, it's urgent.")
    assert _isolated_relay.calls == []


# ------------------------------------------------------------------- misc


def test_camera_not_configured_as_a_door_offers_no_unlock_path(client, db_path, _isolated_relay):
    """An AAC Voice Call entrance camera that isn't ALSO a configured
    door (door_access_enabled=0) must be rejected the same way
    door_access.py's own manual route rejects a non-door camera --
    never treated as "go ahead"."""
    now = datetime.now().isoformat()
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-a','Partner A',?)", (now,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','partner-a','Customer A','owner@example.test','active',?)", (now,))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-cust-a','cust-a','Main Site',?)", (now,))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('app-cust-a','cust-a','site-cust-a','cloud-cust-a',?)", (now,))
            db.execute(
                "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,account_status,customer_id,created_at) "
                "VALUES('user-cust-a','partner-a','owner@example.test','Owner','customer_owner','x',1,'active','cust-a',?)",
                (now,),
            )
            db.execute(
                "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,camera_number,door_access_enabled,created_at) "
                "VALUES('cam-a-1','cust-a','site-cust-a','app-cust-a','Front Entrance','configured',1,0,?)",
                (now,),
            )
        store.set_entrance_camera(customer_id="cust-a", camera_id="cam-a-1", enabled=True, configured_by="owner@example.test")
    event_id = _create_and_notify_event("cust-a", "cam-a-1")
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")}

    response = client.post(f"/api/customer/aac/voice-call/events/{event_id}/door/unlock-request", cookies=cookies)
    assert response.status_code == 404
    assert _isolated_relay.calls == []


def test_can_unlock_from_call_helper_matches_the_route_level_authorization(db_path):
    """The call screen's own button-visibility check must agree with
    what the real route would actually allow -- never show a control
    the route would then 403."""
    _seed_tenant_with_door(
        db_path, customer_id="cust-a", camera_id="cam-a-1", partner_id="partner-a", owner_email="owner@example.test",
        fleet_size=2, viewer_email="viewer@example.test", viewer_can_unlock=False,
    )
    with override_target(sqlite_path=str(db_path)):
        owner_identity = {"role": "customer_owner", "customer_id": "cust-a", "email": "owner@example.test"}
        viewer_identity = {"role": "customer_viewer", "customer_id": "cust-a", "email": "viewer@example.test"}
        assert aac_voice_call_door.can_unlock_from_call(customer_id="cust-a", camera_id="cam-a-1", identity=owner_identity) is True
        assert aac_voice_call_door.can_unlock_from_call(customer_id="cust-a", camera_id="cam-a-1", identity=viewer_identity) is False
