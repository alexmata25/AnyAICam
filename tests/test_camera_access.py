"""Per-camera user permissions (app/camera_access.py) -- separate from
per-camera analytics entitlements (customer_analytics_panel.py).

Pure logic tests plus real SQLite tests against the actual schema, same
established pattern as test_dynamic_camera_provisioning.py.
"""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from camera_access import (  # noqa: E402
    authorized_camera_ids,
    filter_authorized_cameras,
    is_camera_authorized,
    remove_camera_access,
    set_camera_access,
)

LIVE_VIEW_SOURCE = (ROOT / "app" / "live_view_page.py").read_text(encoding="utf-8")

SCHEMA = """
CREATE TABLE customers(id TEXT PRIMARY KEY);
CREATE TABLE sites(id TEXT PRIMARY KEY, customer_id TEXT);
CREATE TABLE cameras(id TEXT PRIMARY KEY, customer_id TEXT, site_id TEXT);
CREATE TABLE partner_users(id TEXT PRIMARY KEY, customer_id TEXT, role TEXT, camera_access_mode TEXT NOT NULL DEFAULT 'selected');
CREATE TABLE customer_camera_permissions(user_id TEXT NOT NULL, camera_id TEXT NOT NULL, can_live INTEGER NOT NULL DEFAULT 1, can_playback INTEGER NOT NULL DEFAULT 1, can_download INTEGER NOT NULL DEFAULT 0, can_share INTEGER NOT NULL DEFAULT 0, can_alerts INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(user_id, camera_id));
"""


def fresh_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.execute("INSERT INTO customers VALUES('cust-1')")
    db.execute("INSERT INTO customers VALUES('cust-2')")  # for cross-customer leakage tests
    return db


def make_cameras(db, customer_id, n, *, prefix="cam"):
    for index in range(1, n + 1):
        db.execute("INSERT INTO cameras VALUES(?,?,?)", (f"{prefix}-{index}", customer_id, "site-1"))
    db.commit()


class IsCameraAuthorizedPureTests(unittest.TestCase):
    def test_administrator_is_always_authorized(self):
        self.assertTrue(is_camera_authorized("cam-1", role="administrator", access_mode="selected", permitted_camera_ids=set()))

    def test_customer_owner_is_always_authorized(self):
        self.assertTrue(is_camera_authorized("cam-1", role="customer_owner", access_mode="selected", permitted_camera_ids=set()))

    def test_customer_viewer_all_mode_is_authorized_for_any_camera(self):
        self.assertTrue(is_camera_authorized("cam-99", role="customer_viewer", access_mode="all", permitted_camera_ids=set()))

    def test_customer_viewer_selected_mode_requires_explicit_membership(self):
        self.assertTrue(is_camera_authorized("cam-1", role="customer_viewer", access_mode="selected", permitted_camera_ids={"cam-1"}))
        self.assertFalse(is_camera_authorized("cam-2", role="customer_viewer", access_mode="selected", permitted_camera_ids={"cam-1"}))

    def test_customer_viewer_with_zero_permitted_cameras_is_denied_not_default_allowed(self):
        # The exact fail-open bug this module fixes: a brand new viewer
        # with no rows configured must see nothing, not everything.
        self.assertFalse(is_camera_authorized("cam-1", role="customer_viewer", access_mode="selected", permitted_camera_ids=set()))

    def test_unrecognized_role_is_denied(self):
        self.assertFalse(is_camera_authorized("cam-1", role="installer", access_mode="all", permitted_camera_ids=set()))

    def test_unrecognized_access_mode_fails_closed(self):
        self.assertFalse(is_camera_authorized("cam-1", role="customer_viewer", access_mode="bogus", permitted_camera_ids={"cam-1"}))


class FilterAuthorizedCamerasTests(unittest.TestCase):
    def test_one_of_five_cameras(self):
        result = filter_authorized_cameras(
            ["cam-1", "cam-2", "cam-3", "cam-4", "cam-5"],
            role="customer_viewer", access_mode="selected", permitted_camera_ids={"cam-3"},
        )
        self.assertEqual(result, ["cam-3"])

    def test_three_of_ten_cameras(self):
        all_ids = [f"cam-{n}" for n in range(1, 11)]
        permitted = {"cam-2", "cam-5", "cam-9"}
        result = filter_authorized_cameras(all_ids, role="customer_viewer", access_mode="selected", permitted_camera_ids=permitted)
        self.assertEqual(set(result), permitted)
        self.assertEqual(len(result), 3)

    def test_all_camera_user_gets_every_camera_in_the_input_list(self):
        all_ids = [f"cam-{n}" for n in range(1, 6)]
        result = filter_authorized_cameras(all_ids, role="customer_viewer", access_mode="all", permitted_camera_ids=set())
        self.assertEqual(result, all_ids)


class AuthorizedCameraIdsDbTests(unittest.TestCase):
    def test_all_mode_user_gets_every_camera_for_their_own_customer_only(self):
        db = fresh_db()
        make_cameras(db, "cust-1", 5)
        make_cameras(db, "cust-2", 3, prefix="other")
        db.execute("INSERT INTO partner_users VALUES('user-1','cust-1','customer_viewer','all')")
        db.commit()
        result = authorized_camera_ids(db, user_id="user-1", customer_id="cust-1", role="customer_viewer", access_mode="all")
        self.assertEqual(result, {"cam-1", "cam-2", "cam-3", "cam-4", "cam-5"})

    def test_selected_mode_one_of_five(self):
        db = fresh_db()
        make_cameras(db, "cust-1", 5)
        db.execute("INSERT INTO partner_users VALUES('user-1','cust-1','customer_viewer','selected')")
        db.execute("INSERT INTO customer_camera_permissions(user_id,camera_id) VALUES('user-1','cam-3')")
        db.commit()
        result = authorized_camera_ids(db, user_id="user-1", customer_id="cust-1", role="customer_viewer", access_mode="selected")
        self.assertEqual(result, {"cam-3"})

    def test_selected_mode_three_of_ten(self):
        db = fresh_db()
        make_cameras(db, "cust-1", 10)
        db.execute("INSERT INTO partner_users VALUES('user-1','cust-1','customer_viewer','selected')")
        for camera_id in ("cam-2", "cam-5", "cam-9"):
            db.execute("INSERT INTO customer_camera_permissions(user_id,camera_id) VALUES(?,?)", ("user-1", camera_id))
        db.commit()
        result = authorized_camera_ids(db, user_id="user-1", customer_id="cust-1", role="customer_viewer", access_mode="selected")
        self.assertEqual(result, {"cam-2", "cam-5", "cam-9"})

    def test_no_cross_customer_leakage_even_with_all_mode(self):
        db = fresh_db()
        make_cameras(db, "cust-1", 2)
        make_cameras(db, "cust-2", 4, prefix="other")
        db.execute("INSERT INTO partner_users VALUES('user-1','cust-1','customer_owner','all')")
        db.commit()
        result = authorized_camera_ids(db, user_id="user-1", customer_id="cust-1", role="customer_owner", access_mode="all")
        self.assertEqual(result, {"cam-1", "cam-2"})
        self.assertTrue(all(not camera_id.startswith("other") for camera_id in result))

    def test_new_camera_does_not_auto_expose_to_a_selected_mode_viewer(self):
        db = fresh_db()
        make_cameras(db, "cust-1", 3)
        db.execute("INSERT INTO partner_users VALUES('user-1','cust-1','customer_viewer','selected')")
        db.execute("INSERT INTO customer_camera_permissions(user_id,camera_id) VALUES('user-1','cam-1')")
        db.commit()
        before = authorized_camera_ids(db, user_id="user-1", customer_id="cust-1", role="customer_viewer", access_mode="selected")
        self.assertEqual(before, {"cam-1"})

        # A new camera is added to the customer's fleet -- no permission
        # row is ever created for it automatically.
        db.execute("INSERT INTO cameras VALUES('cam-4','cust-1','site-1')")
        db.commit()

        after = authorized_camera_ids(db, user_id="user-1", customer_id="cust-1", role="customer_viewer", access_mode="selected")
        self.assertEqual(after, {"cam-1"})  # unchanged -- cam-4 not included
        self.assertNotIn("cam-4", after)


class SetAndRemoveCameraAccessTests(unittest.TestCase):
    def test_set_camera_access_selected_replaces_the_full_row_set(self):
        db = fresh_db()
        make_cameras(db, "cust-1", 5)
        db.execute("INSERT INTO partner_users VALUES('user-1','cust-1','customer_viewer','selected')")
        db.execute("INSERT INTO customer_camera_permissions(user_id,camera_id) VALUES('user-1','cam-1')")
        db.commit()

        set_camera_access(db, user_id="user-1", access_mode="selected", camera_ids=["cam-2", "cam-3"], now="2026-01-01T00:00:00")
        db.commit()

        result = authorized_camera_ids(db, user_id="user-1", customer_id="cust-1", role="customer_viewer", access_mode="selected")
        self.assertEqual(result, {"cam-2", "cam-3"})
        self.assertNotIn("cam-1", result)  # old grant is gone, not just added-to

    def test_set_camera_access_all_mode_updates_the_stored_mode(self):
        db = fresh_db()
        make_cameras(db, "cust-1", 3)
        db.execute("INSERT INTO partner_users VALUES('user-1','cust-1','customer_viewer','selected')")
        db.commit()
        set_camera_access(db, user_id="user-1", access_mode="all", camera_ids=[], now="2026-01-01T00:00:00")
        db.commit()
        mode = db.execute("SELECT camera_access_mode FROM partner_users WHERE id='user-1'").fetchone()["camera_access_mode"]
        self.assertEqual(mode, "all")

    def test_removing_one_camera_access_takes_effect_immediately_others_untouched(self):
        db = fresh_db()
        make_cameras(db, "cust-1", 3)
        db.execute("INSERT INTO partner_users VALUES('user-1','cust-1','customer_viewer','selected')")
        for camera_id in ("cam-1", "cam-2"):
            db.execute("INSERT INTO customer_camera_permissions(user_id,camera_id) VALUES(?,?)", ("user-1", camera_id))
        db.commit()

        remove_camera_access(db, user_id="user-1", camera_id="cam-1")
        db.commit()

        result = authorized_camera_ids(db, user_id="user-1", customer_id="cust-1", role="customer_viewer", access_mode="selected")
        self.assertEqual(result, {"cam-2"})
        self.assertFalse(is_camera_authorized("cam-1", role="customer_viewer", access_mode="selected", permitted_camera_ids=result))


class LiveViewEnforcementWiringTests(unittest.TestCase):
    """app/live_view_page.py depends on FastAPI -- static source checks,
    same established pattern as the rest of this suite."""

    def test_authorized_camera_fails_closed_using_is_camera_authorized(self):
        start = LIVE_VIEW_SOURCE.index("def _authorized_camera(")
        body = LIVE_VIEW_SOURCE[start:start + 1600]
        self.assertIn("is_camera_authorized(", body)
        self.assertIn("raise HTTPException(status_code=403", body)
        self.assertNotIn("configured_permissions", body)  # the old fail-open pattern is gone

    def test_camera_access_assignment_route_is_customer_owner_only(self):
        start = LIVE_VIEW_SOURCE.index("def set_user_camera_access")
        body = LIVE_VIEW_SOURCE[start:start + 900]
        self.assertIn("_require_customer_owner(request)", body)

    def test_camera_access_assignment_rejects_cameras_from_another_customer(self):
        start = LIVE_VIEW_SOURCE.index("def set_user_camera_access")
        body = LIVE_VIEW_SOURCE[start:start + 1600]
        self.assertIn("do not belong to this customer", body)

    def test_analytics_assignment_routes_are_customer_owner_only_and_camera_scoped(self):
        for fn_name in ("assign_camera_analytic", "remove_camera_analytic"):
            start = LIVE_VIEW_SOURCE.index(f"def {fn_name}")
            body = LIVE_VIEW_SOURCE[start:start + 700]
            self.assertIn("_require_customer_owner(request)", body)
            self.assertIn("WHERE id=? AND customer_id=?", body)

    def test_analytics_assignment_route_surfaces_license_limit_as_409(self):
        start = LIVE_VIEW_SOURCE.index("def assign_camera_analytic")
        body = LIVE_VIEW_SOURCE[start:start + 1200]
        self.assertIn("except LicenseLimitExceeded as error:", body)
        self.assertIn("status_code=409", body)


if __name__ == "__main__":
    unittest.main()
