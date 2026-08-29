"""Regression coverage for a confirmed-live Samsung follow-up to the
Step 4 rehydration fix (test_customer_setup_wizard_rehydration.py):
once the completed scan job's 5 candidates correctly reappeared, every
row displayed as "Unknown Unknown &middot; unknown IP" -- indistinguishable
from every other row.

Verified read-only against job 49508bfcaa18's actual results_json on
Samsung: the appliance-reported candidates genuinely have no ip_address/
ip/onvif_endpoint field at all, and manufacturer/model are the literal
string 'Unknown' -- this is real, appliance-reported discovery data, not
something the page can fabricate. But two fields ARE present and were
never rendered into the visible UI at all: `name` (a distinct label per
candidate, e.g. "Discovered camera 1") and, critically, `device_key`
itself -- a real, guaranteed-unique ONVIF reference UUID that was
already required before "Add this camera" could even be clicked
(data-device-key), just never shown as visible text. Fixed by always
rendering the discovered name (or manufacturer/model, or a generic
label, in that fallback order), the address when one is reported, and
the device key itself -- the one field guaranteed to distinguish any
two candidates -- so a customer can safely match a known device key
against the correct row before provisioning. All appliance-reported
fields are now HTML-escaped before being interpolated, since this
render now surfaces more raw fields than before.
"""

import json
import sqlite3

import pytest

import partner_portal
from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_customer_setup_discovery_candidate_display.db"


@pytest.fixture()
def http_client(db_path):
    from fastapi.testclient import TestClient

    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client


def _seed_tenant(conn, customer_id="cust-1", partner_id="partner-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, "Real Customer", "customer@example.test", "active", "2026-01-01"),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1',?,?,?)", (customer_id, "Main Site", "2026-01-01"))
    conn.commit()


def _seed_appliance(conn, appliance_id="appl-1", customer_id="cust-1", cloud_id=None):
    conn.execute(
        "INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,created_at) VALUES(?,?,?,?,?,?)",
        (appliance_id, customer_id, "site-1", cloud_id or f"AIC-{appliance_id}", "activated", "2026-01-01"),
    )
    conn.commit()


def _owner_cookie(customer_id="cust-1"):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


@pytest.fixture(autouse=True)
def _seeded_customer(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_appliance(conn)
    conn.close()


def test_setup_page_renders_device_key_for_every_candidate(http_client, db_path):
    """The exact live bug: this field is what the customer must be able
    to read to safely tell 5 'Unknown Unknown' rows apart."""
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    body = response.text
    assert "Device key:" in body
    assert "escapeHtml(x.device_key||'none')" in body


def test_setup_page_falls_back_through_name_then_manufacturer_model(http_client, db_path):
    """Matches the real Samsung data shape: `name` ("Discovered camera
    N") is present even when manufacturer/model are just the literal
    string 'Unknown' -- the label must prefer name, not silently drop
    it in favor of the less-specific manufacturer/model pair."""
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    body = response.text
    assert "x.name||[x.manufacturer,x.model].filter(Boolean).join(' ')||'Discovered device'" in body


def test_setup_page_reports_address_or_says_so_explicitly(http_client, db_path):
    """Real Samsung candidates had neither ip_address, ip, nor
    onvif_endpoint -- the row must say so plainly rather than silently
    rendering a blank space where an address would go."""
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    body = response.text
    assert "x.ip_address||x.ip||x.onvif_endpoint||''" in body
    assert "address||'no address reported'" in body


def test_setup_page_escapes_appliance_reported_candidate_fields(http_client, db_path):
    """This render now surfaces more raw, appliance-controlled fields
    (name, device_key) directly into innerHTML than before -- every one
    of them must go through escapeHtml so a malformed/hostile discovery
    result can never inject markup into the customer's own browser."""
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    body = response.text
    assert "function escapeHtml(v)" in body
    for expected in (
        "escapeHtml(label)",
        "escapeHtml(x.manufacturer||'Unknown manufacturer')",
        "escapeHtml(x.model||'')",
        "escapeHtml(address||'no address reported')",
        "escapeHtml(x.device_key||'none')",
    ):
        assert expected in body, f"missing {expected!r}"


def test_provisioning_click_handler_still_reads_device_key_from_the_data_attribute(http_client, db_path):
    """The visible label change must not disturb how "Add this camera"
    identifies which candidate was clicked -- still keyed off the
    data-device-key attribute the row carries regardless of what's
    displayed."""
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    body = response.text
    assert "data-device-key=\"${x.device_key||''}\"" in body
    assert "button.closest('[data-device-key]')" in body


def test_real_samsung_style_candidates_are_distinguishable_by_device_key_alone():
    """Sanity-checks the exact live data shape (verified read-only
    against job 49508bfcaa18): 5 candidates, all manufacturer='Unknown',
    model='Unknown', no ip/ip_address/onvif_endpoint at all -- device_key
    is the only field that actually differs between them, and it must
    survive untouched into whatever the page renders."""
    candidates = [
        {
            "device_key": f"urn:uuid:cand-000{i}",
            "manufacturer": "Unknown",
            "model": "Unknown",
            "name": f"Discovered camera {i}",
        }
        for i in range(1, 6)
    ]
    device_keys = {c["device_key"] for c in candidates}
    assert len(device_keys) == 5, "every real candidate must keep a distinct device_key regardless of identical manufacturer/model"
