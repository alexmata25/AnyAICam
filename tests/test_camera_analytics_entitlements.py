"""Per-camera analytics entitlements (not per-site).

Real behavioral tests against an in-memory SQLite database using the
actual schema (mirroring db_migrations.py), and the real, unmodified
app/customer_analytics_panel.py functions -- same established pattern as
tests/test_dynamic_camera_provisioning.py.
"""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from customer_analytics_panel import (  # noqa: E402
    ANALYTIC_KEYS,
    LicenseLimitExceeded,
    analytics_row_state,
    assign_entitlement,
    camera_entitlement_rows,
    enabled_analytics,
    licensed_quantity_exceeded,
    remove_entitlement,
)

SCHEMA = """
CREATE TABLE customers(id TEXT PRIMARY KEY);
CREATE TABLE sites(id TEXT PRIMARY KEY, customer_id TEXT);
CREATE TABLE cameras(id TEXT PRIMARY KEY, customer_id TEXT, site_id TEXT, name TEXT);
CREATE TABLE analytics_subscriptions(id TEXT PRIMARY KEY, customer_id TEXT, site_id TEXT, analytic_key TEXT, status TEXT, licensed_quantity INTEGER NOT NULL DEFAULT 1);
CREATE TABLE camera_analytics_entitlements(
    camera_id TEXT NOT NULL, analytic_key TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(camera_id, analytic_key)
);
"""


def fresh_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.execute("INSERT INTO customers VALUES('cust-1')")
    db.execute("INSERT INTO sites VALUES('site-1','cust-1')")
    return db


def make_cameras(db, n):
    for index in range(1, n + 1):
        db.execute("INSERT INTO cameras VALUES(?,?,?,?)", (f"cam-{index}", "cust-1", "site-1", f"Camera {index}"))
    db.commit()


def license(db, analytic_key, quantity, *, sub_id="sub-auto"):
    db.execute(
        "INSERT INTO analytics_subscriptions VALUES(?,?,?,?,?,?)",
        (sub_id, "cust-1", "site-1", analytic_key, "active", quantity),
    )


class TenCameraMixedAssignmentTests(unittest.TestCase):
    """The exact scenario from the requirement: one site, 10 cameras,
    LPR on 2, People Counting on 4 (overlapping with one LPR camera),
    PPE on 1, and several with nothing."""

    def setUp(self):
        self.db = fresh_db()
        make_cameras(self.db, 10)
        license(self.db, "lpr", 2, sub_id="sub-lpr")
        license(self.db, "people_counting", 4, sub_id="sub-pc")
        license(self.db, "ppe", 1, sub_id="sub-ppe")
        self.db.commit()
        now = "2026-01-01T00:00:00"
        assign_entitlement(self.db, "cam-1", "lpr", now=now)
        assign_entitlement(self.db, "cam-2", "lpr", now=now)
        for camera_id in ("cam-2", "cam-3", "cam-4", "cam-5"):
            assign_entitlement(self.db, camera_id, "people_counting", now=now)
        assign_entitlement(self.db, "cam-6", "ppe", now=now)
        self.db.commit()

    def _enabled(self, camera_id):
        return enabled_analytics(camera_entitlement_rows(self.db, camera_id))

    def test_lpr_is_enabled_on_exactly_cameras_1_and_2(self):
        for camera_id in ("cam-1", "cam-2"):
            self.assertIn("lpr", self._enabled(camera_id))
        for camera_id in ("cam-3", "cam-4", "cam-5", "cam-6", "cam-7", "cam-8", "cam-9", "cam-10"):
            self.assertNotIn("lpr", self._enabled(camera_id))

    def test_people_counting_is_enabled_on_exactly_cameras_2_through_5(self):
        for camera_id in ("cam-2", "cam-3", "cam-4", "cam-5"):
            self.assertIn("people_counting", self._enabled(camera_id))
        for camera_id in ("cam-1", "cam-6", "cam-7", "cam-8", "cam-9", "cam-10"):
            self.assertNotIn("people_counting", self._enabled(camera_id))

    def test_cameras_6_through_10_have_no_analytics_except_camera_6s_ppe(self):
        self.assertEqual(self._enabled("cam-6"), ["ppe"])
        for camera_id in ("cam-7", "cam-8", "cam-9", "cam-10"):
            self.assertEqual(self._enabled(camera_id), [])

    def test_camera_2_has_multiple_analytics_lpr_and_people_counting(self):
        self.assertEqual(set(self._enabled("cam-2")), {"lpr", "people_counting"})

    def test_no_cross_camera_leakage_camera_1s_lpr_does_not_appear_on_camera_3(self):
        self.assertIn("lpr", self._enabled("cam-1"))
        self.assertNotIn("lpr", self._enabled("cam-3"))

    def test_focused_live_row_state_is_correct_for_every_camera_in_the_fleet(self):
        expected = {
            "cam-1": {"lpr"},
            "cam-2": {"lpr", "people_counting"},
            "cam-3": {"people_counting"},
            "cam-4": {"people_counting"},
            "cam-5": {"people_counting"},
            "cam-6": {"ppe"},
            "cam-7": set(), "cam-8": set(), "cam-9": set(), "cam-10": set(),
        }
        for camera_id, expected_enabled in expected.items():
            state = analytics_row_state(camera_entitlement_rows(self.db, camera_id))
            actual_enabled = {item["key"] for item in state if item["enabled"]}
            self.assertEqual(actual_enabled, expected_enabled, camera_id)
            # every camera still gets all four pills back, real or upgrade
            self.assertEqual(len(state), len(ANALYTIC_KEYS), camera_id)


class EntitlementRemovalTests(unittest.TestCase):
    def test_removing_an_entitlement_disables_it_for_that_camera_only(self):
        db = fresh_db()
        make_cameras(db, 2)
        license(db, "lpr", 5)
        db.commit()
        now = "2026-01-01T00:00:00"
        assign_entitlement(db, "cam-1", "lpr", now=now)
        assign_entitlement(db, "cam-2", "lpr", now=now)
        db.commit()
        self.assertIn("lpr", enabled_analytics(camera_entitlement_rows(db, "cam-1")))

        remove_entitlement(db, "cam-1", "lpr", now="2026-01-02T00:00:00")
        db.commit()

        self.assertNotIn("lpr", enabled_analytics(camera_entitlement_rows(db, "cam-1")))
        self.assertIn("lpr", enabled_analytics(camera_entitlement_rows(db, "cam-2")))  # untouched

    def test_removal_is_a_soft_status_change_the_row_still_exists(self):
        db = fresh_db()
        make_cameras(db, 1)
        license(db, "ppe", 5)
        db.commit()
        assign_entitlement(db, "cam-1", "ppe", now="2026-01-01T00:00:00")
        remove_entitlement(db, "cam-1", "ppe", now="2026-01-02T00:00:00")
        db.commit()
        row = db.execute("SELECT status FROM camera_analytics_entitlements WHERE camera_id='cam-1' AND analytic_key='ppe'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "cancelled")

    def test_reassigning_after_removal_re_enables_it(self):
        db = fresh_db()
        make_cameras(db, 1)
        license(db, "smart_motion", 5)
        db.commit()
        assign_entitlement(db, "cam-1", "smart_motion", now="2026-01-01T00:00:00")
        remove_entitlement(db, "cam-1", "smart_motion", now="2026-01-02T00:00:00")
        assign_entitlement(db, "cam-1", "smart_motion", now="2026-01-03T00:00:00")
        db.commit()
        self.assertIn("smart_motion", enabled_analytics(camera_entitlement_rows(db, "cam-1")))


class AssignEntitlementValidationTests(unittest.TestCase):
    def test_assigning_an_unknown_analytic_key_raises(self):
        db = fresh_db()
        make_cameras(db, 1)
        with self.assertRaises(ValueError):
            assign_entitlement(db, "cam-1", "not_a_real_analytic", now="2026-01-01T00:00:00")

    def test_assigning_the_same_entitlement_twice_is_idempotent(self):
        db = fresh_db()
        make_cameras(db, 1)
        license(db, "lpr", 5)
        db.commit()
        assign_entitlement(db, "cam-1", "lpr", now="2026-01-01T00:00:00")
        assign_entitlement(db, "cam-1", "lpr", now="2026-01-02T00:00:00")
        db.commit()
        rows = db.execute("SELECT * FROM camera_analytics_entitlements WHERE camera_id='cam-1' AND analytic_key='lpr'").fetchall()
        self.assertEqual(len(rows), 1)


class SiteLevelSubscriptionDoesNotLeakToUnentitledCamerasTests(unittest.TestCase):
    """The explicit requirement: do not infer that every camera at a site
    has every purchased (site-level) analytic."""

    def test_a_site_level_purchase_alone_enables_nothing_on_any_camera(self):
        db = fresh_db()
        make_cameras(db, 3)
        license(db, "lpr", 5)
        db.commit()
        # No camera_analytics_entitlements row was ever created for any
        # camera -- the site-level subscription by itself must not turn
        # on LPR anywhere.
        for camera_id in ("cam-1", "cam-2", "cam-3"):
            self.assertEqual(enabled_analytics(camera_entitlement_rows(db, camera_id)), [])


class ZeroAnalyticsCameraTests(unittest.TestCase):
    def test_a_camera_with_no_entitlements_shows_all_four_as_upgrade_opportunities(self):
        db = fresh_db()
        make_cameras(db, 1)
        state = analytics_row_state(camera_entitlement_rows(db, "cam-1"))
        self.assertEqual(len(state), len(ANALYTIC_KEYS))
        self.assertTrue(all(item["enabled"] is False for item in state))


class LicensedQuantityLimitTests(unittest.TestCase):
    """Billing authority: assign_entitlement() must never let a camera-
    level entitlement exceed what analytics_subscriptions says was
    actually purchased. subscription = what they bought; camera
    entitlement = where they use it."""

    def test_licensed_quantity_exceeded_pure_logic(self):
        self.assertFalse(licensed_quantity_exceeded(licensed_quantity=2, currently_entitled_count=1, already_entitled=False))
        self.assertTrue(licensed_quantity_exceeded(licensed_quantity=2, currently_entitled_count=2, already_entitled=False))
        # re-assigning a camera that already has it never counts against the limit
        self.assertFalse(licensed_quantity_exceeded(licensed_quantity=2, currently_entitled_count=2, already_entitled=True))

    def test_two_purchased_lpr_licenses_allow_exactly_two_cameras(self):
        db = fresh_db()
        make_cameras(db, 3)
        license(db, "lpr", 2)
        db.commit()
        now = "2026-01-01T00:00:00"
        assign_entitlement(db, "cam-1", "lpr", now=now)
        assign_entitlement(db, "cam-2", "lpr", now=now)
        db.commit()
        with self.assertRaises(LicenseLimitExceeded):
            assign_entitlement(db, "cam-3", "lpr", now=now)
        self.assertNotIn("lpr", enabled_analytics(camera_entitlement_rows(db, "cam-3")))

    def test_no_subscription_at_all_means_zero_licensed_seats(self):
        db = fresh_db()
        make_cameras(db, 1)
        db.commit()
        with self.assertRaises(LicenseLimitExceeded):
            assign_entitlement(db, "cam-1", "lpr", now="2026-01-01T00:00:00")

    def test_freeing_a_seat_by_removal_allows_a_different_camera_to_use_it(self):
        db = fresh_db()
        make_cameras(db, 3)
        license(db, "lpr", 1)
        db.commit()
        now = "2026-01-01T00:00:00"
        assign_entitlement(db, "cam-1", "lpr", now=now)
        db.commit()
        with self.assertRaises(LicenseLimitExceeded):
            assign_entitlement(db, "cam-2", "lpr", now=now)

        remove_entitlement(db, "cam-1", "lpr", now="2026-01-02T00:00:00")
        db.commit()
        assign_entitlement(db, "cam-2", "lpr", now="2026-01-03T00:00:00")  # no longer raises
        db.commit()
        self.assertIn("lpr", enabled_analytics(camera_entitlement_rows(db, "cam-2")))

    def test_direct_db_state_cannot_create_analytics_the_customer_did_not_purchase(self):
        # assign_entitlement() is the only supported path, and it always
        # checks the subscription record -- there is no way through this
        # function to grant an analytic with zero purchased (non-
        # cancelled) seats.
        db = fresh_db()
        make_cameras(db, 1)
        license(db, "ppe", 5, sub_id="cancelled-sub")
        db.execute("UPDATE analytics_subscriptions SET status='cancelled' WHERE id='cancelled-sub'")
        db.commit()
        with self.assertRaises(LicenseLimitExceeded):
            assign_entitlement(db, "cam-1", "ppe", now="2026-01-01T00:00:00")


if __name__ == "__main__":
    unittest.main()
