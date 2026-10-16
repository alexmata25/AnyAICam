"""VMS 1.2.7: the installed VMS offers "Activate Your System" only to a linked appliance whose account has no plan.

Owner-approved flow (2026-10-16): download -> install -> link -> Activate Your System -> Build Your System checkout ->
back to the installed VMS. The prompt must never appear on the cloud, before linking, for a paying account, or when
the appliance simply has not heard from the cloud yet (unknown).
"""
import json
import sqlite3

import pytest

import activation_prompt
import appliance_activation
import appliance_config_cache
import main
import partner_portal
import vms_capacity
from database_backend import override_target
from partner_db import initialize_database

URL = "https://anyaicam.com/build-your-system.html?source=installed"
NO_PLAN = {"camera_slot_quantity": 0, "plan_camera_slots": 0, "vms_license_capacity": 0}
PLAN_4 = {"camera_slot_quantity": 4, "plan_camera_slots": 4, "vms_license_capacity": 4}


def _write_identity(identity_file, customer_id="cust-1"):
    identity_file.write_text(json.dumps({
        "appliance_id": "appl-1", "cloud_id": "AIC-C0FFEE01", "credential": "secret", "customer_id": customer_id,
        "site_id": "site-1", "partner_id": "partner-1", "activated_at": "2026-10-16T00:00:00", "activation_version": 1,
    }), encoding="utf-8")


@pytest.fixture()
def edge(tmp_path, monkeypatch):
    identity_file = tmp_path / "appliance_identity.json"
    monkeypatch.setattr(appliance_activation, "ACTIVATION_IDENTITY_FILE", identity_file)
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")
    appliance_config_cache.reset()
    monkeypatch.setattr(vms_capacity, "load_capacity", lambda: None)
    yield identity_file
    appliance_config_cache.reset()


def _poll(capacity):
    appliance_config_cache.publish({"cameras": [], "usable_camera_capacity": dict(capacity)})


# ------------------------------------------------------------------------------------------------ state
def test_never_on_the_cloud(edge, monkeypatch):
    _write_identity(edge)
    _poll(NO_PLAN)
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "cloud")
    assert activation_prompt.activation_state() == activation_prompt.NOT_EDGE
    assert activation_prompt.banner_html("customer_owner") == ""


def test_never_before_the_appliance_is_linked(edge):
    _poll(NO_PLAN)
    assert activation_prompt.activation_state() == activation_prompt.NOT_LINKED


def test_linked_and_the_cloud_reports_no_plan_and_no_licence(edge):
    _write_identity(edge)
    _poll(NO_PLAN)
    assert activation_prompt.activation_state() == activation_prompt.NEEDS_ACTIVATION


@pytest.mark.parametrize("capacity", [PLAN_4, {"plan_camera_slots": 0, "vms_license_capacity": 8},
                                      {"plan_camera_slots": 2, "vms_license_capacity": 0}])
def test_a_plan_or_a_licence_means_active(edge, capacity):
    _write_identity(edge)
    _poll(capacity)
    assert activation_prompt.activation_state() == activation_prompt.ACTIVE
    assert activation_prompt.banner_html("customer_owner") == ""


def test_unknown_never_prompts_a_paying_customer(edge):
    """Right after linking, or offline with no accepted signed capacity: no fresh poll, nothing accepted."""
    _write_identity(edge)
    assert activation_prompt.activation_state() == activation_prompt.UNKNOWN
    assert activation_prompt.banner_html("customer_owner") == ""


def test_offline_the_last_accepted_signed_capacity_decides(edge, monkeypatch):
    _write_identity(edge)
    monkeypatch.setattr(vms_capacity, "load_capacity", lambda: {**NO_PLAN, "source": "signed"})
    assert activation_prompt.activation_state() == activation_prompt.NEEDS_ACTIVATION
    monkeypatch.setattr(vms_capacity, "load_capacity", lambda: {**PLAN_4, "source": "signed"})
    assert activation_prompt.activation_state() == activation_prompt.ACTIVE


def test_the_fresh_cloud_poll_wins_over_the_older_accepted_capacity(edge, monkeypatch):
    """A customer who just paid: the poll already says 4 while the accepted store still says 0."""
    _write_identity(edge)
    monkeypatch.setattr(vms_capacity, "load_capacity", lambda: {**NO_PLAN, "source": "signed"})
    _poll(PLAN_4)
    assert activation_prompt.activation_state() == activation_prompt.ACTIVE


def test_a_broken_reader_fails_quietly_to_unknown(edge, monkeypatch):
    _write_identity(edge)

    def boom():
        raise RuntimeError("disk")
    monkeypatch.setattr(vms_capacity, "load_capacity", boom)
    assert activation_prompt.activation_state() == activation_prompt.UNKNOWN


# ------------------------------------------------------------------------------------------------ banner
def test_owner_banner_links_to_build_your_system_with_no_identifiers(edge):
    _write_identity(edge)
    _poll(NO_PLAN)
    html = activation_prompt.banner_html("customer_owner")
    assert 'id="activate-system-banner"' in html and "Activate Your System" in html
    assert f'href="{URL}"' in html.replace("&amp;", "&")
    assert 'target="_blank" rel="noopener"' in html
    for secret in ("AIC-C0FFEE01", "cust-1", "appl-1", "site-1", "secret"):
        assert secret not in html


def test_a_viewer_is_told_to_ask_the_owner_and_gets_no_button(edge):
    _write_identity(edge)
    _poll(NO_PLAN)
    html = activation_prompt.banner_html("customer_viewer")
    assert "Ask the account owner" in html
    assert 'id="activate-system-button"' not in html


# ------------------------------------------------------------------------------------------------ pages
@pytest.fixture()
def edge_client(tmp_path, edge):
    from fastapi.testclient import TestClient
    db_path = tmp_path / "edge.db"
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','P','2026-10-16')")
        conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) "
                     "VALUES('cust-1','partner-1','Home','owner@example.test','active','2026-10-16')")
        conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Home','2026-10-16')")
        conn.commit()
        conn.close()
        with TestClient(main.app, follow_redirects=False) as client:
            yield client


def _cookie(role="customer_owner"):
    return {partner_portal.SESSION_COOKIE: partner_portal._token("owner@example.test", role, None, "cust-1", None)}


def test_my_cameras_offers_activation_instead_of_a_dead_end(edge_client, edge):
    _write_identity(edge)
    _poll(NO_PLAN)
    response = edge_client.get("/customer-account", cookies=_cookie())
    assert response.status_code == 200, response.headers.get("location")
    assert 'id="activate-system-banner"' in response.text
    assert 'id="customer-account-activate"' in response.text
    assert URL in response.text.replace("&amp;", "&")
    assert "No camera slots are available on this account yet." not in response.text


def test_an_active_account_sees_no_activation_anywhere(edge_client, edge):
    _write_identity(edge)
    _poll(PLAN_4)
    response = edge_client.get("/customer-account", cookies=_cookie())
    assert response.status_code == 200
    assert "activate-system" not in response.text and "customer-account-activate" not in response.text
