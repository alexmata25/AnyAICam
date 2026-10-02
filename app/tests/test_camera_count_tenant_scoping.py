"""Regression coverage for a confirmed-live cross-tenant bug: EC2's
get_camera_numbers() (and everything built on it -- get_camera_count(),
license_enforcement_snapshot(), license_warning_banner(),
customer_cloud_usage_snapshot()) queried the shared, multi-tenant
`cameras` table with no customer_id scoping at all. Confirmed live: two
unrelated customers each had 5 cameras of their own (camera_number 1-5
apiece); the unscoped query summed both, producing "10 cameras
configured, but the license permits 5" for one customer, driven
entirely by a second, completely unrelated customer's own cameras.

Fix: get_camera_numbers()/get_camera_count() gained an optional,
additive `customer_id` parameter. Omitted (every existing edge-role
caller), behavior is byte-for-byte unchanged -- the unscoped query
still runs, exactly as before. Provided (the cloud-role customer-facing
call sites this fix updates: page_shell()'s license_warning_banner(),
and customer_cloud_usage_snapshot()'s "configured_cameras"), the query
is scoped to AND customer_id=?, and the legacy CAMERA<n>_HOST env-var
fallback (an edge-only concept) is skipped even on a genuine
zero-cameras result for that one customer.

Deliberately NOT covered here: whether a non-'configured' status
(e.g. pending_installation) should count -- see this fix's own report
for why that's a separate, not-yet-decided product question this patch
does not touch.
"""

import sqlite3

import pytest

from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_camera_count_tenant_scoping.db"


@pytest.fixture(autouse=True)
def _isolated_license_state(tmp_path, monkeypatch):
    # LICENSE_STATE_FILE lives under RECORDINGS_FOLDER, not the
    # sqlite db override_target() already isolates -- redirect it to a
    # throwaway path so these tests never touch the real one on disk.
    monkeypatch.setattr(main, "LICENSE_STATE_FILE", tmp_path / "license_state.json")


def _seed_tenant(conn, customer_id, partner_id="partner-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, f"Customer {customer_id}", f"{customer_id}@example.test", "active", "2026-01-01"),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{customer_id}", customer_id, "Main Site", "2026-01-01"))


def _seed_camera(conn, camera_id, *, customer_id, camera_number, name, status="configured"):
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES(?,?,?,?,?,?,?)",
        (camera_id, customer_id, f"site-{customer_id}", name, status, camera_number, "2026-01-01"),
    )


def _seed_two_customers_five_cameras_each(db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-a")
    _seed_tenant(conn, "cust-b")
    for n in range(1, 6):
        _seed_camera(conn, f"a-cam-{n}", customer_id="cust-a", camera_number=n, name=f"A Camera {n}")
        _seed_camera(conn, f"b-cam-{n}", customer_id="cust-b", camera_number=n, name=f"B Camera {n}")
    conn.commit()
    conn.close()


# --------------------------------------------------------------- get_camera_count() / get_camera_numbers()

def test_customer_a_reports_5_even_though_customer_b_also_has_5(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        _seed_two_customers_five_cameras_each(db_path)
        assert main.get_camera_count(customer_id="cust-a") == 5
        assert main.get_camera_count(customer_id="cust-b") == 5


def test_customer_bs_cameras_never_affect_customer_as_number_list(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        _seed_two_customers_five_cameras_each(db_path)
        assert main.get_camera_numbers(customer_id="cust-a") == [1, 2, 3, 4, 5]
        # Not just the count -- the actual identity of the numbers must
        # never be influenced by a second tenant's own rows either.
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM cameras WHERE customer_id='cust-b'")
        conn.commit()
        conn.close()
        assert main.get_camera_numbers(customer_id="cust-a") == [1, 2, 3, 4, 5]


def test_a_customer_with_genuinely_zero_cameras_reports_zero_not_the_edge_fallback(db_path):
    # The legacy CAMERA<n>_HOST/USERNAME/PASSWORD env-var fallback exists
    # for a brand-new *edge* install with nothing provisioned yet -- it
    # must never apply just because one specific cloud customer_id
    # happens to have zero cameras while OTHER customers have some.
    with override_target(sqlite_path=db_path):
        initialize_database()
        _seed_two_customers_five_cameras_each(db_path)
        assert main.get_camera_numbers(customer_id="cust-c-has-none") == []
        assert main.get_camera_count(customer_id="cust-c-has-none") == 0


def test_edge_role_callers_omitting_customer_id_are_completely_unchanged(db_path):
    # Every existing edge-role call site (dozens, across live view,
    # recording, analytics, etc.) calls get_camera_numbers()/
    # get_camera_count() with no arguments at all -- this is the
    # single-tenant-database behavior those callers have always gotten,
    # reproduced exactly: the unscoped query, summing every row
    # regardless of customer_id (correct on a real edge appliance,
    # whose own database only ever holds one customer's rows to begin
    # with -- this test is proving the *plumbing* is unchanged, using a
    # multi-tenant seed only to make "unscoped" unambiguous to assert on).
    with override_target(sqlite_path=db_path):
        initialize_database()
        _seed_two_customers_five_cameras_each(db_path)
        assert main.get_camera_count() == 10
        assert main.get_camera_numbers() == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]


# --------------------------------------------------------------- license_enforcement_snapshot() / license_warning_banner()

def test_license_enforcement_snapshot_receives_the_scoped_count(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        _seed_two_customers_five_cameras_each(db_path)
        snapshot = main.license_enforcement_snapshot(customer_id="cust-a")
        assert snapshot["current_camera_count"] == 5


def test_license_enforcement_snapshot_without_customer_id_keeps_prior_unscoped_behavior(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        _seed_two_customers_five_cameras_each(db_path)
        snapshot = main.license_enforcement_snapshot()
        assert snapshot["current_camera_count"] == 10


def test_an_explicit_camera_count_argument_still_wins_over_customer_id(db_path):
    # camera_count, when the caller already provides one, must still take
    # precedence exactly as it always has -- customer_id only ever feeds
    # the get_camera_count() fallback used when camera_count is None.
    with override_target(sqlite_path=db_path):
        initialize_database()
        _seed_two_customers_five_cameras_each(db_path)
        snapshot = main.license_enforcement_snapshot(camera_count=2, customer_id="cust-a")
        assert snapshot["current_camera_count"] == 2


def test_license_warning_banner_message_reflects_only_that_customers_cameras(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        _seed_two_customers_five_cameras_each(db_path)
        # A camera_limit of 3 for cust-a (5 real cameras, license permits
        # 3) so the warning path actually renders a message to assert on.
        monkeypatch.setattr(main, "load_license_state", lambda: {**main.default_license_state(), "camera_limit": 3, "status": "active"})
        monkeypatch.setattr(main, "LICENSE_ENFORCEMENT_MODE", "warning")
        banner_a = main.license_warning_banner(customer_id="cust-a")
        assert "5 cameras are configured" in banner_a
        assert "10 cameras are configured" not in banner_a


# --------------------------------------------------------------- customer_cloud_usage_snapshot()

def test_customer_cloud_usage_snapshot_configured_cameras_is_scoped(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        _seed_two_customers_five_cameras_each(db_path)
        usage = main.customer_cloud_usage_snapshot({"customer_id": "cust-a"})
        assert usage["configured_cameras"] == 5


def test_customer_cloud_usage_snapshot_without_customer_id_key_falls_back_unscoped(db_path):
    # A caller whose user dict has no customer_id at all (e.g. today's
    # legacy current_user() anonymous/admin fallback) gets user.get(
    # "customer_id") == None, which reproduces the pre-fix unscoped
    # total -- not a crash, not an empty result.
    with override_target(sqlite_path=db_path):
        initialize_database()
        _seed_two_customers_five_cameras_each(db_path)
        usage = main.customer_cloud_usage_snapshot({})
        assert usage["configured_cameras"] == 10
