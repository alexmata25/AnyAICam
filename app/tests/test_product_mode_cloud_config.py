"""Entitlement-driven product mode (2026-09-21): customer_entitlements.
product_mode_for_customer() and its exposure through the edge sync
channel, GET /api/appliance/configuration's `product_mode` field -- see
appliance_cloud.appliance_configuration()'s own comment and product_
mode.py's module docstring for why this makes an "Upgrade Local ->
Hybrid" action genuinely billing-driven: the moment a Hybrid checkout
completes (create_camera_slot_checkout() -> the existing Stripe webhook
-> upsert_entitlement(product="camera_slots_hybrid")), this field starts
reporting "hybrid" for that customer with no separate code path.

Real HTTP throughout for the route-level tests (TestClient(main.app)),
mirroring test_rdm_cloud_policy.py's own established pattern for testing
this same GET /api/appliance/configuration route.
"""
import json
import secrets
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from database_backend import override_target
import product_mode as pm


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_product_mode_cloud_config.db"


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main

        monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
        monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")

        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client


def _seed_tenant(db_path, customer_id, partner_id=None):
    partner_id = partner_id or f"partner-{customer_id}"
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Partner", "2026-01-01"))
        conn.execute(
            "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            (customer_id, partner_id, "Customer", f"{customer_id}@example.test", "active", "2026-01-01"),
        )
        conn.commit()


def _seed_appliance(db_path, customer_id, appliance_id, cloud_id, credential):
    from partner_db import connection, password_hash
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{customer_id}", customer_id, "Site", "2026-01-01"))
            db.execute(
                "INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
                (appliance_id, customer_id, f"site-{customer_id}", cloud_id, "2026-01-01"),
            )
            db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", (f"cred-{appliance_id}", appliance_id, password_hash(credential), "2026-01-01"))


def _appliance_auth_headers(appliance_id, credential):
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


# ------------------------------------------------------ product_mode_for_customer()


def test_no_camera_slot_entitlement_reports_no_mode(client, db_path):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import product_mode_for_customer
        assert product_mode_for_customer("cust-1") == ""


def test_active_local_entitlement_reports_local(client, db_path):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import product_mode_for_customer, upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
        assert product_mode_for_customer("cust-1") == "local"


def test_active_hybrid_entitlement_reports_hybrid(client, db_path):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import product_mode_for_customer, upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8)
        assert product_mode_for_customer("cust-1") == "hybrid"


def test_upgrade_from_local_to_hybrid_reports_hybrid_even_with_the_old_local_row_still_active(client, db_path):
    """The exact moment right after a real Local -> Hybrid upgrade
    checkout completes: upsert_entitlement() is idempotent per
    (customer_id, product), so the original one-time Local purchase and
    the new Hybrid subscription are two independent, coexisting rows --
    "the customer just paid for Hybrid" must take effect immediately,
    with no separate step to first retire the Local row."""
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import product_mode_for_customer, upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
        assert product_mode_for_customer("cust-1") == "local"
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8)
        assert product_mode_for_customer("cust-1") == "hybrid"


def test_a_cancelled_entitlement_is_not_counted(client, db_path):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import product_mode_for_customer, upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8, status="cancelled")
        assert product_mode_for_customer("cust-1") == ""


def test_downgrade_falls_back_to_local_when_a_local_entitlement_is_still_on_file(client, db_path):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import product_mode_for_customer, upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8)
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8, status="cancelled")
        assert product_mode_for_customer("cust-1") == "local"


# --------------------------------------------------- GET /api/appliance/configuration


def test_configuration_route_reports_no_mode_with_no_entitlement(client, db_path):
    _seed_tenant(db_path, "cust-1")
    _seed_appliance(db_path, "cust-1", "appl-1", "AIC-MODE1", "test-credential")
    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert response.status_code == 200
    assert response.json()["product_mode"] == ""


def test_configuration_route_reflects_a_local_entitlement(client, db_path):
    _seed_tenant(db_path, "cust-1")
    _seed_appliance(db_path, "cust-1", "appl-1", "AIC-MODE1", "test-credential")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert response.json()["product_mode"] == "local"


def test_configuration_route_reflects_a_hybrid_upgrade_immediately(client, db_path):
    _seed_tenant(db_path, "cust-1")
    _seed_appliance(db_path, "cust-1", "appl-1", "AIC-MODE1", "test-credential")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert response.json()["product_mode"] == "local"

    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8)
    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert response.json()["product_mode"] == "hybrid"


def test_two_appliances_under_different_customers_see_their_own_mode_only(client, db_path):
    _seed_tenant(db_path, "cust-a", partner_id="partner-a")
    _seed_tenant(db_path, "cust-b", partner_id="partner-b")
    _seed_appliance(db_path, "cust-a", "appl-a", "AIC-MODEA", "cred-a")
    _seed_appliance(db_path, "cust-b", "appl-b", "AIC-MODEB", "cred-b")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-a", product="camera_slots_local", camera_slot_quantity=8)
        upsert_entitlement(customer_id="cust-b", product="camera_slots_hybrid", camera_slot_quantity=8)

    response_a = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-a", "cred-a"))
    response_b = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-b", "cred-b"))
    assert response_a.json()["product_mode"] == "local"
    assert response_b.json()["product_mode"] == "hybrid"


# --------------------------------------- automatic restart_vms on mode change


def _pending_restart_commands(db_path, appliance_id):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM appliance_commands WHERE appliance_id=? AND command='restart_vms'", (appliance_id,)
        ).fetchall()]


def _last_reported_mode(db_path, appliance_id):
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT last_reported_product_mode FROM appliances WHERE id=?", (appliance_id,)).fetchone()
        return row["last_reported_product_mode"]


def test_local_to_hybrid_upgrade_queues_exactly_one_restart_vms_command(client, db_path):
    _seed_tenant(db_path, "cust-1")
    _seed_appliance(db_path, "cust-1", "appl-1", "AIC-RESTART1", "test-credential")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    # First poll establishes "local" as the baseline -- no prior mode to
    # transition FROM yet, and local<->unset changes no flags anyway, so
    # this alone must queue nothing.
    client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert _pending_restart_commands(db_path, "appl-1") == []
    assert _last_reported_mode(db_path, "appl-1") == "local"

    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8)
    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert response.json()["product_mode"] == "hybrid"

    commands = _pending_restart_commands(db_path, "appl-1")
    assert len(commands) == 1
    assert commands[0]["status"] == "pending"
    assert commands[0]["created_by"] == "product-mode-transition"
    payload = json.loads(commands[0]["payload_json"])
    assert payload["confirmed"] is True
    assert "local -> hybrid" in payload["reason"]
    assert _last_reported_mode(db_path, "appl-1") == "hybrid"


def test_hybrid_to_local_downgrade_also_queues_a_restart(client, db_path):
    _seed_tenant(db_path, "cust-1")
    _seed_appliance(db_path, "cust-1", "appl-1", "AIC-RESTART2", "test-credential")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8)
    client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    # The very first real mode (unset -> hybrid) DOES change flags (every
    # governed flag flips from its legacy-false default to true), so this
    # already queues a restart -- confirmed here rather than assumed, then
    # cleared before testing the downgrade in isolation below.
    first_commands = _pending_restart_commands(db_path, "appl-1")
    assert len(first_commands) == 1
    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE appliance_commands SET status='completed' WHERE id=?", (first_commands[0]["id"],))
        conn.commit()

    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8, status="cancelled")
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert response.json()["product_mode"] == "local"

    commands = _pending_restart_commands(db_path, "appl-1")
    assert len(commands) == 2  # the original (now completed) + this new downgrade one
    downgrade_command = next(c for c in commands if c["status"] == "pending")
    payload = json.loads(downgrade_command["payload_json"])
    assert "hybrid -> local" in payload["reason"]
    assert _last_reported_mode(db_path, "appl-1") == "local"


def test_repeated_polls_with_an_unchanged_mode_never_queue_a_second_restart(client, db_path):
    """The core loop-prevention guarantee: any number of poll cycles
    with no real mode change must queue nothing beyond the one restart
    from the original transition."""
    _seed_tenant(db_path, "cust-1")
    _seed_appliance(db_path, "cust-1", "appl-1", "AIC-RESTART3", "test-credential")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8)

    for _ in range(5):
        client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))

    commands = _pending_restart_commands(db_path, "appl-1")
    assert len(commands) == 1


def test_a_rapid_flip_before_the_first_restart_is_consumed_does_not_double_queue(client, db_path):
    """Loop prevention's second layer: even if the mode flips again
    while the first restart_vms command is still pending/delivered
    (not yet completed by the appliance), the existing-pending dedup
    check must refuse to queue a second one."""
    _seed_tenant(db_path, "cust-1")
    _seed_appliance(db_path, "cust-1", "appl-1", "AIC-RESTART4", "test-credential")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))

    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8)
    client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert len(_pending_restart_commands(db_path, "appl-1")) == 1  # still pending/undelivered

    # Flip back to local before the appliance has consumed the first command.
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8, status="cancelled")
    client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))

    commands = _pending_restart_commands(db_path, "appl-1")
    assert len(commands) == 1, "the still-pending restart_vms command must not be duplicated"


def test_first_ever_local_purchase_queues_no_restart(client, db_path):
    """"" -> "local" changes no governed flag by default (both resolve
    every flag to False), so a customer's very first Local purchase --
    before this appliance has ever reported a real mode -- must queue
    nothing. There is nothing running yet for a restart to change."""
    _seed_tenant(db_path, "cust-1")
    _seed_appliance(db_path, "cust-1", "appl-1", "AIC-RESTART5", "test-credential")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert _pending_restart_commands(db_path, "appl-1") == []
    assert _last_reported_mode(db_path, "appl-1") == "local"


def test_a_lapsed_entitlement_never_clears_or_restarts(client, db_path):
    """A cancelled Hybrid subscription with no fallback Local
    entitlement reports product_mode="" (see product_mode_for_customer()
    -- "Decision B", never guessed) -- this must never clear
    last_reported_product_mode or queue a restart of its own; the
    appliance keeps running its last real mode until a real mode is
    reported again."""
    _seed_tenant(db_path, "cust-1")
    _seed_appliance(db_path, "cust-1", "appl-1", "AIC-RESTART6", "test-credential")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8)
    client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert len(_pending_restart_commands(db_path, "appl-1")) == 1

    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8, status="cancelled")
    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert response.json()["product_mode"] == ""
    assert _last_reported_mode(db_path, "appl-1") == "hybrid"  # untouched, not cleared
    assert len(_pending_restart_commands(db_path, "appl-1")) == 1  # no new/second command queued


def test_product_mode_changed_audit_entry_is_recorded(client, db_path):
    _seed_tenant(db_path, "cust-1")
    _seed_appliance(db_path, "cust-1", "appl-1", "AIC-RESTART7", "test-credential")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8)
    client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "test-credential"))

    with override_target(sqlite_path=str(db_path)):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        entry = conn.execute(
            "SELECT * FROM audit_logs WHERE action='appliance.product_mode_changed' AND entity_id='appl-1'"
        ).fetchone()
    assert entry is not None
    details = json.loads(entry["details_json"])
    assert details["old_mode"] == "unset"
    assert details["new_mode"] == "hybrid"
    assert set(details["changed_flags"]) == set(pm.FLAG_REGISTRY.keys())
    assert "restart_command_id" in details


# ------------------------------------------- one customer, multiple appliances


def test_two_appliances_under_the_same_customer_each_get_their_own_independent_restart(client, db_path):
    """product_mode_for_customer() is customer-scoped (both appliances
    resolve the same "hybrid"), but last_reported_product_mode and the
    restart_vms dedup/queue are tracked per-APPLIANCE -- a customer with
    two Ryzen units must get two independent restarts, one per box, not
    a single shared one and not a second appliance silently skipped
    because the first already "used up" the transition."""
    _seed_tenant(db_path, "cust-1")
    _seed_appliance(db_path, "cust-1", "appl-a", "AIC-MULTI-A", "cred-a")
    _seed_appliance(db_path, "cust-1", "appl-b", "AIC-MULTI-B", "cred-b")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8)

    response_a = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-a", "cred-a"))
    response_b = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-b", "cred-b"))
    assert response_a.json()["product_mode"] == "hybrid"
    assert response_b.json()["product_mode"] == "hybrid"

    commands_a = _pending_restart_commands(db_path, "appl-a")
    commands_b = _pending_restart_commands(db_path, "appl-b")
    assert len(commands_a) == 1
    assert len(commands_b) == 1
    assert commands_a[0]["id"] != commands_b[0]["id"]
    assert _last_reported_mode(db_path, "appl-a") == "hybrid"
    assert _last_reported_mode(db_path, "appl-b") == "hybrid"

    # A repeat poll on EITHER appliance alone must not queue a second
    # restart for the OTHER one that hasn't polled again yet.
    client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-a", "cred-a"))
    assert len(_pending_restart_commands(db_path, "appl-a")) == 1
    assert len(_pending_restart_commands(db_path, "appl-b")) == 1


# ------------------------------------------------- non-active entitlement statuses


def test_a_past_due_entitlement_is_not_counted_as_active(client, db_path):
    """Only status=='active' counts -- any other real Stripe status
    (past_due, incomplete, trialing, etc.) must be treated the same as
    cancelled: not active, never guessed into a mode."""
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import product_mode_for_customer, upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8, status="past_due")
        assert product_mode_for_customer("cust-1") == ""


def test_a_trialing_entitlement_is_not_counted_as_active(client, db_path):
    _seed_tenant(db_path, "cust-1")
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import product_mode_for_customer, upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8, status="trialing")
        assert product_mode_for_customer("cust-1") == ""
