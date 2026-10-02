"""Grants real analytics_subscriptions (licensed_quantity=5 -- exactly
the real 5-camera fleet, no more, no less) for the real pilot/staging
customer, so RDM entitlement toggling can be tested against the real
5-camera fleet through the real product functions
(assign_entitlement()/remove_entitlement() in
customer_analytics_panel.py), rather than a synthetic camera.

Explicitly authorized 2026-09-16: the user clarified there are no real
AnyAiCam VMS customers or real customer billing yet (the product has
not launched) -- this staging account is a development/test account,
not real customer billing data. This script is still deliberately
conservative: licensed_quantity is set to exactly 5 (the real camera
count), not an arbitrary large number, so the license-limit
enforcement logic itself stays real and testable rather than being
neutered by an unlimited-looking quantity.
"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app")

from partner_db import connection

CUSTOMER_ID = "d75bdbecdd4887de4d2b89a9fcea9092"
SITE_ID = "f67fa371cd"
ANALYTIC_KEYS = ("smart_motion", "people_counting", "lpr", "ppe")
LICENSED_QUANTITY = 5


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    with connection() as db:
        for analytic_key in ANALYTIC_KEYS:
            existing = db.execute(
                "SELECT id, licensed_quantity FROM analytics_subscriptions WHERE customer_id=? AND analytic_key=? AND status!='cancelled'",
                (CUSTOMER_ID, analytic_key),
            ).fetchone()
            if existing:
                db.execute(
                    "UPDATE analytics_subscriptions SET licensed_quantity=? WHERE id=?",
                    (LICENSED_QUANTITY, existing["id"]),
                )
                print(f"updated {analytic_key}: licensed_quantity -> {LICENSED_QUANTITY} (was {existing['licensed_quantity']})")
            else:
                db.execute(
                    "INSERT INTO analytics_subscriptions"
                    "(id,customer_id,site_id,analytic_key,status,monthly_retail,monthly_partner,licensed_quantity,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        f"real-fleet-sub-{analytic_key}", CUSTOMER_ID, SITE_ID,
                        analytic_key, "active", 0.0, 0.0, LICENSED_QUANTITY, now,
                    ),
                )
                print(f"created {analytic_key}: licensed_quantity={LICENSED_QUANTITY}")

    with connection() as db:
        rows = db.execute(
            "SELECT analytic_key, status, licensed_quantity FROM analytics_subscriptions WHERE customer_id=? ORDER BY analytic_key",
            (CUSTOMER_ID,),
        ).fetchall()
    print("final subscription state:")
    for row in rows:
        print(" ", dict(row))


if __name__ == "__main__":
    main()
