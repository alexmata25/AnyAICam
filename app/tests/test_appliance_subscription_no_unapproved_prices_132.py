"""Appliance My subscription (Codex round 5 APPLIANCE-PRICING-DRIFT; owner 2026-10-21): on an appliance -- which never
has Stripe Price env vars -- a locally signed-in owner must see NO unapproved add-on amount, in Local (Basic Local,
AI Local) or Hybrid mode: every optional analytics package, Talk Down and Face Access row reads "Coming soon" with no
amount and no buy button. The owner-approved plan prices (Basic Local $9.99, AI Local $14.99, Hybrid $19.99 per
camera/month) and one-time licence prices ($24.99 / $37.49 / $62.49 / $99.99) are unchanged. Carried to the Windows
correction candidate by cherry-picking 99a19591 (Green, patch b212c398) onto 26b2dbfc."""
import re
import sqlite3
from datetime import datetime

import pytest

import partner_portal
import per_camera_billing
import pricing_catalog
from database_backend import override_target
from partner_db import initialize_database

import main

UNAPPROVED_AMOUNTS = ("$7.99", "$4.99", "$39.99", "$49.99", "$69.99")   # amounts that collide with no approved price
ROW = re.compile(r'<div class="health-row">(.*?)</div>', re.S)


@pytest.fixture()
def appliance_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import os
    for name in [key for key in os.environ if "PRICE" in key.upper()]:
        monkeypatch.delenv(name, raising=False)          # an appliance has no Stripe Price configured
    db_path = tmp_path / "appliance-subscription.db"
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, base_url="http://127.0.0.1:8000", follow_redirects=False) as client:
            yield client, db_path


def _seed(db_path, plan_key):
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Customer','owner@example.test','active',?)", (now,))
    conn.execute("INSERT INTO camera_plan_entitlements_v2(id,customer_id,plan_key,camera_quantity,stripe_customer_id,stripe_subscription_id,"
                 "stripe_subscription_item_id,stripe_price_id,status,local_retention_days,created_at,updated_at) "
                 "VALUES('v2-1','cust-1',?,2,'cus_A','sub_A','si_A','price_x','active',30,?,?)", (plan_key, now, now))
    conn.commit()
    conn.close()


def _page(client, db_path, plan_key):
    _seed(db_path, plan_key)
    cookie = partner_portal._token("owner@example.test", "customer_owner", None, "cust-1", None)
    response = client.get("/subscription-portal", cookies={partner_portal.SESSION_COOKIE: cookie})
    assert response.status_code == 200
    return response.text


def _addon_section(html):
    start = html.find("Optional analytics</h3>")
    end = html.find("Advanced Line Crossing is not sold separately")
    assert start != -1 and end > start, "the add-on section is missing from My subscription"
    return html[start:end]


@pytest.mark.parametrize("plan_key", ["basic_local", "ai_local", "hybrid"])
def test_no_unapproved_add_on_amount_is_shown_on_an_appliance(appliance_client, plan_key):
    client, db_path = appliance_client
    html = _page(client, db_path, plan_key)
    section = _addon_section(html)
    rows = ROW.findall(section)
    assert rows, "no add-on rows rendered"
    for row in rows:
        assert "$" not in row, f"an add-on row shows an amount on an appliance ({plan_key}): {row}"
    assert "Coming soon" in section
    assert "Not available yet" not in section
    assert "addon-buy-button" not in section          # nothing to buy without an approved, configured Price
    for amount in UNAPPROVED_AMOUNTS:
        assert amount not in html, f"{amount} appears on the {plan_key} appliance page"


def test_face_access_and_talk_down_rows_are_coming_soon_without_amounts(appliance_client):
    client, db_path = appliance_client
    section = _addon_section(_page(client, db_path, "basic_local"))
    face = [row for row in ROW.findall(section) if "Face Access" in row]
    assert face and all("Coming soon" in row and "per door/month" not in row for row in face if "Enterprise" not in row)
    talk = [row for row in ROW.findall(section) if "Talk Down" in row]
    assert all("$" not in row for row in talk)


def test_the_owner_approved_plan_and_licence_prices_are_unchanged():
    assert {key: plan["monthly_cents_per_camera"] for key, plan in per_camera_billing.PLANS.items()} == {
        "basic_local": 999, "ai_local": 1499, "hybrid": 1999}
    assert [(capacity, cents) for capacity, cents, _env in pricing_catalog.VMS_LICENSES] == [
        (8, 2499), (16, 3749), (32, 6249), (64, 9999)]
