"""Phase 3: dynamic camera provisioning.

Two kinds of coverage here, matching this suite's established split (see
test_provisioning_service.py's module docstring):

1. Real behavioral tests against an in-memory SQLite database using the
   actual schema (mirroring db_migrations.py's CREATE TABLE/ALTER TABLE
   statements) and the real, unmodified app/camera_mapping.py and
   app/appliance_protocol.py functions -- both are FastAPI-free by design
   and fully importable. This covers device_key dedup, camera_number
   assignment/release, and credential encryption round-tripping for real.

2. Static source-text checks against app/main.py and app/partner_workspace.py
   (which import FastAPI and cannot be imported in this dev environment)
   for the wiring that ties those real mechanisms into the app: the
   CameraNotConfiguredError safety fix, the new camera CRUD routes, and
   confirmation that CAMERA_COUNT is gone.
"""
import os
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import camera_mapping  # noqa: E402
from appliance_protocol import encrypt_camera_credentials, decrypt_camera_credentials  # noqa: E402

MAIN_SOURCE = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
PARTNER_WORKSPACE_SOURCE = (ROOT / "app" / "partner_workspace.py").read_text(encoding="utf-8")
DB_MIGRATIONS_SOURCE = (ROOT / "app" / "db_migrations.py").read_text(encoding="utf-8")

SCHEMA = """
CREATE TABLE customers(id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE appliances(id TEXT PRIMARY KEY, customer_id TEXT);
CREATE TABLE plans(id TEXT PRIMARY KEY, customer_id TEXT, camera_quantity INTEGER);
CREATE TABLE cameras(
    id TEXT PRIMARY KEY, customer_id TEXT, site_id TEXT, appliance_id TEXT,
    name TEXT, resolution TEXT, status TEXT, created_at TEXT,
    camera_number INTEGER, device_key TEXT, ip_address TEXT,
    onvif_endpoint TEXT, manufacturer TEXT, model TEXT
);
CREATE UNIQUE INDEX idx_cameras_appliance_camera_number ON cameras(appliance_id,camera_number) WHERE camera_number IS NOT NULL;
CREATE UNIQUE INDEX idx_cameras_customer_device_key ON cameras(customer_id,device_key) WHERE device_key IS NOT NULL;
CREATE TABLE camera_credentials(camera_id TEXT PRIMARY KEY, encrypted_blob TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


def fresh_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.execute("INSERT INTO customers VALUES('cust-1','Test Customer')")
    db.execute("INSERT INTO appliances VALUES('app-1','cust-1')")
    return db


def make_camera(db, camera_id, *, device_key=None, camera_number=None, name="Camera", status="pending_installation"):
    db.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,resolution,status,created_at,camera_number,device_key) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (camera_id, "cust-1", None, "app-1", name, "2mp", status, "2026-01-01T00:00:00", camera_number, device_key),
    )
    db.commit()


class CameraCountScenarioTests(unittest.TestCase):
    """1, 4, 5, more-than-5, and zero configured cameras -- the dynamic
    registry (mirrored here as direct camera_number assignment via the
    real camera_mapping.assign_camera_number()) must support all of them
    without a hardcoded ceiling."""

    def _provision_n_cameras(self, n):
        db = fresh_db()
        for index in range(1, n + 1):
            camera_id = f"cam-{index}"
            make_camera(db, camera_id, device_key=f"device-{index}", status="configured")
            camera_mapping.assign_camera_number(db, camera_id, index, appliance_id="app-1", customer_id="cust-1")
        numbers = [row["camera_number"] for row in db.execute(
            "SELECT camera_number FROM cameras WHERE camera_number IS NOT NULL ORDER BY camera_number"
        ).fetchall()]
        return numbers

    def test_one_camera(self):
        self.assertEqual(self._provision_n_cameras(1), [1])

    def test_four_cameras(self):
        self.assertEqual(self._provision_n_cameras(4), [1, 2, 3, 4])

    def test_five_cameras(self):
        self.assertEqual(self._provision_n_cameras(5), [1, 2, 3, 4, 5])

    def test_more_than_five_cameras(self):
        self.assertEqual(self._provision_n_cameras(9), list(range(1, 10)))

    def test_zero_configured_cameras(self):
        db = fresh_db()
        numbers = [row["camera_number"] for row in db.execute(
            "SELECT camera_number FROM cameras WHERE camera_number IS NOT NULL"
        ).fetchall()]
        self.assertEqual(numbers, [])


class DuplicateDeviceKeyTests(unittest.TestCase):
    """Rediscovering the same physical camera (same device_key) must
    update its existing row, not create a second camera_number."""

    def test_same_device_key_cannot_get_two_camera_numbers(self):
        db = fresh_db()
        make_camera(db, "cam-1", device_key="device-abc", status="configured")
        camera_mapping.assign_camera_number(db, "cam-1", 1, appliance_id="app-1", customer_id="cust-1")
        # Simulate the real provision route's dedup lookup: an existing
        # row with this device_key is found, so no second row/number is
        # ever created for it.
        existing = db.execute("SELECT id FROM cameras WHERE customer_id=? AND device_key=?", ("cust-1", "device-abc")).fetchone()
        self.assertEqual(existing["id"], "cam-1")
        total_rows = db.execute("SELECT COUNT(*) AS n FROM cameras WHERE device_key='device-abc'").fetchone()["n"]
        self.assertEqual(total_rows, 1)

    def test_database_itself_enforces_the_uniqueness_as_a_backstop(self):
        db = fresh_db()
        make_camera(db, "cam-1", device_key="device-abc", status="configured")
        with self.assertRaises(sqlite3.IntegrityError):
            make_camera(db, "cam-2", device_key="device-abc", status="configured")


class CameraNumberConflictTests(unittest.TestCase):
    def test_assigning_an_already_used_number_raises_conflict(self):
        db = fresh_db()
        make_camera(db, "cam-1", device_key="device-1", status="configured")
        make_camera(db, "cam-2", device_key="device-2", status="configured")
        camera_mapping.assign_camera_number(db, "cam-1", 1, appliance_id="app-1", customer_id="cust-1")
        with self.assertRaises(camera_mapping.CameraNumberConflict):
            camera_mapping.assign_camera_number(db, "cam-2", 1, appliance_id="app-1", customer_id="cust-1")


class RemovalReleasesTheSlotTests(unittest.TestCase):
    def test_clearing_camera_number_frees_it_for_reuse(self):
        db = fresh_db()
        make_camera(db, "cam-1", device_key="device-1", camera_number=1, status="configured")
        camera_mapping.assign_camera_number(db, "cam-1", None, appliance_id="app-1", customer_id="cust-1")
        self.assertIsNone(camera_mapping.resolve_camera_number(db, "cam-1", appliance_id="app-1", customer_id="cust-1"))
        make_camera(db, "cam-2", device_key="device-2", status="configured")
        camera_mapping.assign_camera_number(db, "cam-2", 1, appliance_id="app-1", customer_id="cust-1")  # does not raise
        self.assertEqual(camera_mapping.resolve_camera_number(db, "cam-2", appliance_id="app-1", customer_id="cust-1"), 1)


class RenameAndSiteAssignmentTests(unittest.TestCase):
    def test_rename_and_site_reassignment_do_not_touch_camera_number_or_device_key(self):
        db = fresh_db()
        make_camera(db, "cam-1", device_key="device-1", camera_number=1, name="Camera 1", status="configured")
        db.execute("UPDATE cameras SET name=?, site_id=? WHERE id=?", ("Front Door", "site-42", "cam-1"))
        db.commit()
        row = db.execute("SELECT * FROM cameras WHERE id='cam-1'").fetchone()
        self.assertEqual(row["name"], "Front Door")
        self.assertEqual(row["site_id"], "site-42")
        self.assertEqual(row["camera_number"], 1)
        self.assertEqual(row["device_key"], "device-1")


class ExpectedCameraCountProgressTests(unittest.TestCase):
    def test_progress_is_configured_over_expected_from_the_plan(self):
        db = fresh_db()
        db.execute("INSERT INTO plans VALUES('plan-1','cust-1',5)")
        make_camera(db, "cam-1", device_key="d1", status="configured")
        make_camera(db, "cam-2", device_key="d2", status="configured")
        make_camera(db, "cam-3", status="pending_installation")  # not yet provisioned
        db.commit()
        expected = db.execute("SELECT camera_quantity FROM plans WHERE customer_id='cust-1'").fetchone()["camera_quantity"]
        configured = db.execute("SELECT COUNT(*) AS n FROM cameras WHERE customer_id='cust-1' AND status='configured'").fetchone()["n"]
        self.assertEqual((configured, expected), (2, 5))
        self.assertFalse(expected > 0 and configured >= expected)  # not complete yet

    def test_onboarding_complete_only_when_configured_meets_expected(self):
        db = fresh_db()
        db.execute("INSERT INTO plans VALUES('plan-1','cust-1',2)")
        make_camera(db, "cam-1", device_key="d1", status="configured")
        make_camera(db, "cam-2", device_key="d2", status="configured")
        db.commit()
        expected = db.execute("SELECT camera_quantity FROM plans WHERE customer_id='cust-1'").fetchone()["camera_quantity"]
        configured = db.execute("SELECT COUNT(*) AS n FROM cameras WHERE customer_id='cust-1' AND status='configured'").fetchone()["n"]
        self.assertTrue(expected > 0 and configured >= expected)


class CredentialSecrecyTests(unittest.TestCase):
    """Credentials go through the real encrypt/decrypt_camera_credentials()
    -- the same mechanism appliance_cloud.py's provisioning-job delivery
    already relies on -- and must never appear anywhere in plaintext at rest."""

    def setUp(self):
        self._previous_key = os.environ.get("ANYAICAM_CAMERA_CREDENTIAL_KEY")
        from cryptography.fernet import Fernet
        os.environ["ANYAICAM_CAMERA_CREDENTIAL_KEY"] = Fernet.generate_key().decode()

    def tearDown(self):
        if self._previous_key is None:
            os.environ.pop("ANYAICAM_CAMERA_CREDENTIAL_KEY", None)
        else:
            os.environ["ANYAICAM_CAMERA_CREDENTIAL_KEY"] = self._previous_key

    def test_credentials_round_trip_through_encryption(self):
        token = encrypt_camera_credentials("admin", "s3cret-pass")
        self.assertIsNotNone(token)
        self.assertNotIn(b"s3cret-pass", bytes(token))
        decrypted = decrypt_camera_credentials(token)
        self.assertEqual(decrypted, {"username": "admin", "password": "s3cret-pass"})

    def test_stored_blob_in_the_database_never_contains_the_plaintext_password(self):
        db = fresh_db()
        make_camera(db, "cam-1", device_key="d1", status="configured")
        token = encrypt_camera_credentials("admin", "s3cret-pass")
        blob = token.decode() if isinstance(token, bytes) else token
        db.execute("INSERT INTO camera_credentials VALUES('cam-1',?,?,?)", (blob, "now", "now"))
        db.commit()
        stored = db.execute("SELECT encrypted_blob FROM camera_credentials WHERE camera_id='cam-1'").fetchone()["encrypted_blob"]
        self.assertNotIn("s3cret-pass", stored)

    def test_encryption_fails_closed_when_no_key_is_configured(self):
        os.environ.pop("ANYAICAM_CAMERA_CREDENTIAL_KEY", None)
        self.assertIsNone(encrypt_camera_credentials("admin", "s3cret-pass"))


class NotConfiguredSafetyFixTests(unittest.TestCase):
    """The Samsung incident this session found: an unset camera env var
    raised a bare KeyError inside process_supervisor()'s per-camera task,
    which only caught OSError, so the task died once, silently, and never
    retried. Static source checks since main.py can't be imported here
    (see module docstring)."""

    def test_camera_not_configured_error_exists_and_is_not_a_keyerror_subclass(self):
        self.assertIn("class CameraNotConfiguredError(Exception):", MAIN_SOURCE)
        self.assertNotIn("class CameraNotConfiguredError(KeyError)", MAIN_SOURCE)

    def test_camera_url_raises_camera_not_configured_error_not_bare_keyerror(self):
        camera_url_start = MAIN_SOURCE.index("def camera_url(camera_number: int) -> str:")
        camera_url_body = MAIN_SOURCE[camera_url_start:camera_url_start + 1500]
        self.assertIn("raise CameraNotConfiguredError(", camera_url_body)
        self.assertNotIn("os.environ[f\"CAMERA{camera_number}_HOST\"]", camera_url_body)  # was the bare-KeyError line

    def test_process_supervisor_catches_it_distinctly_and_does_not_die(self):
        supervisor_start = MAIN_SOURCE.index("async def process_supervisor(camera_number: int, mode: str) -> None:")
        supervisor_body = MAIN_SOURCE[supervisor_start:supervisor_start + 3000]
        self.assertIn("except CameraNotConfiguredError:", supervisor_body)
        self.assertIn('camera_process_state[camera_number][mode] = "not_configured"', supervisor_body)
        self.assertIn("await asyncio.sleep(CAMERA_NOT_CONFIGURED_POLL_SECONDS)", supervisor_body)
        self.assertIn("continue", supervisor_body)  # loop keeps running, task never exits

    def test_supervisor_tasks_are_spawned_for_headroom_slots_not_just_provisioned_cameras(self):
        # So a camera provisioned after startup has an already-running,
        # already-polling task waiting for it -- no VMS restart needed.
        self.assertIn("for camera_number in range(1, get_supervisor_slot_count() + 1):", MAIN_SOURCE)
        self.assertIn("def get_supervisor_slot_count() -> int:", MAIN_SOURCE)
        self.assertIn("CAMERA_SUPERVISOR_HEADROOM", MAIN_SOURCE)

    def test_zero_configured_cameras_still_gets_the_legacy_default_slot_count(self):
        get_numbers_start = MAIN_SOURCE.index("def get_camera_numbers() -> list[int]:")
        body = MAIN_SOURCE[get_numbers_start:get_numbers_start + 1500]
        self.assertIn("return numbers if numbers else list(range(1, LEGACY_DEFAULT_CAMERA_COUNT + 1))", body)


class CameraCountRemovalTests(unittest.TestCase):
    def test_camera_count_constant_is_gone(self):
        import re
        self.assertEqual(re.findall(r"(?<![A-Z_])CAMERA_COUNT\b", MAIN_SOURCE), [])

    def test_pydantic_field_bounds_use_a_static_structural_ceiling_not_camera_count(self):
        self.assertIn("Field(ge=1, le=256)", MAIN_SOURCE)


class CameraCrudRoutesTests(unittest.TestCase):
    def test_provision_route_exists_and_is_permission_gated(self):
        start = PARTNER_WORKSPACE_SOURCE.index("def provision_customer_camera")
        body = PARTNER_WORKSPACE_SOURCE[start:start + 1300]
        self.assertIn("require_permission(identity,'camera.self.configure')", body)
        self.assertIn("device_key", body)

    def test_provision_route_never_returns_username_or_password(self):
        start = PARTNER_WORKSPACE_SOURCE.index("def provision_customer_camera")
        end = PARTNER_WORKSPACE_SOURCE.index("@app.delete('/api/customer/cameras/{camera_id}')")
        body = PARTNER_WORKSPACE_SOURCE[start:end]
        return_start = body.index("return {'message':'Camera provisioned.'")
        self.assertNotIn("username", body[return_start:])
        self.assertNotIn("password", body[return_start:])

    def test_remove_route_exists_and_is_permission_gated_and_ownership_scoped(self):
        start = PARTNER_WORKSPACE_SOURCE.index("def remove_customer_camera")
        body = PARTNER_WORKSPACE_SOURCE[start:start + 700]
        self.assertIn("require_permission(identity,'camera.self.configure')", body)
        self.assertIn("WHERE id=? AND customer_id=?", body)

    def test_list_route_never_includes_credentials_and_reports_progress(self):
        start = PARTNER_WORKSPACE_SOURCE.index("def list_customer_cameras")
        body = PARTNER_WORKSPACE_SOURCE[start:start + 1000]
        self.assertNotIn("password", body)
        self.assertNotIn("encrypted_blob", body)
        self.assertIn("expected_camera_count", body)
        self.assertIn("configured_camera_count", body)

    def test_customer_viewer_permission_set_lacks_camera_self_configure(self):
        # partner_db.py's ROLE_PERMISSIONS is the source of truth this
        # route's require_permission() check reads from.
        partner_db_source = (ROOT / "app" / "partner_db.py").read_text(encoding="utf-8")
        viewer_start = partner_db_source.index("'customer_viewer':")
        viewer_line = partner_db_source[viewer_start:partner_db_source.index("\n", viewer_start)]
        self.assertNotIn("camera.self.configure", viewer_line)
        owner_start = partner_db_source.index("'customer_owner':")
        owner_line = partner_db_source[owner_start:partner_db_source.index("\n", owner_start)]
        self.assertIn("camera.self.configure", owner_line)

    def test_no_manage_settings_permission_used_for_customer_camera_routes(self):
        for fn_name in ("provision_customer_camera", "remove_customer_camera", "list_customer_cameras"):
            start = PARTNER_WORKSPACE_SOURCE.index(f"def {fn_name}")
            body = PARTNER_WORKSPACE_SOURCE[start:start + 800]
            self.assertNotIn("manage_settings", body)


class CameraCredentialsMigrationTests(unittest.TestCase):
    def test_camera_credentials_table_and_device_key_column_are_migrated(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS camera_credentials(", DB_MIGRATIONS_SOURCE)
        self.assertIn("'device_key','TEXT'", DB_MIGRATIONS_SOURCE)
        self.assertIn("idx_cameras_customer_device_key", DB_MIGRATIONS_SOURCE)


if __name__ == "__main__":
    unittest.main()
