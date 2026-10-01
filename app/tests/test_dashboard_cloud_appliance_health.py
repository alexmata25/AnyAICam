"""Cloud customer Dashboard health (2026-10-01): on the cloud portal a
customer's System health, CPU/memory and Storage come from their own
appliance's last heartbeat -- never from the cloud host. On staging a
camera-less customer was shown "VMS running", the EC2's own CPU/memory and
"12.0 GB free · 74.6% of disk in use", and the page polled the host's
/api/system/metrics. The edge appliance's own Dashboard is unchanged.

Reuses test_dashboard_plan_indicator.py's harness.
"""

import sqlite3
from datetime import datetime

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
        conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at,state,cpu,memory,disk_capacity,disk,last_check_in) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (f"{customer_id}-appl-{n}", customer_id, f"{customer_id}-site", f"AIC-{customer_id}-{n}", NOW, state, cpu, memory, capacity, used,
                      datetime.now().isoformat()))
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


@pytest.mark.parametrize("path", sorted(main.CLOUD_HOST_LOCAL_API_PATHS))
def test_every_host_local_api_refuses_a_cloud_customer(http_client, db_path, cloud, path):
    """Probed live on staging with a test customer: each of these answered
    with the cloud host's own unscoped data (its alert log, site summary
    with 19 cameras, motion events, CPU/disk, AI configuration)."""
    _seed(db_path, "cust-mw")
    for role in ("customer_owner", "customer_viewer"):
        cookie = {partner_portal.SESSION_COOKIE: partner_portal._token("owner@example.test", role, None, "cust-mw", None)}
        response = http_client.get(path, cookies=cookie)
        assert response.status_code == 403, (path, role, response.text[:200])


def test_the_customer_dashboard_still_loads_its_own_apis(http_client, db_path, cloud):
    _seed(db_path, "cust-ok")
    cookie = {partner_portal.SESSION_COOKIE: partner_portal._token("owner@example.test", "customer_owner", None, "cust-ok", None)}
    import time
    day_start = int(time.time() // 86400 * 86400 * 1000)
    for path in ("/api/cameras/status", "/api/customer/events/recent",
                 f"/api/customer/dashboard/intelligence?start_ms={day_start}&end_ms={day_start + 86400000}"):
        assert http_client.get(path, cookies=cookie).status_code == 200, path


def test_local_storage_serves_only_update_packages(tmp_path, monkeypatch):
    """Face crops are stored as thumbnails/facial/<customer>/<event>.jpg in
    the local storage backend; /storage served any category to any
    signed-in account."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import cloud_features
    import object_storage
    import dataclasses
    monkeypatch.setattr(cloud_features, "settings", dataclasses.replace(cloud_features.settings, storage_backend="local"))
    object_storage.LocalStorage(str(tmp_path)).put("thumbnails", "facial/cust-2/evt.jpg", b"face")
    object_storage.LocalStorage(str(tmp_path)).put("updates", "pkg.tar.gz", b"pkg")
    monkeypatch.setattr(cloud_features, "LocalStorage", lambda root=None: object_storage.LocalStorage(str(tmp_path)))
    app = FastAPI()
    cloud_features.register_cloud_feature_routes(app, lambda *a, **k: "")
    route = next(r for r in app.routes if getattr(r, "path", "") == "/storage/{category}/{object_key:path}")
    with TestClient(app) as client:
        assert client.get("/storage/thumbnails/facial/cust-2/evt.jpg").status_code == 404
        assert client.get("/storage/updates/pkg.tar.gz").content == b"pkg"
    assert route is not None


def _fresh(minutes_ago=0):
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(minutes=minutes_ago)).isoformat()


def _set_check_in(db_path, customer_id, when):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE appliances SET last_check_in=? WHERE customer_id=?", (when, customer_id))
    conn.commit()
    conn.close()


def test_a_degraded_but_reporting_appliance_is_online(http_client, db_path, cloud):
    _seed(db_path, "cust-deg", [("degraded", 20, 30, 100.0, 50.0)])
    _set_check_in(db_path, "cust-deg", _fresh(1))
    assert "Appliance online" in _dashboard(http_client, "cust-deg")


def test_an_appliance_silent_for_over_three_minutes_is_offline_whatever_its_state(http_client, db_path, cloud):
    """The fleet sweep that flips state to offline only runs when staff
    pages load, so a stale 'online' must not be believed."""
    _seed(db_path, "cust-stale", [("online", 20, 30, 100.0, 50.0)])
    _set_check_in(db_path, "cust-stale", _fresh(10))
    html = _dashboard(http_client, "cust-stale")
    assert "Appliance offline" in html and "Appliance CPU" not in html


def test_cloud_customer_pages_show_no_host_license_banner(http_client, db_path, cloud, monkeypatch):
    monkeypatch.setattr(main, "LICENSE_ENFORCEMENT_MODE", "warn")
    monkeypatch.setattr(main, "license_enforcement_snapshot", lambda **kwargs: {"warnings": [{"message": "License status is inactive."}]})
    _seed(db_path, "cust-lic")
    assert "License attention" not in _dashboard(http_client, "cust-lic")


def test_the_edge_appliance_still_shows_its_license_banner(http_client, db_path, monkeypatch):
    monkeypatch.setattr(main, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(main, "LICENSE_ENFORCEMENT_MODE", "warn")
    monkeypatch.setattr(main, "license_enforcement_snapshot", lambda **kwargs: {"warnings": [{"message": "License status is inactive."}]})
    _seed(db_path, "cust-lic-edge")
    assert "License attention" in _dashboard(http_client, "cust-lic-edge")


def test_an_empty_table_message_stays_in_view_on_a_phone(http_client, db_path, cloud):
    """Events on a 390 px phone: the empty-state message was centred across
    the table's full scroll width and cut off mid-sentence."""
    _seed(db_path, "cust-empty")
    html = _dashboard(http_client, "cust-empty")
    assert ".data-table td .empty-stage{position:sticky;left:0;width:calc(100vw - 60px)" in html
