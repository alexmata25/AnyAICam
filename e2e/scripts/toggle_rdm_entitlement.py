"""Toggles one analytic entitlement on the RDM test harness camera, via
the real product functions (assign_entitlement()/remove_entitlement()
in customer_analytics_panel.py) -- never raw SQL against
camera_analytics_entitlements -- so this exercises the actual code path
the product uses, including its own license-limit enforcement.

Usage: python3 toggle_rdm_entitlement.py <on|off> <analytic_key>
"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app")

from partner_db import connection
from customer_analytics_panel import assign_entitlement, remove_entitlement

CAMERA_ID = "rdm-test-harness-camera-1"


def main() -> None:
    action, analytic_key = sys.argv[1], sys.argv[2]
    now = datetime.now(timezone.utc).isoformat()
    with connection() as db:
        if action == "on":
            assign_entitlement(db, CAMERA_ID, analytic_key, now=now)
        elif action == "off":
            remove_entitlement(db, CAMERA_ID, analytic_key, now=now)
        else:
            raise SystemExit(f"unknown action: {action!r}")
        rows = db.execute(
            "SELECT analytic_key, status FROM camera_analytics_entitlements WHERE camera_id=? ORDER BY analytic_key",
            (CAMERA_ID,),
        ).fetchall()
    print(f"{action} {analytic_key} -> current state:")
    for row in rows:
        print(" ", dict(row))


if __name__ == "__main__":
    main()
