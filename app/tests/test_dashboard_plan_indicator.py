"""Dashboard plan indicator (2026-09-21): a status-only "Plan" stat
(Local/Hybrid/No active plan) added to the dashboard's system-summary
row, linking to /subscription-portal ("My subscription") for the
detailed comparison/upgrade/add-ons -- deliberately no pricing on the
dashboard itself, per the standing "do not clutter the main dashboard
with pricing" instruction. Derived from the same product_mode_for_
customer() the /subscription-portal customer branch itself uses, so the
two pages can never disagree about which plan a customer is on.

Reuses test_dashboard_camera_tenant_scoping.py's own established harness.
"""

import sqlite3

import pytest

import partner_portal
from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_dashboard_plan_indicator.db"


@pytest.fixture()
def http_client(db_path):
    from fastapi.testclient import TestClient

    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client


def _seed_tenant(conn, customer_id, partner_id="partner-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, f"Customer {customer_id}", f"{customer_id}@example.test", "active", "2026-01-01"),
    )


def _owner_cookie(customer_id):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


def _admin_cookie():
    return partner_portal._token("admin@example.test", "administrator")


def _plan_stat(html: str) -> str:
    marker = '<span class="stat-label">Plan</span><span class="stat-value">'
    start = html.index(marker) + len(marker)
    return html[start:html.index("</span>", start)]


def test_local_customer_dashboard_shows_local(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_local", camera_slot_quantity=8)
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert _plan_stat(response.text) == "Local"


def test_hybrid_customer_dashboard_shows_hybrid(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8)
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert _plan_stat(response.text) == "Hybrid"


def test_customer_with_no_plan_dashboard_shows_no_active_plan(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert _plan_stat(response.text) == "No active plan"


def test_plan_stat_links_to_subscription_portal(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert 'href="/subscription-portal"' in response.text


def test_dashboard_never_shows_pricing_in_the_plan_stat(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    conn.commit()
    conn.close()
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-1", product="camera_slots_hybrid", camera_slot_quantity=8)
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert "$" not in _plan_stat(response.text)


def test_staff_session_dashboard_shows_no_plan_stat_at_all(http_client, db_path):
    """No per-customer entitlement concept applies to a staff/legacy
    dashboard view -- the stat is omitted entirely, not shown as a
    guess or a broken placeholder."""
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _admin_cookie()})
    assert response.status_code == 200
    assert '<span class="stat-label">Plan</span>' not in response.text
