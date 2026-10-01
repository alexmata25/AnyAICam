"""Cloud customer Dashboard health (2026-10-01): on the cloud portal a
customer's System health, CPU/memory and Storage come from their own
appliance's last heartbeat -- never from the cloud host. On staging a
camera-less customer was shown "VMS running", the EC2's own CPU/memory and
"12.0 GB free · 74.6% of disk in use", and the page polled the host's
/api/system/metrics. The edge appliance's own Dashboard is unchanged.

Reuses test_dashboard_plan_indicator.py's harness.
"""

import sqlite3

import pytest

import partner_portal
from database_backend import override_target
from partner_db import initialize_database

import main

NOW = "2026-10-01T00:00:00"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_dashboard_cloud_appliance_health.db"


@pytest.fixture()
def http_client(db_path):
    from fastapi.testclient import TestClient

    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client


def _seed(db_path, customer_id, appliances=()):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','P',?)", (NOW,))
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
                 (customer_id, "partner-1", customer_id, f"{customer_id}@example.test", "active", NOW))
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"{customer_id}-site", customer_id, "Site", NOW))
    for n, (state, cpu, memory, capacity, used) in enumerate(appliances):
        conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at,state,cpu,memory,disk_capacity,disk) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (f"{customer_id}-appl-{n}", customer_id, f"{customer_id}-site", f"AIC-{customer_id}-{n}", NOW, state, cpu, memory, capacity, used))
    conn.commit()
    conn.close()


def _dashboard(client, customer_id):
    return client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)}).text


@pytest.fixture()
def cloud(monkeypatch):
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")


def _never_the_host(html):
    assert "VMS running" not in html and "Saved clips" not in html
    assert 'data-live="1"' not in html  # so the page never polls this host's /api/system/metrics
    assert "-day retention" not in html


def test_customer_with_no_appliance(http_client, db_path, cloud):
    _seed(db_path, "cust-none")
    html = _dashboard(http_client, "cust-none")
    _never_the_host(html)
    assert "No appliance connected" in html and "Appliance CPU" not in html


def test_customer_with_one_online_appliance_sees_its_own_numbers(http_client, db_path, cloud):
    _seed(db_path, "cust-one", [("online", 37.4, 52.6, 457.0, 284.0)])
    html = _dashboard(http_client, "cust-one")
    _never_the_host(html)
    assert "Appliance online" in html
    assert '<span class="stat-label">Appliance CPU</span><span class="stat-value" id="cpu-metric">37%</span>' in html
    assert '<span class="stat-label">Appliance memory</span><span class="stat-value" id="memory-metric">53%</span>' in html
    assert "173.0 GB free" in html and "62% of appliance disk in use" in html


def test_offline_appliance_and_unreported_storage_are_said_plainly(http_client, db_path, cloud):
    _seed(db_path, "cust-off", [("offline", 0, 0, 0, 0)])
    html = _dashboard(http_client, "cust-off")
    _never_the_host(html)
    assert "Appliance offline" in html and "Not reported yet" in html and "Appliance CPU" not in html


def test_several_appliances_are_summed_and_never_another_customers(http_client, db_path, cloud):
    _seed(db_path, "cust-two", [("online", 10, 20, 100.0, 40.0), ("offline", 90, 90, 100.0, 60.0)])
    _seed(db_path, "cust-other", [("online", 99, 99, 1000.0, 1.0)])
    html = _dashboard(http_client, "cust-two")
    _never_the_host(html)
    assert "1 of 2 appliances online" in html and "100.0 GB free" in html and "50% of appliance disk in use" in html
    assert "Appliance CPU" not in html and "999.0 GB" not in html and "1099.0 GB" not in html


def test_the_edge_appliance_dashboard_is_unchanged(http_client, db_path, monkeypatch):
    monkeypatch.setattr(main, "RUNTIME_ROLE", "edge")
    _seed(db_path, "cust-edge")
    html = _dashboard(http_client, "cust-edge")
    assert "VMS running" in html and "Saved clips" in html and 'data-live="1"' in html


@pytest.mark.parametrize("path", ["/api/system/metrics", "/api/alerts"])
def test_host_level_apis_refuse_a_cloud_customer(http_client, db_path, cloud, path):
    """Staging: any signed-in customer could read the cloud host's CPU/disk
    and its whole alert log (unscoped). No customer page uses either."""
    _seed(db_path, "cust-api")
    cookie = {partner_portal.SESSION_COOKIE: partner_portal._token("owner@example.test", "customer_owner", None, "cust-api", None)}
    assert http_client.get(path, cookies=cookie).status_code == 403


@pytest.mark.parametrize("path", ["/api/system/metrics", "/api/alerts"])
def test_host_level_apis_unchanged_on_the_edge_appliance(http_client, db_path, monkeypatch, path):
    monkeypatch.setattr(main, "RUNTIME_ROLE", "edge")
    _seed(db_path, "cust-edge-api")
    cookie = {partner_portal.SESSION_COOKIE: partner_portal._token("owner@example.test", "customer_owner", None, "cust-edge-api", None)}
    assert http_client.get(path, cookies=cookie).status_code == 200
