"""Salesperson attribution changes are centrally audited (2026-10-02, Codex
finding). POST /api/admin/sales-attribution could replace a customer's
salesperson -- redirecting future commissions -- with no audit entry
preserving the previous assignment. Initial assignment and replacement now
both write one, through sales_commissions.set_attribution()."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target
from test_pricing_ff_commission import _seed, db_path  # noqa: F401 -- fixtures and helpers


@pytest.fixture()
def admin_client(db_path):
    from global_admin_helper import make_live_global_admin
    with override_target(sqlite_path=str(db_path)):
        _seed(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) "
                     "VALUES('sales-2','partner-1','second@example.test','Second Sales','salesperson','x',1,'2026-01-01')")
        conn.commit()
        conn.close()
        make_live_global_admin("platform@anyaicam.test")
        with TestClient(main.app, base_url="https://app.anyaicam.com") as client:
            client.cookies.set(partner_portal.SESSION_COOKIE,
                               partner_portal._token("platform@anyaicam.test", "administrator", "anyaicam-primary"))
            yield client


def _audit(db_path):
    conn = sqlite3.connect(db_path)
    return [(r[0], r[1], r[2], json.loads(r[3])) for r in conn.execute(
        "SELECT actor_email,action,entity_id,details_json FROM audit_logs WHERE action LIKE 'sales_attribution.%' ORDER BY rowid")]


def test_initial_assignment_and_replacement_are_both_audited(admin_client, db_path):
    first = admin_client.post("/api/admin/sales-attribution", json={"customer_id": "cust-1", "salesperson": "sales@example.test"})
    assert first.status_code == 200, first.text
    second = admin_client.post("/api/admin/sales-attribution",
                               json={"customer_id": "cust-1", "salesperson": "second@example.test", "replace": True})
    assert second.status_code == 200 and second.json()["attribution"]["salesperson_user_id"] == "sales-2"
    entries = _audit(db_path)
    assert [e[1] for e in entries] == ["sales_attribution.assigned", "sales_attribution.replaced"]
    assigned, replaced = entries[0][3], entries[1][3]
    assert entries[0][0] == "platform@anyaicam.test" and entries[0][2] == "cust-1"
    assert assigned["previous_salesperson_user_id"] is None and assigned["new_salesperson_user_id"] == "sales-1"
    assert assigned["replacement"] is False
    assert replaced["previous_salesperson_user_id"] == "sales-1" and replaced["new_salesperson_user_id"] == "sales-2"
    assert replaced["replacement"] is True and replaced["customer_id"] == "cust-1"


def test_a_refused_non_replacing_change_is_not_logged_as_a_change(admin_client, db_path):
    admin_client.post("/api/admin/sales-attribution", json={"customer_id": "cust-1", "salesperson": "sales@example.test"})
    kept = admin_client.post("/api/admin/sales-attribution", json={"customer_id": "cust-1", "salesperson": "second@example.test"})
    assert kept.json()["attribution"]["salesperson_user_id"] == "sales-1"  # replace was not requested
    assert [e[1] for e in _audit(db_path)] == ["sales_attribution.assigned"]
