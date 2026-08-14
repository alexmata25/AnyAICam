"""Phase 6a (docs/AI_HANDOFF.md Sec 8): focused unit tests for
app/camera_mapping.py's resolve_camera_number()/assign_camera_number().

Deliberately does NOT import `main` or `partner_db` -- camera_mapping.py
itself takes an already-open `db` connection rather than opening its own, so
these tests build their own throwaway in-memory SQLite `cameras` table
directly. This sidesteps the documented test-discovery-order DB fragility
(see test_live_view_page_characterization.py's docstring) entirely rather
than working around it by filename, since nothing here ever imports `main`.
"""

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camera_mapping import (
    CameraNumberConflict,
    assign_camera_number,
    resolve_camera_number,
)


def _make_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE cameras(id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, "
        "site_id TEXT NOT NULL, appliance_id TEXT, name TEXT, resolution TEXT, "
        "status TEXT, created_at TEXT NOT NULL, camera_number INTEGER)"
    )
    # Mirrors the exact partial unique index added in app/db_migrations.py.
    db.execute(
        "CREATE UNIQUE INDEX idx_cameras_appliance_camera_number "
        "ON cameras(appliance_id, camera_number) WHERE camera_number IS NOT NULL"
    )
    return db


def _insert_camera(db, camera_id, *, customer_id="cust-1", appliance_id="app-1", camera_number=None):
    db.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,resolution,status,created_at,camera_number) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (camera_id, customer_id, "site-1", appliance_id, "Camera", "2mp", "discovered", "2026-08-14T00:00:00", camera_number),
    )


class ResolveCameraNumberTests(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()

    def test_assigned_camera_resolves_to_its_camera_number(self):
        _insert_camera(self.db, "cam-1", camera_number=3)
        self.assertEqual(resolve_camera_number(self.db, "cam-1", "app-1", "cust-1"), 3)

    def test_unassigned_camera_resolves_to_none(self):
        _insert_camera(self.db, "cam-1", camera_number=None)
        self.assertIsNone(resolve_camera_number(self.db, "cam-1", "app-1", "cust-1"))

    def test_nonexistent_camera_id_resolves_to_none(self):
        self.assertIsNone(resolve_camera_number(self.db, "does-not-exist", "app-1", "cust-1"))

    def test_wrong_appliance_id_resolves_to_none(self):
        _insert_camera(self.db, "cam-1", appliance_id="app-1", camera_number=3)
        self.assertIsNone(resolve_camera_number(self.db, "cam-1", "app-OTHER", "cust-1"))

    def test_wrong_customer_id_resolves_to_none(self):
        _insert_camera(self.db, "cam-1", customer_id="cust-1", camera_number=3)
        self.assertIsNone(resolve_camera_number(self.db, "cam-1", "app-1", "cust-OTHER"))

    def test_reassigned_camera_resolves_to_new_value_after_change(self):
        _insert_camera(self.db, "cam-1", camera_number=3)
        self.db.execute("UPDATE cameras SET camera_number=5 WHERE id=?", ("cam-1",))
        self.assertEqual(resolve_camera_number(self.db, "cam-1", "app-1", "cust-1"), 5)

    def test_wrong_appliance_and_wrong_customer_both_indistinguishable_from_unassigned(self):
        _insert_camera(self.db, "cam-1", camera_number=3)
        _insert_camera(self.db, "cam-2", camera_number=None)
        wrong_appliance = resolve_camera_number(self.db, "cam-1", "app-OTHER", "cust-1")
        wrong_customer = resolve_camera_number(self.db, "cam-1", "app-1", "cust-OTHER")
        genuinely_unassigned = resolve_camera_number(self.db, "cam-2", "app-1", "cust-1")
        self.assertEqual(wrong_appliance, wrong_customer)
        self.assertEqual(wrong_appliance, genuinely_unassigned)
        self.assertIsNone(wrong_appliance)


class AssignCameraNumberTests(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()

    def test_assign_valid_camera_number_succeeds(self):
        _insert_camera(self.db, "cam-1")
        assign_camera_number(self.db, "cam-1", 7, appliance_id="app-1", customer_id="cust-1")
        self.assertEqual(resolve_camera_number(self.db, "cam-1", "app-1", "cust-1"), 7)

    def test_assign_none_clears_existing_camera_number(self):
        _insert_camera(self.db, "cam-1", camera_number=7)
        assign_camera_number(self.db, "cam-1", None, appliance_id="app-1", customer_id="cust-1")
        self.assertIsNone(resolve_camera_number(self.db, "cam-1", "app-1", "cust-1"))

    def test_assign_out_of_range_camera_number_raises_value_error(self):
        _insert_camera(self.db, "cam-1")
        for bad in (0, -1, 257):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    assign_camera_number(self.db, "cam-1", bad, appliance_id="app-1", customer_id="cust-1")

    def test_assign_non_integer_camera_number_raises_value_error(self):
        _insert_camera(self.db, "cam-1")
        for bad in ("abc", 1.5, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    assign_camera_number(self.db, "cam-1", bad, appliance_id="app-1", customer_id="cust-1")

    def test_assign_to_nonexistent_camera_id_raises_lookup_error(self):
        with self.assertRaises(LookupError):
            assign_camera_number(self.db, "does-not-exist", 1, appliance_id="app-1", customer_id="cust-1")

    def test_assign_with_wrong_appliance_id_raises_lookup_error(self):
        _insert_camera(self.db, "cam-1", appliance_id="app-1")
        with self.assertRaises(LookupError):
            assign_camera_number(self.db, "cam-1", 1, appliance_id="app-OTHER", customer_id="cust-1")

    def test_assign_with_wrong_customer_id_raises_lookup_error(self):
        _insert_camera(self.db, "cam-1", customer_id="cust-1")
        with self.assertRaises(LookupError):
            assign_camera_number(self.db, "cam-1", 1, appliance_id="app-1", customer_id="cust-OTHER")

    def test_assign_duplicate_camera_number_on_same_appliance_raises_conflict(self):
        _insert_camera(self.db, "cam-1", appliance_id="app-1", camera_number=2)
        _insert_camera(self.db, "cam-2", appliance_id="app-1", camera_number=None)
        with self.assertRaises(CameraNumberConflict):
            assign_camera_number(self.db, "cam-2", 2, appliance_id="app-1", customer_id="cust-1")

    def test_assign_same_camera_number_to_different_appliance_succeeds(self):
        _insert_camera(self.db, "cam-1", appliance_id="app-1", camera_number=2)
        _insert_camera(self.db, "cam-2", appliance_id="app-2", camera_number=None)
        assign_camera_number(self.db, "cam-2", 2, appliance_id="app-2", customer_id="cust-1")
        self.assertEqual(resolve_camera_number(self.db, "cam-2", "app-2", "cust-1"), 2)

    def test_reassign_camera_number_to_a_new_value_succeeds_and_frees_old_value(self):
        _insert_camera(self.db, "cam-1", appliance_id="app-1", camera_number=2)
        _insert_camera(self.db, "cam-2", appliance_id="app-1", camera_number=None)
        assign_camera_number(self.db, "cam-1", 9, appliance_id="app-1", customer_id="cust-1")
        # Old number (2) is now free -- assigning it to a different camera on
        # the same appliance must succeed, not conflict with the old holder.
        assign_camera_number(self.db, "cam-2", 2, appliance_id="app-1", customer_id="cust-1")
        self.assertEqual(resolve_camera_number(self.db, "cam-1", "app-1", "cust-1"), 9)
        self.assertEqual(resolve_camera_number(self.db, "cam-2", "app-1", "cust-1"), 2)

    def test_camera_with_null_appliance_id_cannot_be_assigned(self):
        _insert_camera(self.db, "cam-1", appliance_id=None)
        with self.assertRaises(ValueError):
            assign_camera_number(self.db, "cam-1", 1, appliance_id=None, customer_id="cust-1")

    def test_db_unique_index_backstops_a_race(self):
        # Simulates two concurrent assigns bypassing the app-layer conflict
        # check entirely (raw SQL, not assign_camera_number()) -- proves the
        # partial unique index itself is the real backstop, not just the
        # Python-level pre-check.
        _insert_camera(self.db, "cam-1", appliance_id="app-1", camera_number=None)
        _insert_camera(self.db, "cam-2", appliance_id="app-1", camera_number=None)
        self.db.execute("UPDATE cameras SET camera_number=4 WHERE id=?", ("cam-1",))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE cameras SET camera_number=4 WHERE id=?", ("cam-2",))


if __name__ == "__main__":
    unittest.main()
