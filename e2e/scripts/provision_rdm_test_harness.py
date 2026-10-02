"""Provisions a wholly synthetic, clearly-labeled test customer/site/camera
on staging for exercising the RDM (per-camera analytics entitlement)
control path independently of the real pilot customer's own billing/
subscription data.

Deliberately NOT attached to the real Ryzen appliance (appliance_id is
left NULL) -- Ryzen's own /api/appliance/configuration scopes cameras by
appliance_id, so attaching a synthetic camera to the real appliance risks
it being treated as a real, discoverable camera by the real, live
appliance. This harness is scoped to what's actually safely testable
without a second physical/virtual appliance: the cloud-side entitlement
path (assign/remove, the customer-facing API, the Live View analytics
pill's real vs. upgrade-card rendering) -- exactly the "desired" half of
RDM's own documented "desired-to-actual convergence." The "actual"
(appliance-runtime) half is already proven absent by an exhaustive
source-code search (see PROJECT_CHECKPOINT.md's own 2026-09-16 RDM
entry) and does not need live camera footage to further confirm.

Run once (idempotent via INSERT OR IGNORE / same fixed ids) via:
  docker cp this file into portal-green, then docker exec python3 it.
"""
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, "/app")

from partner_db import connection, password_hash

CUSTOMER_ID = "rdm-test-harness-customer"
SITE_ID = "rdm-test-harness-site"
CAMERA_ID = "rdm-test-harness-camera-1"
PARTNER_ID = "anyaicam-primary"
LOGIN_EMAIL = "rdm-test-harness@staging.anyaicam.internal"
LOGIN_PASSWORD = "RdmTestHarness-" + uuid.uuid4().hex[:16]

ANALYTIC_KEYS = ("smart_motion", "people_counting", "lpr", "ppe")


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    with connection() as db:
        db.execute(
            "INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)",
            (PARTNER_ID, "AnyAiCam", now),
        )
        db.execute(
            "INSERT OR IGNORE INTO customers"
            "(id,partner_id,name,company,email,status,trial_status,billing_status,source,created_at,created_by) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                CUSTOMER_ID, PARTNER_ID,
                "RDM TEST HARNESS -- synthetic, not a real customer",
                "SYNTHETIC TEST DATA (see PROJECT_CHECKPOINT.md)",
                LOGIN_EMAIL, "active", "n/a", "n/a", "real", now,
                "rdm-test-harness-provisioning-script",
            ),
        )
        db.execute(
            "INSERT OR IGNORE INTO sites(id,customer_id,name,address,site_type,created_at) VALUES(?,?,?,?,?,?)",
            (SITE_ID, CUSTOMER_ID, "RDM Test Harness Site (synthetic)", "", "test", now),
        )
        db.execute(
            "INSERT OR IGNORE INTO partner_users"
            "(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "rdm-test-harness-owner", PARTNER_ID, LOGIN_EMAIL,
                "RDM Test Harness Owner", "customer_owner",
                password_hash(LOGIN_PASSWORD), 1, CUSTOMER_ID, now,
            ),
        )
        # appliance_id left NULL deliberately -- see module docstring.
        # camera_number=1: the only camera under this synthetic customer,
        # no collision risk with the real pilot customer's own numbering
        # (cameras are scoped by customer_id everywhere this session has
        # already confirmed, e.g. _customer_playback_cameras()).
        db.execute(
            "INSERT OR IGNORE INTO cameras"
            "(id,customer_id,site_id,appliance_id,name,resolution,status,camera_number,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                CAMERA_ID, CUSTOMER_ID, SITE_ID, None,
                "RDM Test Camera (synthetic, no real hardware)", "2mp", "configured", 1, now,
            ),
        )
        # One analytics_subscriptions row per analytic, licensed_quantity=1
        # (schema default) -- a real, product-shaped subscription row, just
        # under a customer that only ever exists for this test harness.
        # monthly_retail/partner=0 makes the "not a real charge" intent
        # explicit in the row itself, not just the customer's own name.
        for analytic_key in ANALYTIC_KEYS:
            existing = db.execute(
                "SELECT id FROM analytics_subscriptions WHERE customer_id=? AND analytic_key=?",
                (CUSTOMER_ID, analytic_key),
            ).fetchone()
            if not existing:
                db.execute(
                    "INSERT INTO analytics_subscriptions"
                    "(id,customer_id,site_id,analytic_key,status,monthly_retail,monthly_partner,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        f"rdm-test-sub-{analytic_key}", CUSTOMER_ID, SITE_ID,
                        analytic_key, "active", 0.0, 0.0, now,
                    ),
                )

    print("provisioned:")
    print("  customer_id  =", CUSTOMER_ID)
    print("  site_id      =", SITE_ID)
    print("  camera_id    =", CAMERA_ID)
    print("  login_email  =", LOGIN_EMAIL)
    print("  login_password =", LOGIN_PASSWORD)


if __name__ == "__main__":
    main()
