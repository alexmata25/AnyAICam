"""notification_preferences.py: pure validators plus real-SQLite tests
against a minimal schema, same established pattern as
test_camera_access.py / test_admin_partner_bridge.py.
"""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from notification_preferences import (  # noqa: E402
    EVENT_TYPES,
    NotAuthorizedCameraError,
    get_preferences,
    is_valid_email,
    is_valid_phone,
    is_valid_time,
    resolve_effective_camera_ids,
    save_preferences,
    validate_event_types,
)

SCHEMA = """
CREATE TABLE partner_users(id TEXT PRIMARY KEY, customer_id TEXT);
CREATE TABLE cameras(id TEXT PRIMARY KEY, customer_id TEXT);
CREATE TABLE customer_notification_channels(user_id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,email_address TEXT,email_enabled INTEGER NOT NULL DEFAULT 0,email_verified_at TEXT,phone_number TEXT,sms_enabled INTEGER NOT NULL DEFAULT 0,phone_verified_at TEXT,event_types_json TEXT NOT NULL DEFAULT '[]',camera_scope TEXT NOT NULL DEFAULT 'all',quiet_hours_enabled INTEGER NOT NULL DEFAULT 0,quiet_start TEXT NOT NULL DEFAULT '22:00',quiet_end TEXT NOT NULL DEFAULT '07:00',delivery_mode TEXT NOT NULL DEFAULT 'immediate',updated_at TEXT NOT NULL);
CREATE TABLE customer_notification_channel_cameras(user_id TEXT NOT NULL, camera_id TEXT NOT NULL, PRIMARY KEY(user_id,camera_id));
"""


def fresh_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.execute("INSERT INTO partner_users VALUES('user-1','cust-1')")
    for n in range(1, 6):
        db.execute(f"INSERT INTO cameras VALUES('cam-{n}','cust-1')")
    db.commit()
    return db


class ValidatorTests(unittest.TestCase):
    def test_valid_email_accepted(self):
        self.assertTrue(is_valid_email("owner@example.com"))

    def test_invalid_email_rejected(self):
        self.assertFalse(is_valid_email("not-an-email"))
        self.assertFalse(is_valid_email(""))

    def test_valid_phone_accepted(self):
        self.assertTrue(is_valid_phone("+15551234567"))

    def test_invalid_phone_rejected(self):
        self.assertFalse(is_valid_phone("5551234567"))  # missing +
        self.assertFalse(is_valid_phone("+1555"))  # too short
        self.assertFalse(is_valid_phone(""))

    def test_valid_time_accepted(self):
        self.assertTrue(is_valid_time("22:00"))
        self.assertTrue(is_valid_time("07:05"))

    def test_invalid_time_rejected(self):
        self.assertFalse(is_valid_time("25:00"))
        self.assertFalse(is_valid_time("not-a-time"))

    def test_validate_event_types_drops_unknown_and_dedupes(self):
        result = validate_event_types(["smart_motion", "bogus", "smart_motion", "lpr"])
        self.assertEqual(result, ["smart_motion", "lpr"])

    def test_every_documented_event_type_is_recognized(self):
        for key in ("smart_motion", "person", "vehicle", "lpr", "ppe", "people_counting",
                    "camera_offline", "appliance_offline", "storage_problem", "system_health"):
            self.assertIn(key, EVENT_TYPES)


class ResolveEffectiveCameraIdsTests(unittest.TestCase):
    def test_all_scope_returns_every_currently_authorized_camera(self):
        result = resolve_effective_camera_ids(camera_scope="all", camera_ids=[], authorized_camera_ids={"cam-1", "cam-2"})
        self.assertEqual(result, ["cam-1", "cam-2"])

    def test_selected_scope_intersects_with_live_authorization(self):
        # A camera in the stored selection that's since had its
        # authorization revoked drops out automatically.
        result = resolve_effective_camera_ids(
            camera_scope="selected", camera_ids=["cam-1", "cam-2"], authorized_camera_ids={"cam-1"},
        )
        self.assertEqual(result, ["cam-1"])


class SaveAndGetPreferencesDbTests(unittest.TestCase):
    def test_save_then_get_round_trips_email_only(self):
        db = fresh_db()
        save_preferences(
            db, user_id="user-1", customer_id="cust-1", authorized_camera_ids={"cam-1", "cam-2", "cam-3", "cam-4", "cam-5"},
            now="2026-08-27T00:00:00",
            email_address="owner@example.com", email_enabled=True,
            phone_number="", sms_enabled=False,
            event_types=["smart_motion", "person"], camera_scope="all", camera_ids=[],
            quiet_hours_enabled=False, quiet_start="22:00", quiet_end="07:00", delivery_mode="immediate",
        )
        prefs = get_preferences(db, user_id="user-1")
        self.assertTrue(prefs["email_enabled"])
        self.assertFalse(prefs["sms_enabled"])
        self.assertEqual(prefs["event_types"], ["smart_motion", "person"])

    def test_save_then_get_round_trips_sms_only(self):
        db = fresh_db()
        save_preferences(
            db, user_id="user-1", customer_id="cust-1", authorized_camera_ids={"cam-1"},
            now="2026-08-27T00:00:00",
            email_address="", email_enabled=False,
            phone_number="+15551234567", sms_enabled=True,
            event_types=["camera_offline"], camera_scope="all", camera_ids=[],
            quiet_hours_enabled=False, quiet_start="22:00", quiet_end="07:00", delivery_mode="immediate",
        )
        prefs = get_preferences(db, user_id="user-1")
        self.assertFalse(prefs["email_enabled"])
        self.assertTrue(prefs["sms_enabled"])
        self.assertEqual(prefs["phone_number"], "+15551234567")

    def test_save_both_channels_enabled(self):
        db = fresh_db()
        save_preferences(
            db, user_id="user-1", customer_id="cust-1", authorized_camera_ids={"cam-1"},
            now="2026-08-27T00:00:00",
            email_address="owner@example.com", email_enabled=True,
            phone_number="+15551234567", sms_enabled=True,
            event_types=["smart_motion"], camera_scope="all", camera_ids=[],
            quiet_hours_enabled=False, quiet_start="22:00", quiet_end="07:00", delivery_mode="immediate",
        )
        prefs = get_preferences(db, user_id="user-1")
        self.assertTrue(prefs["email_enabled"])
        self.assertTrue(prefs["sms_enabled"])

    def test_both_channels_disabled_is_the_default_and_a_valid_save(self):
        db = fresh_db()
        save_preferences(
            db, user_id="user-1", customer_id="cust-1", authorized_camera_ids={"cam-1"},
            now="2026-08-27T00:00:00",
            email_address="", email_enabled=False, phone_number="", sms_enabled=False,
            event_types=[], camera_scope="all", camera_ids=[],
            quiet_hours_enabled=False, quiet_start="22:00", quiet_end="07:00", delivery_mode="immediate",
        )
        prefs = get_preferences(db, user_id="user-1")
        self.assertFalse(prefs["email_enabled"])
        self.assertFalse(prefs["sms_enabled"])

    def test_get_preferences_for_unknown_user_returns_honest_defaults(self):
        db = fresh_db()
        prefs = get_preferences(db, user_id="nobody")
        self.assertFalse(prefs["email_enabled"])
        self.assertFalse(prefs["sms_enabled"])
        self.assertEqual(prefs["event_types"], [])

    def test_selected_camera_scope_persists_the_junction_rows(self):
        db = fresh_db()
        save_preferences(
            db, user_id="user-1", customer_id="cust-1", authorized_camera_ids={"cam-1", "cam-2", "cam-3"},
            now="2026-08-27T00:00:00",
            email_address="owner@example.com", email_enabled=True, phone_number="", sms_enabled=False,
            event_types=["smart_motion"], camera_scope="selected", camera_ids=["cam-1", "cam-3"],
            quiet_hours_enabled=False, quiet_start="22:00", quiet_end="07:00", delivery_mode="immediate",
        )
        prefs = get_preferences(db, user_id="user-1")
        self.assertEqual(prefs["camera_scope"], "selected")
        self.assertEqual(prefs["camera_ids"], ["cam-1", "cam-3"])

    def test_selecting_an_unauthorized_camera_is_rejected_not_silently_dropped(self):
        """The exact permission requirement: a restricted user's request
        for a camera outside their authorized set must fail loudly."""
        db = fresh_db()
        with self.assertRaises(NotAuthorizedCameraError):
            save_preferences(
                db, user_id="user-1", customer_id="cust-1", authorized_camera_ids={"cam-1"},
                now="2026-08-27T00:00:00",
                email_address="owner@example.com", email_enabled=True, phone_number="", sms_enabled=False,
                event_types=["smart_motion"], camera_scope="selected", camera_ids=["cam-1", "cam-5"],
                quiet_hours_enabled=False, quiet_start="22:00", quiet_end="07:00", delivery_mode="immediate",
            )
        # Nothing was saved -- the rejected request must not partially persist.
        prefs = get_preferences(db, user_id="user-1")
        self.assertEqual(prefs["camera_ids"], [])

    def test_invalid_email_with_email_enabled_is_rejected(self):
        db = fresh_db()
        with self.assertRaises(ValueError):
            save_preferences(
                db, user_id="user-1", customer_id="cust-1", authorized_camera_ids={"cam-1"},
                now="2026-08-27T00:00:00",
                email_address="not-an-email", email_enabled=True, phone_number="", sms_enabled=False,
                event_types=[], camera_scope="all", camera_ids=[],
                quiet_hours_enabled=False, quiet_start="22:00", quiet_end="07:00", delivery_mode="immediate",
            )

    def test_invalid_phone_with_sms_enabled_is_rejected(self):
        db = fresh_db()
        with self.assertRaises(ValueError):
            save_preferences(
                db, user_id="user-1", customer_id="cust-1", authorized_camera_ids={"cam-1"},
                now="2026-08-27T00:00:00",
                email_address="", email_enabled=False, phone_number="555-1234", sms_enabled=True,
                event_types=[], camera_scope="all", camera_ids=[],
                quiet_hours_enabled=False, quiet_start="22:00", quiet_end="07:00", delivery_mode="immediate",
            )

    def test_disabled_channel_does_not_require_a_valid_address(self):
        # email_enabled=False must never block a save just because the
        # (unused) address field is empty or malformed.
        db = fresh_db()
        save_preferences(
            db, user_id="user-1", customer_id="cust-1", authorized_camera_ids={"cam-1"},
            now="2026-08-27T00:00:00",
            email_address="", email_enabled=False, phone_number="", sms_enabled=False,
            event_types=[], camera_scope="all", camera_ids=[],
            quiet_hours_enabled=False, quiet_start="22:00", quiet_end="07:00", delivery_mode="immediate",
        )  # must not raise

    def test_quiet_hours_enabled_requires_valid_times(self):
        db = fresh_db()
        with self.assertRaises(ValueError):
            save_preferences(
                db, user_id="user-1", customer_id="cust-1", authorized_camera_ids={"cam-1"},
                now="2026-08-27T00:00:00",
                email_address="", email_enabled=False, phone_number="", sms_enabled=False,
                event_types=[], camera_scope="all", camera_ids=[],
                quiet_hours_enabled=True, quiet_start="not-a-time", quiet_end="07:00", delivery_mode="immediate",
            )

    def test_quiet_hours_round_trip(self):
        db = fresh_db()
        save_preferences(
            db, user_id="user-1", customer_id="cust-1", authorized_camera_ids={"cam-1"},
            now="2026-08-27T00:00:00",
            email_address="owner@example.com", email_enabled=True, phone_number="", sms_enabled=False,
            event_types=[], camera_scope="all", camera_ids=[],
            quiet_hours_enabled=True, quiet_start="21:30", quiet_end="06:45", delivery_mode="immediate",
        )
        prefs = get_preferences(db, user_id="user-1")
        self.assertTrue(prefs["quiet_hours_enabled"])
        self.assertEqual(prefs["quiet_start"], "21:30")
        self.assertEqual(prefs["quiet_end"], "06:45")

    def test_changing_email_address_resets_verification(self):
        db = fresh_db()
        db.execute(
            "INSERT INTO customer_notification_channels(user_id,customer_id,email_address,email_enabled,email_verified_at,updated_at) "
            "VALUES('user-1','cust-1','old@example.com',1,'2026-08-01T00:00:00','2026-08-01T00:00:00')"
        )
        db.commit()
        save_preferences(
            db, user_id="user-1", customer_id="cust-1", authorized_camera_ids={"cam-1"},
            now="2026-08-27T00:00:00",
            email_address="new@example.com", email_enabled=True, phone_number="", sms_enabled=False,
            event_types=[], camera_scope="all", camera_ids=[],
            quiet_hours_enabled=False, quiet_start="22:00", quiet_end="07:00", delivery_mode="immediate",
        )
        prefs = get_preferences(db, user_id="user-1")
        self.assertIsNone(prefs["email_verified_at"])

    def test_resaving_the_same_email_address_keeps_verification(self):
        db = fresh_db()
        db.execute(
            "INSERT INTO customer_notification_channels(user_id,customer_id,email_address,email_enabled,email_verified_at,updated_at) "
            "VALUES('user-1','cust-1','owner@example.com',1,'2026-08-01T00:00:00','2026-08-01T00:00:00')"
        )
        db.commit()
        save_preferences(
            db, user_id="user-1", customer_id="cust-1", authorized_camera_ids={"cam-1"},
            now="2026-08-27T00:00:00",
            email_address="owner@example.com", email_enabled=True, phone_number="", sms_enabled=False,
            event_types=["smart_motion"], camera_scope="all", camera_ids=[],
            quiet_hours_enabled=False, quiet_start="22:00", quiet_end="07:00", delivery_mode="immediate",
        )
        prefs = get_preferences(db, user_id="user-1")
        self.assertEqual(prefs["email_verified_at"], "2026-08-01T00:00:00")


if __name__ == "__main__":
    unittest.main()
