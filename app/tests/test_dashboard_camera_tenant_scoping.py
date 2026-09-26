"""Dashboard/`/api/cameras/status` tenant-scoping fix (2026-09-04): a
confirmed-live cross-tenant bug distinct from get_camera_numbers()'s own
already-fixed license-count leak (see test_camera_count_tenant_scoping.py
-- that fix is untouched by this one and stays covered by its own
suite). camera_status() (GET /api/cameras/status, the endpoint
dashboard()'s own JS fetches on load) had no request parameter and no
customer_id concept at all -- it unconditionally iterated the shared,
multi-tenant get_camera_numbers() sequence, so a real customer_owner's
Dashboard summed camera rows across every customer_id in the database.
Confirmed live: the same person's two real accounts (an active one with
5 real cameras, and an older, still-open pending_installation one with
5 placeholder cameras using the same camera_number range) produced
"0 of 10 cameras online" on the active account's own Dashboard. It also
always reported every camera offline regardless of real state, because
its online/recording signal was a local HLS-manifest file plus an
in-process camera_process_state dict -- both edge-appliance-only
concepts (correct on Samsung/Ryzen, where a local relay actually writes
those files) that are never populated on this cloud host, which runs no
local relay for a customer's cameras at all.

Fix: camera_status() and dashboard() both gained a request parameter
and now check _customer_playback_cameras(request) first -- the same
customer-identity boundary Playback/Events/Alerts/Investigate already
use (see that function's own docstring). A customer-portal identity
gets only its own camera list, already scoped end-to-end by that
helper's existing customer_id-filtered query, with online/recording
read from appliance_camera_status (the appliance's own live heartbeat,
already correct and already used elsewhere -- e.g.
partner_workspace.py's installed-camera check) keyed by the real
camera.id, never the collision-prone bare camera_number. No customer
identity at all (every existing edge/Samsung/Ryzen caller) falls
through to _legacy_camera_status(), the exact original function body,
completely unchanged, still keyed by the unscoped camera_number
sequence and the local HLS-manifest/camera_process_state signal.

Full-stack tests below hit the real FastAPI routes through TestClient
with a real customer_owner session cookie, exactly like production --
see test_customer_camera_count_excludes_placeholders.py for the same
established harness pattern this file reuses.
"""

import sqlite3

import pytest

import partner_portal
from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_dashboard_camera_tenant_scoping.db"


@pytest.fixture()
def http_client(db_path):
    from fastapi.testclient import TestClient

    with override_target(sqlite_path=db_path):
        initialize_database()
        # This suite runs its swap-test directly inside the real,
        # deployed EC2 container, where cloud_settings.deployed is True
        # and TrustedHostMiddleware is therefore active (confirmed
        # pre-existing and unrelated to this fix: every other http_client
        # TestClient suite in this repo, e.g.
        # test_customer_camera_count_excludes_placeholders.py, hits the
        # same "Invalid host header" 400 when run directly on this host
        # for the same reason). base_url must be one of
        # cloud_settings.effective_trusted_hosts -- app.anyaicam.com is
        # both an allowed host and the real production hostname the
        # live "10 cameras" bug was reported against.
        with TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client


def _seed_tenant(conn, customer_id, partner_id="partner-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, f"Customer {customer_id}", f"{customer_id}@example.test", "active", "2026-01-01"),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{customer_id}", customer_id, "Main Site", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
        (f"app-{customer_id}", customer_id, f"site-{customer_id}", f"cloud-{customer_id}", "2026-01-01"),
    )


def _seed_camera(conn, camera_id, *, customer_id, camera_number, name, status="configured"):
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES(?,?,?,?,?,?,?)",
        (camera_id, customer_id, f"site-{customer_id}", name, status, camera_number, "2026-01-01"),
    )


def _seed_status(conn, *, appliance_id, camera_id, online, recording):
    conn.execute(
        "INSERT INTO appliance_camera_status(appliance_id,camera_id,online,recording,updated_at) VALUES(?,?,?,?,?)",
        (appliance_id, camera_id, int(online), int(recording), "2026-01-01"),
    )


def _seed_two_customers_five_cameras_each(conn):
    """Mirrors the confirmed-live shape: two real customer_id rows, each
    with 5 cameras numbered 1-5 (a real collision, since both use the
    same camera_number range). Customer A's cameras are genuinely
    online/recording per their appliance's own heartbeat; Customer B's
    are not -- proves the response reflects the real, per-tenant
    heartbeat, not a shared/global or copy-pasted signal."""
    _seed_tenant(conn, "cust-a")
    _seed_tenant(conn, "cust-b")
    for n in range(1, 6):
        _seed_camera(conn, f"a-cam-{n}", customer_id="cust-a", camera_number=n, name=f"A Camera {n}")
        _seed_camera(conn, f"b-cam-{n}", customer_id="cust-b", camera_number=n, name=f"B Camera {n}")
    for n in range(1, 6):
        _seed_status(conn, appliance_id="app-cust-a", camera_id=f"a-cam-{n}", online=True, recording=(n != 3))
        _seed_status(conn, appliance_id="app-cust-b", camera_id=f"b-cam-{n}", online=False, recording=False)


def _owner_cookie(customer_id):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


# --------------------------------------------------------------- GET /api/cameras/status

def test_customer_a_camera_status_returns_exactly_5_cameras_not_10(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_two_customers_five_cameras_each(conn)
    conn.commit()
    conn.close()
    response = http_client.get("/api/cameras/status", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")})
    data = response.json()
    assert len(data["cameras"]) == 5
    assert sorted(c["camera"] for c in data["cameras"]) == [1, 2, 3, 4, 5]


def test_customer_bs_cameras_never_appear_in_customer_as_status_response(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_two_customers_five_cameras_each(conn)
    # Give customer B's cameras a distinctive, obviously-wrong signal
    # (all online) so if they ever leaked into A's response, this test
    # would catch it even if the count alone happened to still read 5.
    conn.execute("UPDATE appliance_camera_status SET online=1,recording=1 WHERE appliance_id='app-cust-b'")
    conn.commit()
    conn.close()
    response = http_client.get("/api/cameras/status", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")})
    data = response.json()
    assert len(data["cameras"]) == 5
    # Customer A seeded: online for all 5, recording stopped only for #3.
    assert all(c["online"] for c in data["cameras"])
    stopped = [c for c in data["cameras"] if c["recording"] == "stopped"]
    assert [c["camera"] for c in stopped] == [3]


def test_online_and_recording_reflect_the_real_appliance_heartbeat(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_two_customers_five_cameras_each(conn)
    conn.commit()
    conn.close()
    response = http_client.get("/api/cameras/status", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-b")})
    data = response.json()
    assert len(data["cameras"]) == 5
    # Customer B seeded: online=0, recording=0 for every camera.
    assert all(c["online"] is False for c in data["cameras"])
    assert all(c["recording"] == "stopped" for c in data["cameras"])
    assert all(c["stream"] == "offline" for c in data["cameras"])


def test_a_customer_with_zero_cameras_gets_an_empty_list_not_the_global_total(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_two_customers_five_cameras_each(conn)
    conn.commit()
    conn.close()
    response = http_client.get("/api/cameras/status", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-c-has-none")})
    data = response.json()
    assert data["cameras"] == []


def test_camera_status_dispatches_to_the_untouched_legacy_function_when_no_customer_identity(monkeypatch):
    # A caller with no customer-portal identity at all (every existing
    # edge/Samsung/Ryzen local-VMS session, and the legacy admin/
    # installer cloud identity) requires its own, separate local-VMS
    # authentication that has nothing to do with this fix -- proving
    # that stack end-to-end belongs to its own suite, not this one.
    # What this fix actually changes is the dispatch in front of it:
    # _customer_playback_cameras(request) returning None must route to
    # _legacy_camera_status(), the exact original camera_status() body,
    # completely unchanged (only renamed by the patch that wraps it).
    monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: None)
    sentinel = {"cameras": ["exact-original-behavior"], "checked_at": "sentinel"}
    monkeypatch.setattr(main, "_legacy_camera_status", lambda: sentinel)

    def _fail_if_called(*a, **k):
        raise AssertionError("_customer_camera_status must not run when there is no customer identity")

    monkeypatch.setattr(main, "_customer_camera_status", _fail_if_called)
    assert main.camera_status(request=None) is sentinel


def test_camera_status_dispatches_to_the_customer_branch_when_identity_present(monkeypatch):
    customer_cameras = [{"id": "cam-1", "name": "Front Door", "camera_number": 1}]
    monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: customer_cameras)
    sentinel = {"cameras": ["customer-branch"], "checked_at": "sentinel"}
    calls = []

    def _fake_customer_camera_status(cameras):
        calls.append(cameras)
        return sentinel

    monkeypatch.setattr(main, "_customer_camera_status", _fake_customer_camera_status)

    def _fail_if_called():
        raise AssertionError("_legacy_camera_status must not run when a customer identity is present")

    monkeypatch.setattr(main, "_legacy_camera_status", _fail_if_called)
    assert main.camera_status(request=None) is sentinel
    assert calls == [customer_cameras]


def test_dashboard_camera_numbers_fall_back_to_the_unscoped_list_source_check():
    # dashboard()'s own body is too heavy to unit-call directly (real
    # recordings folder, disk usage, etc. -- all pre-existing and
    # completely untouched by this fix). The one line this fix actually
    # changed in the no-customer-identity branch is a trivial,
    # unconditional fallback to the exact original expression -- proven
    # here by source inspection instead of execution, the same
    # structural-proof idiom test_mobile_playback_video_cards.py already
    # established in this suite for a similarly heavy render function.
    import inspect

    source = inspect.getsource(main.dashboard)
    assert "_dashboard_camera_numbers = list(get_camera_numbers())" in source
    assert "for camera_number in _dashboard_camera_numbers" in inspect.getsource(main.dashboard)


# --------------------------------------------------------------- GET /dashboard

def test_dashboard_page_renders_5_camera_cards_for_the_real_customer_not_10(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_two_customers_five_cameras_each(conn)
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")})
    assert response.status_code == 200
    html = response.text
    assert html.count('class="dashboard-camera-card"') == 5
    for n in range(1, 6):
        assert f'id="dashboard-camera-{n}"' in html


def test_dashboard_redirects_to_login_with_no_session_at_all_same_as_before_this_fix(http_client, db_path):
    # Pre-existing, unrelated to this fix: /dashboard requires some
    # login (confirmed identical before and after this patch via direct
    # A/B on the live container) -- a request with no session of any
    # kind never reaches dashboard()'s body at all, customer or legacy.
    # This only proves that gate still exists and dashboard() itself
    # doesn't bypass it; the actual "legacy caller" no-op-change proof
    # is test_dashboard_camera_numbers_fall_back_to_the_unscoped_list_
    # source_check() above, since a real legacy (local-VMS) session
    # requires unrelated local-auth setup this fix's own diff never
    # touches.
    conn = sqlite3.connect(db_path)
    _seed_two_customers_five_cameras_each(conn)
    conn.commit()
    conn.close()
    response = http_client.get("/dashboard")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/customer-login.html")


# --------------------------------------------------------------- _customer_camera_status() unit-level proof

def test_customer_camera_status_helper_scopes_purely_by_the_cameras_it_is_given(db_path):
    """Even at the helper level (bypassing the route/cookie layer
    entirely): _customer_camera_status() only ever looks up status for
    the exact camera dicts it's handed -- it has no customer_id
    parameter of its own to get wrong, because the scoping already
    happened one layer up, in _customer_playback_cameras()'s own query."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_customers_five_cameras_each(conn)
        conn.commit()
        conn.close()
        customer_a_cameras = [{"id": f"a-cam-{n}", "name": f"A Camera {n}", "camera_number": n} for n in range(1, 6)]
        result = main._customer_camera_status(customer_a_cameras)
        assert len(result["cameras"]) == 5
        assert all(c["online"] for c in result["cameras"])


# --------------------------------------------------------------- license-count fix stays untouched

def test_license_count_fix_is_unaffected_by_this_change(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_two_customers_five_cameras_each(conn)
        conn.commit()
        conn.close()
        assert main.get_camera_count(customer_id="cust-a") == 5
        assert main.get_camera_count(customer_id="cust-b") == 5
