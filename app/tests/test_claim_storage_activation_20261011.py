"""Claim-flow activation starts the deferred storage plan (2026-10-11).

An appliance linked through a claim code stayed activation_status='pending',
so build_orders.py never started the storage plan its customer paid for with
the appliance. Now:

* claim/complete marks the row activated_via='claim'; the first heartbeat
  after it really enrolled (appliance_readiness.enrollment_ready) records
  activated_at once (audited) and starts the plan through the same idempotent,
  off-session path token activation uses. activation_status keeps its meaning.
* appliances claimed BEFORE this change are never auto-charged: the one-time
  migration holds their pending start (state 'owner_review', audited), and
  nothing -- heartbeat, billing worker, restart, a later claim -- starts it.
  Only the platform owner/admin's explicit, audited "start storage now" does.
* readiness requires a live (not revoked/released) appliance with a
  non-revoked credential, token-activated or claimed (Codex, 9b892ab).
* an admin-only report lists doors still enabled on accounts without Face
  Access (they keep working under 9b892ab); nothing changes them.
"""
import secrets
import sqlite3
import time
from datetime import datetime, timedelta

import pytest

from database_backend import override_target
from test_build_order_billing import (APPLIANCE, _Lost, _Refused, _paid, _start, _storage, _worker,  # noqa: F401
                                      stripe_subscriptions)
from test_hardware_delivery_flow import _hardware_order
from test_hardware_fulfillment_admin import _admin_cookie
from test_hybrid_build_system_flow import (_activate_appliance, _customer_id, _new_customer,  # noqa: F401
                                           db_path, license_portal, package, portal, shop, site, storage)

pytestmark = pytest.mark.usefixtures("stripe_follows_events")


def _conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert(conn, table, values):
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    values = {key: value for key, value in values.items() if key in columns}
    conn.execute(f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' * len(values))})", tuple(values.values()))


def _claimed_appliance(db_path, customer_id, *, appliance_id="app-claim", marked=True, enrolled=False):
    """A row as claim/complete creates it (completed claim + credential).
    marked=False is an appliance claimed before claim-flow activation shipped
    (no activated_via); enrolled=True means it already used its credential
    and checked in."""
    from partner_db import password_hash
    credential = secrets.token_urlsafe(24)
    issued = (datetime.now() - timedelta(minutes=5)).isoformat()
    conn = _conn(db_path)
    site_id = conn.execute("SELECT id FROM sites WHERE customer_id=?", (customer_id,)).fetchone()[0]
    _insert(conn, "appliances", {"id": appliance_id, "customer_id": customer_id, "site_id": site_id,
                                 "cloud_id": f"CLOUD-{appliance_id}".upper(), "created_at": issued,
                                 "activated_via": "claim" if marked else None,
                                 "last_check_in": datetime.now().isoformat() if enrolled else None})
    _insert(conn, "appliance_claims", {"id": f"claim-{appliance_id}", "device_id": f"dev-{appliance_id}",
                                       "claim_session_id": f"sess-{appliance_id}", "claim_code_hash": "x", "status": "completed",
                                       "customer_id": customer_id, "site_id": site_id, "appliance_id": appliance_id,
                                       "expires_at": issued, "completed_at": issued, "created_at": issued})
    _insert(conn, "appliance_credentials", {"id": f"cred-{appliance_id}", "appliance_id": appliance_id,
                                            "credential_hash": password_hash(credential), "created_at": issued,
                                            "created_by": "claim",
                                            "last_used_at": datetime.now().isoformat() if enrolled else None})
    conn.commit()
    conn.close()
    return credential


def _heartbeat(client, appliance_id, credential, *, wait=True):
    """An authenticated heartbeat; waits for the background storage start
    it may have scheduled (off the request path)."""
    import appliance_claims
    import appliance_cloud
    appliance_cloud.request_limiter.events.clear()
    response = client.post("/api/appliance/heartbeat", json={"uptime_seconds": 120, "cpu": 1, "memory": 1},
                           headers={"X-Appliance-Id": appliance_id, "X-Request-Timestamp": str(int(time.time())),
                                    "X-Request-Nonce": secrets.token_hex(16), "Authorization": f"Bearer {credential}"})
    if wait:
        appliance_claims.wait_for_background_storage_starts()
    return response


def _deploy_migration(db_path):
    """What deploying this change does to an existing database: the one-time
    hold runs (its marker is absent before the deploy)."""
    import db_migrations
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM schema_migrations WHERE version=?", (db_migrations.HOLD_CLAIMED_STORAGE_STARTS,))
    conn.commit()
    conn.close()
    _restart(db_path)


def _restart(db_path):
    """A process (web or billing worker) restart: migrations run again."""
    import db_migrations
    with override_target(sqlite_path=str(db_path)):
        db_migrations.apply_migrations()


def _audits(db_path, action, entity_id=None):
    conn = _conn(db_path)
    found = conn.execute("SELECT * FROM audit_logs WHERE action=?" + (" AND entity_id=?" if entity_id else ""),
                         (action, *((entity_id,) if entity_id else ()))).fetchall()
    conn.close()
    return [dict(item) for item in found]


def _appliance(db_path, appliance_id):
    conn = _conn(db_path)
    found = dict(conn.execute("SELECT * FROM appliances WHERE id=?", (appliance_id,)).fetchone())
    conn.close()
    return found


# ------------------------------------------------------------ new claims start the plan

def test_a_new_claim_starts_the_storage_plan_once_when_it_really_enrolls(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "claim-new@example.test", APPLIANCE, captured)
    credential = _claimed_appliance(db_path, customer_id)
    # claim/complete alone (no enrollment yet): nothing is charged.
    assert _worker(db_path) == 0 and stripe_subscriptions["created"] == []
    assert _start(db_path, customer_id)["reason"] == "appliance not activated"

    assert _heartbeat(client, "app-claim", credential).status_code == 200
    storage = _storage(db_path, customer_id)
    assert storage["state"] == "started" and storage["stripe_subscription_id"] == "sub_storage_1"
    assert len(stripe_subscriptions["created"]) == 1
    fields = dict(stripe_subscriptions["created"][0])
    assert fields["off_session"] == "true" and fields["payment_behavior"] == "error_if_incomplete"
    assert fields["default_payment_method"] == "pm_saved_card" and fields["items[0][quantity]"] == "8"
    appliance = _appliance(db_path, "app-claim")
    assert appliance["activated_at"] and appliance["activated_via"] == "claim"
    assert appliance["activation_status"] == "pending"  # its meaning is unchanged (token activation)
    activated = _audits(db_path, "appliance.activated", "app-claim")
    assert len(activated) == 1 and '"via": "claim"' in activated[0]["details_json"]

    # Further heartbeats, worker passes and duplicate events never charge again.
    assert _heartbeat(client, "app-claim", credential).status_code == 200
    assert _worker(db_path) == 0
    import build_orders
    with override_target(sqlite_path=str(db_path)):
        assert build_orders.on_appliance_activated(customer_id)["status"] == "ignored"
    assert len(stripe_subscriptions["created"]) == 1 and len(_audits(db_path, "appliance.activated", "app-claim")) == 1


def test_a_slow_stripe_call_never_delays_the_heartbeat(shop, db_path, stripe_subscriptions, monkeypatch):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "claim-slow@example.test", APPLIANCE, captured)
    credential = _claimed_appliance(db_path, customer_id)
    import threading

    import appliance_claims
    import main
    release, reached = threading.Event(), threading.Event()
    stripe_post = main.stripe_api_post

    def slow_post(path, fields, idempotency_key=None):
        reached.set()
        assert release.wait(20), "Stripe was never released"
        return stripe_post(path, fields, idempotency_key=idempotency_key)
    monkeypatch.setattr(main, "stripe_api_post", slow_post)
    started = time.monotonic()
    response = _heartbeat(client, "app-claim", credential, wait=False)
    elapsed = time.monotonic() - started
    assert response.status_code == 200 and elapsed < 5
    assert reached.wait(10)  # the start runs in the background, after the answer
    assert _appliance(db_path, "app-claim")["activated_at"]  # the activation was recorded in the request
    assert _storage(db_path, customer_id)["state"] == "starting"
    assert _worker(db_path) == 0  # a start in progress is never started twice
    release.set()
    appliance_claims.wait_for_background_storage_starts()
    assert _storage(db_path, customer_id)["state"] == "started" and len(stripe_subscriptions["created"]) == 1


def test_a_lost_stripe_answer_on_a_claim_start_is_retried_by_the_worker(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "claim-lost@example.test", APPLIANCE, captured)
    credential = _claimed_appliance(db_path, customer_id)
    stripe_subscriptions["fail"].append(_Lost("timeout"))
    assert _heartbeat(client, "app-claim", credential).status_code == 200  # never fails the heartbeat
    assert _storage(db_path, customer_id)["state"] == "failed"
    assert _worker(db_path) == 1 and _worker(db_path) == 0
    assert len(stripe_subscriptions["made"]) == 1 and _storage(db_path, customer_id)["state"] == "started"


def test_a_declined_card_on_a_claim_start_waits_for_the_customer(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "claim-declined@example.test", APPLIANCE, captured)
    credential = _claimed_appliance(db_path, customer_id)
    stripe_subscriptions["fail"].append(_Refused("card_declined"))
    assert _heartbeat(client, "app-claim", credential).status_code == 200
    assert _storage(db_path, customer_id)["state"] == "payment_failed"
    assert _worker(db_path) == 0 and stripe_subscriptions["created"] == []
    retry = client.post("/api/v2/customer/storage/start")
    assert retry.status_code == 200 and len(stripe_subscriptions["created"]) == 1


def test_a_missed_heartbeat_record_is_picked_up_by_the_worker_sweep(shop, db_path, stripe_subscriptions, monkeypatch):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "claim-sweep@example.test", APPLIANCE, captured)
    credential = _claimed_appliance(db_path, customer_id)
    import appliance_claims
    real = appliance_claims.record_claim_activation
    monkeypatch.setattr(appliance_claims, "record_claim_activation", lambda _id: (_ for _ in ()).throw(RuntimeError("db busy")))
    assert _heartbeat(client, "app-claim", credential).status_code == 200
    assert _storage(db_path, customer_id)["state"] == "awaiting_activation" and stripe_subscriptions["created"] == []
    monkeypatch.setattr(appliance_claims, "record_claim_activation", real)
    _worker(db_path)  # its sweep records the activation, which starts the plan
    assert _storage(db_path, customer_id)["state"] == "started" and len(stripe_subscriptions["created"]) == 1
    assert _appliance(db_path, "app-claim")["activated_at"]
    _worker(db_path)
    assert len(stripe_subscriptions["created"]) == 1


def test_a_revoked_or_released_claim_never_activates(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "claim-revoked@example.test", APPLIANCE, captured)
    _claimed_appliance(db_path, customer_id, enrolled=True)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE appliances SET activation_status='released' WHERE id='app-claim'")
    conn.commit()
    conn.close()
    assert _worker(db_path) == 0 and stripe_subscriptions["created"] == []
    assert not _appliance(db_path, "app-claim")["activated_at"]


def test_token_activation_still_starts_the_plan_and_is_recorded(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "token@example.test", APPLIANCE, captured)
    from partner_db import password_hash
    conn = _conn(db_path)
    site_id = conn.execute("SELECT id FROM sites WHERE customer_id=?", (customer_id,)).fetchone()[0]
    _insert(conn, "appliances", {"id": "app-token", "customer_id": customer_id, "site_id": site_id, "cloud_id": "CLOUD-TOKEN",
                                 "created_at": "2026-10-05"})
    _insert(conn, "appliance_activation_tokens", {"id": "tok-1", "appliance_id": "app-token", "token_hash": password_hash("tok-secret"),
                                                  "expires_at": (datetime.now() + timedelta(hours=1)).isoformat(),
                                                  "created_at": "2026-10-05", "created_by": "ops"})
    conn.commit()
    conn.close()
    import appliance_activation
    import appliance_cloud
    appliance_cloud.activation_limiter.events.clear()
    original = appliance_activation.local_activation_tracking_applies
    appliance_activation.local_activation_tracking_applies = lambda: False
    try:
        response = client.post("/api/appliance/activate", json={"cloud_id": "CLOUD-TOKEN", "activation_token": "tok-secret"})
    finally:
        appliance_activation.local_activation_tracking_applies = original
    assert response.status_code == 200
    appliance = _appliance(db_path, "app-token")
    assert appliance["activation_status"] == "activated" and appliance["activated_via"] == "token" and appliance["activated_at"]
    assert _storage(db_path, customer_id)["state"] == "started" and len(stripe_subscriptions["created"]) == 1


# ------------------------------------------------------------ existing claimed customers are never auto-charged

def test_existing_claimed_customers_are_held_for_the_owner_and_never_auto_charged(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "claim-old@example.test", APPLIANCE, captured)
    credential = _claimed_appliance(db_path, customer_id, marked=False, enrolled=True)  # claimed before this change
    _deploy_migration(db_path)
    storage = _storage(db_path, customer_id)
    assert storage["state"] == "owner_review" and storage["hold_reason"] == "claimed_before_claim_activation"
    held = _audits(db_path, "storage_start.held_for_owner_review", customer_id)
    assert len(held) == 1 and held[0]["actor_email"] == "system:migration"

    # Heartbeats, the worker sweep, activation events and restarts never start it.
    assert _heartbeat(client, "app-claim", credential).status_code == 200
    assert _worker(db_path) == 0
    import build_orders
    with override_target(sqlite_path=str(db_path)):
        assert build_orders.on_appliance_activated(customer_id)["status"] == "ignored"
        assert build_orders.start_storage_plan(customer_id)["reason"] == "held for an owner decision"
    _restart(db_path)
    assert _worker(db_path) == 0
    # Not even a NEW claim on the same account, or the customer's own retry.
    second = _claimed_appliance(db_path, customer_id, appliance_id="app-claim-2")
    assert _heartbeat(client, "app-claim-2", second).status_code == 200
    assert _worker(db_path) == 0
    assert client.post("/api/v2/customer/storage/start").status_code == 409
    assert stripe_subscriptions["created"] == [] and stripe_subscriptions["keys"] == []
    assert _storage(db_path, customer_id)["state"] == "owner_review"
    assert not _appliance(db_path, "app-claim")["activated_at"]  # an unmarked (pre-change) claim is never activated


def test_a_restart_never_holds_a_new_claim_and_token_accounts_are_left_alone(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    new_customer = _paid(client, mail, db_path, "claim-fresh@example.test", APPLIANCE, captured)
    credential = _claimed_appliance(db_path, new_customer)  # claimed after the deploy, not yet enrolled
    token_customer = _paid(client, mail, db_path, "token-failed@example.test", APPLIANCE, captured)
    _activate_appliance(db_path, token_customer)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE deferred_storage_plans SET state='failed' WHERE customer_id=?", (token_customer,))
    conn.commit()
    conn.close()
    _restart(db_path)  # the marker is present: the hold never runs again
    assert _storage(db_path, new_customer)["state"] == "awaiting_activation"
    _deploy_migration(db_path)  # even the one-time hold leaves token-activated accounts as they were
    assert _storage(db_path, token_customer)["state"] == "failed"
    # (the hold would catch the not-yet-enrolled new claim only if it ran
    # again; the marker guarantees it runs once, at the deploy)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE deferred_storage_plans SET state='awaiting_activation',hold_reason=NULL,held_at=NULL WHERE customer_id=?",
                 (new_customer,))
    conn.commit()
    conn.close()
    assert _heartbeat(client, "app-claim", credential).status_code == 200
    assert _storage(db_path, new_customer)["state"] == "started"


# ------------------------------------------------------------ the owner/admin decision

def test_only_platform_administrators_see_and_start_held_storage(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "claim-auth@example.test", APPLIANCE, captured)
    _claimed_appliance(db_path, customer_id, marked=False, enrolled=True)
    _deploy_migration(db_path)
    owner_cookies = dict(client.cookies)
    partner_admin = _admin_cookie(db_path, global_grant=False, email="partner-admin@example.test")
    for cookies in (owner_cookies, partner_admin):
        assert client.get("/api/admin/storage-starts", cookies=cookies).status_code == 403
        assert client.get("/admin/storage-starts", cookies=cookies).status_code == 403
        assert client.post(f"/api/admin/storage-starts/{customer_id}/start", json={"confirm": True},
                           cookies=cookies).status_code == 403
    client.cookies.clear()
    assert client.post(f"/api/admin/storage-starts/{customer_id}/start", json={"confirm": True}).status_code in (401, 403)
    assert stripe_subscriptions["created"] == [] and _storage(db_path, customer_id)["state"] == "owner_review"

    admin = _admin_cookie(db_path)
    listing = client.get("/api/admin/storage-starts", cookies=admin).json()["storage_starts"]
    assert [item["customer_id"] for item in listing] == [customer_id]
    assert listing[0]["appliance_enrolled"] is True and listing[0]["hardware_still_paid"] is True
    assert listing[0]["appliances"][0]["claimed_at"]
    page = client.get("/admin/storage-starts", cookies=admin)
    assert page.status_code == 200 and "claim-auth@example.test" in page.text and "Start storage now" in page.text
    # An explicit confirmation is required.
    assert client.post(f"/api/admin/storage-starts/{customer_id}/start", json={}, cookies=admin).status_code == 422
    assert client.post(f"/api/admin/storage-starts/{customer_id}/start", json={"confirm": False}, cookies=admin).status_code == 400
    assert stripe_subscriptions["created"] == []


def test_the_admin_start_is_idempotent_and_audited(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "claim-start@example.test", APPLIANCE, captured)
    _claimed_appliance(db_path, customer_id, marked=False, enrolled=True)
    _deploy_migration(db_path)
    admin = _admin_cookie(db_path)
    first = client.post(f"/api/admin/storage-starts/{customer_id}/start", json={"confirm": True}, cookies=admin)
    assert first.status_code == 200 and first.json()["status"] == "started" and first.json()["subscription_id"] == "sub_storage_1"
    again = client.post(f"/api/admin/storage-starts/{customer_id}/start", json={"confirm": True}, cookies=admin)
    assert again.status_code == 200 and again.json()["status"] == "unchanged"
    assert len(stripe_subscriptions["created"]) == 1 and len(set(stripe_subscriptions["keys"])) == 1
    fields = dict(stripe_subscriptions["created"][0])
    assert fields["off_session"] == "true" and fields["metadata[anyaicam_customer_id]"] == customer_id
    storage = _storage(db_path, customer_id)
    assert storage["state"] == "started" and storage["owner_approved_by"] == "ops@example.test" and storage["owner_approved_at"]
    approved = _audits(db_path, "storage_start.owner_approved", customer_id)
    assert len(approved) == 1 and approved[0]["actor_email"] == "ops@example.test"
    assert len(_audits(db_path, "storage_start.admin_start", customer_id)) == 1
    assert client.get("/api/admin/storage-starts", cookies=admin).json()["storage_starts"] == []


def test_a_transient_stripe_error_after_approval_is_retried_by_the_worker(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "claim-retry@example.test", APPLIANCE, captured)
    _claimed_appliance(db_path, customer_id, marked=False, enrolled=True)
    _deploy_migration(db_path)
    stripe_subscriptions["fail"].append(_Lost("timeout"))
    admin = _admin_cookie(db_path)
    first = client.post(f"/api/admin/storage-starts/{customer_id}/start", json={"confirm": True}, cookies=admin)
    assert first.status_code == 200 and first.json()["status"] == "failed"
    assert _worker(db_path) == 1 and _worker(db_path) == 0
    assert len(stripe_subscriptions["made"]) == 1 and _storage(db_path, customer_id)["state"] == "started"


def test_the_admin_start_refuses_without_an_enrolled_appliance_or_a_paid_order(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "claim-notready@example.test", APPLIANCE, captured)
    _claimed_appliance(db_path, customer_id, marked=False, enrolled=False)
    _deploy_migration(db_path)
    admin = _admin_cookie(db_path)
    refused = client.post(f"/api/admin/storage-starts/{customer_id}/start", json={"confirm": True}, cookies=admin)
    assert refused.status_code == 409 and "enrolling" in refused.json()["detail"]
    _hardware_order(db_path, customer_id, status="refunded")
    refused = client.post(f"/api/admin/storage-starts/{customer_id}/start", json={"confirm": True}, cookies=admin)
    assert refused.status_code == 409 and stripe_subscriptions["created"] == []
    assert _storage(db_path, customer_id)["state"] == "owner_review" and not _audits(db_path, "storage_start.owner_approved")
    assert client.post("/api/admin/storage-starts/no-such-customer/start", json={"confirm": True}, cookies=admin).status_code == 404


# ------------------------------------------------------------ fulfillment shows claim activation

def test_fulfillment_shows_claim_activated_appliances(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "claim-ship@example.test", APPLIANCE, captured)
    _hardware_order(db_path, customer_id, fulfillment="delivered")
    credential = _claimed_appliance(db_path, customer_id)
    admin = _admin_cookie(db_path)

    def pill():
        html = client.get("/admin/hardware-orders", cookies=admin).text
        return html.split('<span class="pill">', 1)[1].split("</span>", 1)[0]
    assert pill() == "delivered"
    assert _heartbeat(client, "app-claim", credential).status_code == 200
    assert pill() == "activated"


def test_fulfillment_flags_a_held_storage_start(shop, db_path, stripe_subscriptions):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "claim-flag@example.test", APPLIANCE, captured)
    _hardware_order(db_path, customer_id, fulfillment="delivered")
    _claimed_appliance(db_path, customer_id, marked=False, enrolled=True)
    _deploy_migration(db_path)
    html = client.get("/admin/hardware-orders", cookies=_admin_cookie(db_path)).text
    assert '<span class="pill">activated</span>' in html and "Storage plan held for owner decision" in html
    assert stripe_subscriptions["created"] == []


# ------------------------------------------------------------ readiness (Codex blocker 1 on 9b892ab)

def _ready(db_path, customer_id):
    import appliance_readiness
    with override_target(sqlite_path=str(db_path)):
        return appliance_readiness.customer_has_activated_appliance(customer_id)


def test_a_revoked_token_activated_appliance_is_not_ready(shop, db_path):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "revoked@example.test", APPLIANCE, captured)
    _activate_appliance(db_path, customer_id)  # with its activation credential
    conn = sqlite3.connect(db_path)
    assert _ready(db_path, customer_id) is True
    # As appliance_cloud's revoke does: credentials revoked, state='revoked',
    # activation_status left 'activated'.
    conn.execute("UPDATE appliance_credentials SET revoked_at='2026-10-11' WHERE appliance_id='app-build'")
    conn.execute("UPDATE appliances SET state='revoked',online_status='revoked' WHERE id='app-build'")
    conn.commit()
    assert _ready(db_path, customer_id) is False
    import appliance_readiness
    with override_target(sqlite_path=str(db_path)):
        assert appliance_readiness.appliance_activated(
            {"id": "app-build", "activation_status": "activated", "state": "revoked"}) is False
        # Credential revoked but state not (yet) updated: still not ready.
        conn.execute("UPDATE appliances SET state='online' WHERE id='app-build'")
        conn.commit()
        assert appliance_readiness.appliance_activated({"id": "app-build", "activation_status": "activated"}) is False
    assert _ready(db_path, customer_id) is False
    conn.close()


def test_another_customers_appliance_never_counts(shop, db_path):
    client, captured, mail, _, _ = shop
    mine = _paid(client, mail, db_path, "mine@example.test", APPLIANCE, captured)
    client.cookies.clear()
    theirs = _paid(client, mail, db_path, "theirs@example.test", APPLIANCE, captured)
    _claimed_appliance(db_path, theirs, enrolled=True)
    assert _ready(db_path, theirs) is True and _ready(db_path, mine) is False


def test_a_released_claimed_appliance_is_not_ready(shop, db_path):
    client, captured, mail, _, _ = shop
    customer_id = _paid(client, mail, db_path, "released@example.test", APPLIANCE, captured)
    _claimed_appliance(db_path, customer_id, enrolled=True)
    assert _ready(db_path, customer_id) is True
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE appliances SET activation_status='released' WHERE id='app-claim'")
    conn.commit()
    conn.close()
    assert _ready(db_path, customer_id) is False


# ------------------------------------------------------------ door review (read-only, admin only)

def _camera(db_path, customer_id, camera_id, **door):
    conn = _conn(db_path)
    site_id = conn.execute("SELECT id FROM sites WHERE customer_id=?", (customer_id,)).fetchone()[0]
    _insert(conn, "cameras", {"id": camera_id, "customer_id": customer_id, "site_id": site_id, "name": camera_id,
                              "created_at": "2026-10-01", **door})
    conn.commit()
    conn.close()


def test_the_door_report_lists_only_enabled_doors_without_face_access_and_is_admin_only(shop, db_path):
    client, captured, mail, _, _ = shop
    plain = _paid(client, mail, db_path, "doors-plain@example.test", APPLIANCE, captured)
    owner_cookies = dict(client.cookies)
    client.cookies.clear()
    entitled = _paid(client, mail, db_path, "doors-fa@example.test", APPLIANCE, captured)
    client.cookies.clear()
    _camera(db_path, plain, "cam-old-door", door_access_enabled=1, door_relay_channel=1)
    _camera(db_path, plain, "cam-requested", door_access_enabled=0, door_access_requested=1, door_relay_channel=2)
    _camera(db_path, plain, "cam-not-a-door", door_access_enabled=0)
    _camera(db_path, entitled, "cam-fa-door", door_access_enabled=1, door_relay_channel=1)
    import analytics_entitlements
    with override_target(sqlite_path=str(db_path)):
        analytics_entitlements.upsert_analytics_subscription(customer_id=entitled, analytic_key="facial_recognition", status="active")

    partner_admin = _admin_cookie(db_path, global_grant=False, email="partner-admin@example.test")
    for cookies in (owner_cookies, partner_admin, {}):
        assert client.get("/api/admin/door-access-review", cookies=cookies).status_code in (401, 403)
        assert client.get("/admin/door-access-review", cookies=cookies).status_code in (303, 401, 403)
    admin = _admin_cookie(db_path)
    doors = client.get("/api/admin/door-access-review", cookies=admin).json()["doors"]
    assert [(door["customer_id"], door["camera_id"]) for door in doors] == [(plain, "cam-old-door")]
    page = client.get("/admin/door-access-review", cookies=admin)
    assert page.status_code == 200 and "cam-old-door" in page.text and "cam-fa-door" not in page.text
    # Read-only: nothing changed.
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT door_access_enabled FROM cameras WHERE id='cam-old-door'").fetchone()[0] == 1
    conn.close()
