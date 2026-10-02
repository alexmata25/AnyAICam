"""Provisions a wholly synthetic, clearly-labeled appliance identity on
staging for verifying the WireGuard enrollment route (wireguard_remote.py)
and the gateway's own reconcile loop end to end over the real network --
deliberately never the real pilot customer's own appliance, matching this
project's established disposable-synthetic-tenant convention for any
staging validation that writes data (see
e2e/scripts/provision_face_access_test_harness.py for the precedent this
file follows).

This harness seeds only what appliance_cloud.authenticate_appliance()
itself requires to accept a real, correctly-signed request: a customer,
a site, an appliance row, and one appliance_credentials row with a known
plaintext credential (printed once, to stdout only). It deliberately does
NOT seed a WireGuard peer row itself -- that row is created for real via
a genuine HTTP POST to /api/appliance/wireguard/enroll during
verification, so the verification actually exercises the real route, not
a pre-seeded shortcut.

Run via: docker cp this file into the running portal container, then
`docker exec <container> python3 provision_wireguard_test_harness.py`
(add `--teardown` to remove everything this script created, verified
against exact row counts -- never a blanket DELETE). Prints the generated
appliance credential once, to stdout only.
"""
import secrets
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app")

from partner_db import connection, password_hash

PARTNER_ID = "anyaicam-primary"
CUSTOMER_ID = "e2ewgtest"
SITE_ID = "e2ewgtestsite"
APPLIANCE_ID = "e2ewgtestappliance"
CLOUD_ID = "AIC-E2EWGTEST"
CUSTOMER_EMAIL = "e2e-wireguard-test@anyaicam-test.invalid"


def provision() -> None:
    now = datetime.now(timezone.utc).isoformat()
    with connection() as db:
        existing = db.execute("SELECT id FROM appliances WHERE id=?", (APPLIANCE_ID,)).fetchone()
        if existing:
            print("already provisioned -- no changes made. Run with --teardown first to reseed.")
            return

        credential = secrets.token_urlsafe(32)
        credential_hash = password_hash(credential)

        partner = db.execute("SELECT id FROM partners WHERE id=?", (PARTNER_ID,)).fetchone()
        if not partner:
            db.execute(
                "INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)",
                (PARTNER_ID, "AnyAiCam Primary", "approved", "real", now),
            )

        db.execute(
            "INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
            (CUSTOMER_ID, PARTNER_ID, "E2E WireGuard Test Customer", CUSTOMER_EMAIL, "active", now),
        )
        db.execute(
            "INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)",
            (SITE_ID, CUSTOMER_ID, "E2E WireGuard Test Site", now),
        )
        db.execute(
            "INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,state,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (APPLIANCE_ID, CUSTOMER_ID, SITE_ID, CLOUD_ID, "activated", "online", now),
        )
        db.execute(
            "INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at,created_by) "
            "VALUES(?,?,?,?,?)",
            ("e2ewgtestcred", APPLIANCE_ID, credential_hash, now, "e2e-harness"),
        )

    print("Provisioned synthetic WireGuard test appliance.")
    print(f"  appliance_id: {APPLIANCE_ID}")
    print(f"  cloud_id: {CLOUD_ID}")
    print(f"  credential (X-Appliance-Id={APPLIANCE_ID}, Bearer token): {credential}")


def teardown() -> None:
    with connection() as db:
        peer_count = db.execute(
            "SELECT COUNT(*) c FROM appliance_wireguard_peers WHERE appliance_id=?", (APPLIANCE_ID,)
        ).fetchone()["c"]
        db.execute("DELETE FROM appliance_wireguard_peers WHERE appliance_id=?", (APPLIANCE_ID,))
        db.execute("DELETE FROM appliance_health_history WHERE appliance_id=?", (APPLIANCE_ID,))
        db.execute("DELETE FROM appliance_request_nonces WHERE appliance_id=?", (APPLIANCE_ID,))
        db.execute("DELETE FROM appliance_credentials WHERE appliance_id=?", (APPLIANCE_ID,))
        appliance_deleted = db.execute("DELETE FROM appliances WHERE id=?", (APPLIANCE_ID,)).rowcount
        site_deleted = db.execute("DELETE FROM sites WHERE id=?", (SITE_ID,)).rowcount
        customer_deleted = db.execute("DELETE FROM customers WHERE id=?", (CUSTOMER_ID,)).rowcount
    print(
        f"Teardown complete: {peer_count} wireguard peer row(s), "
        f"{appliance_deleted} appliance, {site_deleted} site, {customer_deleted} customer row(s) removed. "
        "Shared partner row (anyaicam-primary) left in place -- not created solely for this harness."
    )


if __name__ == "__main__":
    if "--teardown" in sys.argv:
        teardown()
    else:
        provision()
