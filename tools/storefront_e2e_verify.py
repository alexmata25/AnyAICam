#!/usr/bin/env python3
"""Read-only check of what the portal recorded for storefront test purchases
(staging E2E, docs/STOREFRONT_STAGING_E2E_CHECKLIST.md).

Opens the portal SQLite database read-only (mode=ro) and, for the given
Checkout Session IDs and/or buyer email, lists the matching rows in every
purchase table: hardware orders, VMS licences/plans (customer_entitlements),
add-ons, the pending links an unregistered buyer gets, and webhook steps.
It never writes, never calls Stripe and never prints raw webhook JSON.

    python3 tools/storefront_e2e_verify.py --db /path/to/portal.db \\
        --session cs_test_... --session cs_test_... --email buyer@example.com

Exit status: 0 when every given session was recorded somewhere, 1 otherwise.
"""
import argparse
import sqlite3
import sys

# table -> columns shown (raw_event_json and other payloads are never shown)
TABLES = {
    "hardware_orders": ("sku", "amount_cents", "currency", "status", "customer_id"),
    "pending_hardware_order_links": ("sku", "amount_cents", "status", "resolved_customer_id"),
    "customer_entitlements": ("product", "camera_slot_quantity", "status", "customer_id"),
    "pending_customer_links": ("product", "camera_slot_quantity", "status", "resolved_customer_id"),
    "pending_analytics_links": ("analytic_key", "stripe_price_id", "status", "resolved_customer_id"),
    "addon_subscriptions": ("addon_key", "status", "stripe_price_id", "customer_id"),
    "checkout_pending": ("purchase", "price_id", "status", "customer_id"),
}
SESSION_COLUMNS = ("stripe_checkout_session_id", "session_id")


def _columns(db, table):
    return [row[1] for row in db.execute(f"PRAGMA table_info({table})")]


def find(db, sessions, email):
    """{table: [row dicts]} for rows that match a session ID or the email."""
    found = {}
    for table, shown in TABLES.items():
        columns = _columns(db, table)
        if not columns:
            continue
        where, args = [], []
        session_column = next((c for c in SESSION_COLUMNS if c in columns), None)
        if session_column and sessions:
            where.append(f"{session_column} IN ({','.join('?' * len(sessions))})")
            args += sessions
        if email and "normalized_email" in columns:
            where.append("normalized_email = ?")
            args.append(email.strip().lower())
        if not where:
            continue
        select = [c for c in (session_column, *shown, "created_at") if c and c in columns]
        rows = db.execute(f"SELECT {', '.join(select)} FROM {table} WHERE {' OR '.join(where)} ORDER BY rowid",
                          args).fetchall()
        if rows:
            found[table] = [dict(zip(select, row)) for row in rows]
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", required=True, help="portal SQLite database (opened read-only)")
    parser.add_argument("--session", action="append", default=[], help="Checkout Session ID (repeatable)")
    parser.add_argument("--email", default="", help="buyer email used on the storefront")
    args = parser.parse_args(argv)
    if not args.session and not args.email:
        parser.error("give at least one --session or --email")
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    found = find(db, args.session, args.email)
    for table, rows in found.items():
        print(f"== {table} ({len(rows)})")
        for row in rows:
            print("   " + "  ".join(f"{k}={v}" for k, v in row.items()))
    recorded = {row.get("stripe_checkout_session_id") or row.get("session_id")
                for rows in found.values() for row in rows}
    missing = [s for s in args.session if s not in recorded]
    for session in missing:
        print(f"MISSING: {session} is not recorded in any purchase table")
    print("RESULT:", "PASS" if not missing else "FAIL")
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
