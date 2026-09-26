"""Regression coverage for a cross-partner data leak on /partner-revenue
and the quote-save path behind it (2026-09-23):

POST /api/partner/calculate (partner_portal.py) saved a quote record with
no partner_id field at all whenever save_quote=true, and GET /partner-revenue
summed every saved quote's monthly_recurring_profit/first_year_profit with
no ownership check whatsoever -- any authenticated partner_owner/salesperson/
technician session could see every OTHER partner's confidential recurring-
revenue and commission totals the moment any partner anywhere had ever saved
a quote.

Fixed by stamping each saved quote with the creating identity's own
partner_id, and filtering /partner-revenue's sum through
tenant_owns_partner() -- the same already-audited primitive
partner_workspace.py's own 2026-09-14 tenant-isolation remediation
established for customer/appliance/plan routes elsewhere in this codebase.
A global administrator still sees every partner's totals unchanged; a
partner-scoped administrator or partner_owner/salesperson/technician does
not; a quote saved before this fix (no partner_id) is excluded for anyone
who isn't a genuine global administrator -- fail closed.

QUOTES_FILE (partner_portal.py's own _read_quotes()/_save_quotes()) is a
flat JSON file under RECORDINGS_FOLDER, not part of the sqlite backend
override_target() isolates, so it's monkeypatched to a per-test tmp_path
for isolation.
"""

import json

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
import pricing_config
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_partner_revenue_tenant_isolation.db"


@pytest.fixture(autouse=True)
def _isolated_quotes_file(tmp_path, monkeypatch):
    monkeypatch.setattr(partner_portal, "QUOTES_FILE", tmp_path / "partner_quotes.json")
    # calculate_partner_quote() (pricing_config.py) requires a configured
    # partner_monthly_price for whatever resolution.recording.retention
    # key _save_quote() below asks for -- seed it here rather than
    # relying on whatever happens to be configured live, so this test is
    # self-contained.
    config_file = tmp_path / "pricing_config.json"
    config_file.write_text(json.dumps({"partner": {"plan_terms": {"4mp.motion.7": {"partner_monthly_price": 6.0}}}}), encoding="utf-8")
    monkeypatch.setattr(pricing_config, "CONFIG_FILE", config_file)


@pytest.fixture()
def http_client(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, base_url="https://portal-staging.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client


def _partner_owner_cookie(partner_id, email="owner@example.test"):
    return partner_portal._token(email, "partner_owner", partner_id, None, None)


def _admin_cookie(email="admin@example.test"):
    return partner_portal._token(email, "administrator")


def _save_quote(client, cookies, customer="Test Customer", monthly_profit=100):
    payload = {
        "save_quote": True,
        "customer": customer,
        "site": "Primary",
        "resolution": "4mp",
        "recording": "motion",
        "retention": 7,
        "quantity": 4,
        "addons": [],
    }
    response = client.post("/api/partner/calculate", json=payload, cookies=cookies)
    assert response.status_code == 200, response.text
    return response.json()


def test_saved_quote_is_stamped_with_the_creating_partners_own_id(http_client):
    cookies = {partner_portal.SESSION_COOKIE: _partner_owner_cookie("partner-a")}
    _save_quote(http_client, cookies)
    quotes = partner_portal._read_quotes()
    assert len(quotes) == 1
    assert quotes[0]["partner_id"] == "partner-a"


def test_a_partner_never_sees_another_partners_revenue(http_client):
    cookie_a = {partner_portal.SESSION_COOKIE: _partner_owner_cookie("partner-a")}
    cookie_b = {partner_portal.SESSION_COOKIE: _partner_owner_cookie("partner-b")}

    _save_quote(http_client, cookie_a, customer="Partner A's Customer")
    _save_quote(http_client, cookie_b, customer="Partner B's Customer")

    response_a = http_client.get("/partner-revenue", cookies=cookie_a)
    assert response_a.status_code == 200
    assert "Partner A's Customer" not in response_a.text
    assert "<span class=\"stat-value\">1</span>" in response_a.text

    response_b = http_client.get("/partner-revenue", cookies=cookie_b)
    assert response_b.status_code == 200
    assert "<span class=\"stat-value\">1</span>" in response_b.text


def test_two_partners_active_estimate_counts_never_bleed_together(http_client):
    cookie_a = {partner_portal.SESSION_COOKIE: _partner_owner_cookie("partner-a")}
    cookie_b = {partner_portal.SESSION_COOKIE: _partner_owner_cookie("partner-b")}

    _save_quote(http_client, cookie_a)
    _save_quote(http_client, cookie_a)
    _save_quote(http_client, cookie_a)
    _save_quote(http_client, cookie_b)

    text_a = http_client.get("/partner-revenue", cookies=cookie_a).text
    text_b = http_client.get("/partner-revenue", cookies=cookie_b).text
    assert "<span class=\"stat-value\">3</span>" in text_a
    assert "<span class=\"stat-value\">1</span>" in text_b


def test_legacy_quote_with_no_partner_id_is_excluded_for_a_plain_partner(http_client):
    """Fail closed: a quote saved before this fix (or by any future bug
    that skips the stamp) must never silently attribute to whichever
    partner happens to view the page first."""
    partner_portal._save_quotes([{"id": "legacy1", "customer": "Orphaned", "monthly_recurring_profit": 500, "first_year_profit": 6000}])
    cookie_a = {partner_portal.SESSION_COOKIE: _partner_owner_cookie("partner-a")}
    text_a = http_client.get("/partner-revenue", cookies=cookie_a).text
    assert "<span class=\"stat-value\">0</span>" in text_a


def test_a_global_administrator_still_sees_every_partners_revenue(http_client):
    """Unchanged, intentional global-admin reach -- matches
    tenant_owns_partner()'s own established has_global_administrator_grant()
    bypass used throughout the rest of this codebase."""
    from appliance_identity import create_grant
    from partner_db import connection

    cookie_a = {partner_portal.SESSION_COOKIE: _partner_owner_cookie("partner-a")}
    cookie_b = {partner_portal.SESSION_COOKIE: _partner_owner_cookie("partner-b")}
    _save_quote(http_client, cookie_a)
    _save_quote(http_client, cookie_b)

    with connection() as db:
        db.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", ("anyaicam-primary", "AnyAiCam", "2026-01-01"))
        db.execute(
            "INSERT OR IGNORE INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("admin-user", "anyaicam-primary", "admin@example.test", "Global Admin", "administrator", "x", 1, "2026-01-01T00:00:00"),
        )
        create_grant(db, user_id="admin-user", role="administrator", scope_type="global", scope_id=None, granted_by="system", now="2026-01-01T00:00:00")

    admin_cookies = {partner_portal.SESSION_COOKIE: _admin_cookie()}
    text = http_client.get("/partner-revenue", cookies=admin_cookies).text
    assert "<span class=\"stat-value\">2</span>" in text
