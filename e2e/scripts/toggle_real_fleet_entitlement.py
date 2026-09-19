"""Toggles one analytic entitlement, for one or all of the 5 real
cameras, through the real product functions (assign_entitlement()/
remove_entitlement() in customer_analytics_panel.py) -- never raw SQL
against camera_analytics_entitlements. Requires
provision_real_fleet_subscriptions.py to have already granted enough
licensed_quantity for the real analytics_subscriptions row this pulls
from.

Usage: python3 toggle_real_fleet_entitlement.py <on|off> <analytic_key> [camera_id ...]
If no camera_id args are given, applies to all 5 real cameras.
"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app")

from partner_db import connection
from customer_analytics_panel import assign_entitlement, remove_entitlement

REAL_CAMERA_IDS = ("dfba6a63ec", "dc7a226120", "5c689a0c0e", "55bdd715ea", "41dc80c85e")


def main() -> None:
    action, analytic_key = sys.argv[1], sys.argv[2]
    camera_ids = sys.argv[3:] or list(REAL_CAMERA_IDS)
    now = datetime.now(timezone.utc).isoformat()
    results = []
    with connection() as db:
        for camera_id in camera_ids:
            try:
                if action == "on":
                    assign_entitlement(db, camera_id, analytic_key, now=now)
                    results.append((camera_id, "ok"))
                elif action == "off":
                    remove_entitlement(db, camera_id, analytic_key, now=now)
                    results.append((camera_id, "ok"))
                else:
                    raise SystemExit(f"unknown action: {action!r}")
            except Exception as error:
                results.append((camera_id, f"ERROR: {type(error).__name__}: {error}"))
    for camera_id, outcome in results:
        print(f"{action} {analytic_key} camera={camera_id}: {outcome}")

    with connection() as db:
        rows = db.execute(
            "SELECT camera_id, analytic_key, status FROM camera_analytics_entitlements "
            "WHERE analytic_key=? AND camera_id IN ({}) ORDER BY camera_id".format(
                ",".join("?" for _ in REAL_CAMERA_IDS)
            ),
            (analytic_key, *REAL_CAMERA_IDS),
        ).fetchall()
    print(f"current {analytic_key} state across all 5 real cameras:")
    for row in rows:
        print(" ", dict(row))


if __name__ == "__main__":
    main()
