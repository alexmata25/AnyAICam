"""Phase 6a (docs/AI_HANDOFF.md Sec 8) wiring tests for:
- the camera_number schema migration in app/db_migrations.py, and
- PUT /api/customer/cameras (app.partner_workspace.configure_customer_cameras()),
  extended to resolve appliance_id from a trusted DB lookup (never the
  browser payload) and assign/clear camera_number via camera_mapping.py.

Named to sort alphabetically AFTER test_cloud_readiness.py on purpose -- see
test_live_view_page_characterization.py's docstring for the full explanation.
Short version: importing partner_workspace pulls in partner_db, whose
initialize_database() only runs on the first import of partner_db in the
process; a file that does this earlier in discovery order than
test_cloud_readiness.py (which deletes/re-initializes its own temp sqlite
file at its own import time) would freeze that first import to a different
temp path and break its migrated-tables assertion. This is the same
pre-existing, documented test-suite fragility (docs/AI_HANDOFF.md §8 Phase 0
finding #2), not something introduced here.

Route-wiring tests use a real, lightweight in-memory SQLite `cameras` table
(no foreign-key enforcement, since this connection bypasses
database_backend.connect()'s PRAGMA foreign_keys=ON) rather than a fully
seeded partners/customers/sites graph -- real enough to exercise the actual
SELECT/UPDATE/conflict logic faithfully (unlike a pure call-recording fake),
without the overhead of the full FK chain. Migration tests use a real
temp-file-backed database via partner_db.initialize_database(), since that
is the one behavior that genuinely needs to be proven end-to-end.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# See test_live_stream_ffmpeg_characterization.py for why ANYAICAM_PARTNER_DB
# is set explicitly here rather than left unset: importing partner_workspace
# pulls in partner_db, which freezes its DB path on first import for the rest
# of the process -- point it at a safe temp path, not a real project path.
os.environ.setdefault("ANYAICAM_DATABASE_BACKEND", "sqlite")
os.environ.setdefault(
    "ANYAICAM_PARTNER_DB",
    str(Path(tempfile.gettempdir()) / "anyaicam-camera-number-wiring-test.db"),
)
os.environ.setdefault("ANYAICAM_ENV", "development")

from fastapi import FastAPI  # noqa: E402

import partner_db  # noqa: E402
import partner_workspace  # noqa: E402
from db_migrations import apply_migrations  # noqa: E402


def _endpoint(app: FastAPI, path: str):
    for candidate_route in app.routes:
        if getattr(candidate_route, "path", None) == path:
            return candidate_route.endpoint
    raise AssertionError(f"route not registered: {path}")


_ROUTE_APP = FastAPI()
partner_workspace.register_partner_workspace_routes(_ROUTE_APP, lambda *_args, **_kwargs: "")
configure_customer_cameras = _endpoint(_ROUTE_APP, "/api/customer/cameras")


class FakeRequest:
    """partner_identity() is mocked in every route-wiring test below, so the
    header/cookie values here are never actually read -- this only exists
    because the route function takes a `request` parameter."""

    def __init__(self):
        self.headers = {}
        self.cookies = {}
        self.client = None


class _ConnectionContext:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self._db

    def __exit__(self, *_args):
        return False


def _make_lightweight_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE cameras(id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, "
        "site_id TEXT NOT NULL, appliance_id TEXT, name TEXT, resolution TEXT, "
        "status TEXT, created_at TEXT NOT NULL, camera_number INTEGER)"
    )
    db.execute(
        "CREATE UNIQUE INDEX idx_cameras_appliance_camera_number "
        "ON cameras(appliance_id, camera_number) WHERE camera_number IS NOT NULL"
    )
    return db


def _insert_camera(db, camera_id, *, customer_id="cust-1", site_id="site-1", appliance_id="app-1", camera_number=None, name="Camera"):
    db.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,resolution,status,created_at,camera_number) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (camera_id, customer_id, site_id, appliance_id, name, "2mp", "discovered", "2026-08-14T00:00:00", camera_number),
    )


IDENTITY = {"role": "customer_owner", "customer_id": "cust-1", "email": "owner@example.com"}


class ConfigureCustomerCamerasWiringTests(unittest.TestCase):
    def setUp(self):
        self.db = _make_lightweight_db()
        self._patches = [
            patch.object(partner_workspace, "partner_identity", return_value=IDENTITY),
            patch.object(partner_workspace, "connection", return_value=_ConnectionContext(self.db)),
            patch.object(partner_workspace, "audit"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def _camera_number(self, camera_id):
        row = self.db.execute("SELECT camera_number FROM cameras WHERE id=?", (camera_id,)).fetchone()
        return row["camera_number"] if row else None

    def test_assigns_camera_number_for_owned_camera(self):
        _insert_camera(self.db, "cam-1", customer_id="cust-1", appliance_id="app-1")
        result = configure_customer_cameras(
            FakeRequest(),
            {"cameras": [{"id": "cam-1", "name": "Front Door", "site_id": "site-1", "camera_number": 3}]},
        )
        self.assertEqual(result["errors"], [])
        self.assertEqual(self._camera_number("cam-1"), 3)

    def test_camera_number_ignored_for_camera_belonging_to_another_customer(self):
        # The pre-existing ownership check (`WHERE id=? AND customer_id=?`)
        # already silently skips the whole item for a camera the caller
        # doesn't own -- confirm camera_number specifically respects this too,
        # and that appliance_id is never sourced from the payload here (the
        # payload doesn't even carry one -- it's irrelevant, since the DB
        # lookup that would supply it never matches in the first place).
        _insert_camera(self.db, "cam-1", customer_id="cust-OTHER", appliance_id="app-1", camera_number=None)
        result = configure_customer_cameras(
            FakeRequest(),
            {"cameras": [{"id": "cam-1", "name": "Hijacked", "site_id": "site-1", "camera_number": 9}]},
        )
        self.assertEqual(result["errors"], [])
        self.assertIsNone(self._camera_number("cam-1"))
        # Confirm the other owned fields were not touched either -- the whole
        # item was skipped, not partially applied.
        name = self.db.execute("SELECT name FROM cameras WHERE id=?", ("cam-1",)).fetchone()["name"]
        self.assertEqual(name, "Camera")

    def test_duplicate_camera_number_reported_as_error_without_blocking_other_items(self):
        _insert_camera(self.db, "cam-1", appliance_id="app-1", camera_number=2, name="Existing")
        _insert_camera(self.db, "cam-2", appliance_id="app-1", camera_number=None)
        result = configure_customer_cameras(
            FakeRequest(),
            {
                "cameras": [
                    {"id": "cam-1", "name": "Renamed", "site_id": "site-1", "status": "configured"},
                    {"id": "cam-2", "name": "Camera", "site_id": "site-1", "camera_number": 2},
                ]
            },
        )
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["camera_id"], "cam-2")
        self.assertIn("errors", result["message"].lower())
        # Batch isolation: cam-1's unrelated rename still succeeded even
        # though cam-2's camera_number assignment conflicted.
        renamed = self.db.execute("SELECT name FROM cameras WHERE id=?", ("cam-1",)).fetchone()["name"]
        self.assertEqual(renamed, "Renamed")
        # cam-2's rejected assignment was not partially applied.
        self.assertIsNone(self._camera_number("cam-2"))
        self.assertEqual(self._camera_number("cam-1"), 2)

    def test_omitting_camera_number_leaves_it_untouched(self):
        _insert_camera(self.db, "cam-1", camera_number=5)
        result = configure_customer_cameras(
            FakeRequest(),
            {"cameras": [{"id": "cam-1", "name": "Renamed Only", "site_id": "site-1"}]},
        )
        self.assertEqual(result["errors"], [])
        self.assertEqual(self._camera_number("cam-1"), 5)


class CameraNumberMigrationTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory(prefix="anyaicam-camera-number-migration-")
        self._db_path = Path(self._tempdir.name) / "migration-test.db"
        self._env_patch = patch.dict(
            os.environ,
            {"ANYAICAM_PARTNER_DB": str(self._db_path), "ANYAICAM_DATABASE_BACKEND": "sqlite"},
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._tempdir.cleanup()

    def test_apply_migrations_twice_is_idempotent_and_adds_camera_number_once(self):
        partner_db.initialize_database()  # runs apply_migrations() once internally
        apply_migrations()  # explicit second run must not raise
        with partner_db.connection() as db:
            columns = [item["name"] for item in db.execute("PRAGMA table_info(cameras)").fetchall()]
        self.assertEqual(columns.count("camera_number"), 1)

    def test_unique_partial_index_created_on_cameras(self):
        partner_db.initialize_database()
        with partner_db.connection() as db:
            index_names = {item["name"] for item in db.execute("PRAGMA index_list(cameras)").fetchall()}
        self.assertIn("idx_cameras_appliance_camera_number", index_names)


if __name__ == "__main__":
    unittest.main()
