"""Admin Portal <-> Partner Portal identity bridge (app/admin_partner_bridge.py).

Pure logic tests plus real SQLite tests against a minimal schema, same
established pattern as test_camera_access.py.
"""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from admin_partner_bridge import (  # noqa: E402
    BRIDGEABLE_ADMIN_ROLES,
    BRIDGEABLE_PARTNER_ROLES,
    bridge_partner_identity,
    can_create_link,
    create_link,
    get_link,
    resolve_bridged_identity,
    revoke_link,
)

SCHEMA = """
CREATE TABLE partner_users(id TEXT PRIMARY KEY, email TEXT NOT NULL, role TEXT NOT NULL, partner_id TEXT, customer_id TEXT, approved INTEGER NOT NULL DEFAULT 1);
CREATE TABLE admin_partner_links(admin_user_id TEXT PRIMARY KEY, admin_email TEXT NOT NULL, partner_user_id TEXT NOT NULL, partner_email TEXT NOT NULL, linked_at TEXT NOT NULL, linked_by TEXT NOT NULL, revoked_at TEXT);
"""


def fresh_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


class CanCreateLinkTests(unittest.TestCase):
    def test_administrator_admin_role_with_administrator_partner_role_is_allowed(self):
        self.assertTrue(can_create_link("administrator", "administrator"))

    def test_every_bridgeable_pair_is_allowed(self):
        for admin_role in BRIDGEABLE_ADMIN_ROLES:
            for partner_role in BRIDGEABLE_PARTNER_ROLES:
                self.assertTrue(can_create_link(admin_role, partner_role), (admin_role, partner_role))

    def test_customer_owner_partner_role_is_never_linkable(self):
        for admin_role in BRIDGEABLE_ADMIN_ROLES:
            self.assertFalse(can_create_link(admin_role, "customer_owner"))

    def test_customer_viewer_partner_role_is_never_linkable(self):
        for admin_role in BRIDGEABLE_ADMIN_ROLES:
            self.assertFalse(can_create_link(admin_role, "customer_viewer"))

    def test_non_admin_legacy_role_can_never_create_a_link(self):
        # Even pointed at a perfectly legitimate partner role, a legacy
        # 'viewer'/'installer'/other non-admin role must never be able to
        # create a bridge -- this is the choke point that keeps the
        # bridge admin-only on the Admin Portal side.
        self.assertFalse(can_create_link("viewer", "administrator"))
        self.assertFalse(can_create_link("installer", "partner_owner"))
        self.assertFalse(can_create_link("customer_owner", "administrator"))

    def test_unrecognized_roles_on_either_side_are_denied(self):
        self.assertFalse(can_create_link("administrator", "bogus_role"))
        self.assertFalse(can_create_link("bogus_role", "administrator"))


class ResolveBridgedIdentityPureTests(unittest.TestCase):
    def _valid_link(self):
        return {"admin_user_id": "admin-1", "partner_email": "tech@example.test", "revoked_at": None}

    def _valid_partner_user(self):
        return {"id": "pu-1", "email": "tech@example.test", "role": "technician", "partner_id": "partner-1", "customer_id": None, "approved": 1}

    def test_valid_link_and_live_user_resolves_to_a_partner_identity_shaped_dict(self):
        identity = resolve_bridged_identity(admin_role="administrator", link_row=self._valid_link(), live_partner_user=self._valid_partner_user())
        self.assertEqual(identity, {
            "role": "technician", "email": "tech@example.test", "partner_id": "partner-1",
            "customer_id": None, "via_bridge": True, "bridged_admin_user_id": "admin-1",
        })

    def test_admin_role_no_longer_bridgeable_is_denied(self):
        self.assertIsNone(resolve_bridged_identity(admin_role="viewer", link_row=self._valid_link(), live_partner_user=self._valid_partner_user()))

    def test_no_link_row_is_denied(self):
        self.assertIsNone(resolve_bridged_identity(admin_role="administrator", link_row=None, live_partner_user=self._valid_partner_user()))

    def test_revoked_link_is_denied(self):
        link = self._valid_link(); link["revoked_at"] = "2026-08-27T00:00:00"
        self.assertIsNone(resolve_bridged_identity(admin_role="administrator", link_row=link, live_partner_user=self._valid_partner_user()))

    def test_deleted_target_partner_user_is_denied(self):
        self.assertIsNone(resolve_bridged_identity(admin_role="administrator", link_row=self._valid_link(), live_partner_user=None))

    def test_unapproved_target_partner_user_is_denied(self):
        user = self._valid_partner_user(); user["approved"] = 0
        self.assertIsNone(resolve_bridged_identity(admin_role="administrator", link_row=self._valid_link(), live_partner_user=user))

    def test_email_mismatch_between_link_and_live_row_is_denied(self):
        user = self._valid_partner_user(); user["email"] = "someone-else@example.test"
        self.assertIsNone(resolve_bridged_identity(admin_role="administrator", link_row=self._valid_link(), live_partner_user=user))

    def test_role_drifted_to_customer_owner_since_linking_is_denied(self):
        # The exact scenario this bridge must never allow: a link created
        # while the target was 'technician', later changed to
        # 'customer_owner' on the partner side -- must lose bridged
        # access on its very next use, not silently keep working.
        user = self._valid_partner_user(); user["role"] = "customer_owner"
        self.assertIsNone(resolve_bridged_identity(admin_role="administrator", link_row=self._valid_link(), live_partner_user=user))

    def test_role_drifted_to_customer_viewer_since_linking_is_denied(self):
        user = self._valid_partner_user(); user["role"] = "customer_viewer"
        self.assertIsNone(resolve_bridged_identity(admin_role="administrator", link_row=self._valid_link(), live_partner_user=user))

    def test_role_drifted_to_unrecognized_role_is_denied(self):
        user = self._valid_partner_user(); user["role"] = "some_future_role"
        self.assertIsNone(resolve_bridged_identity(admin_role="administrator", link_row=self._valid_link(), live_partner_user=user))


class DbWrapperTests(unittest.TestCase):
    def test_create_then_get_link_round_trips(self):
        db = fresh_db()
        create_link(db, admin_user_id="admin-1", admin_email="admin@example.test", partner_user_id="pu-1", partner_email="tech@example.test", linked_by="admin@example.test", now="2026-08-27T00:00:00")
        link = get_link(db, admin_user_id="admin-1")
        self.assertEqual(link["partner_user_id"], "pu-1")
        self.assertEqual(link["partner_email"], "tech@example.test")
        self.assertIsNone(link["revoked_at"])

    def test_relinking_the_same_admin_replaces_not_accumulates(self):
        db = fresh_db()
        create_link(db, admin_user_id="admin-1", admin_email="admin@example.test", partner_user_id="pu-1", partner_email="tech@example.test", linked_by="admin@example.test", now="2026-08-27T00:00:00")
        create_link(db, admin_user_id="admin-1", admin_email="admin@example.test", partner_user_id="pu-2", partner_email="owner@example.test", linked_by="admin@example.test", now="2026-08-27T01:00:00")
        link = get_link(db, admin_user_id="admin-1")
        self.assertEqual(link["partner_user_id"], "pu-2")
        count = db.execute("SELECT COUNT(*) c FROM admin_partner_links WHERE admin_user_id='admin-1'").fetchone()["c"]
        self.assertEqual(count, 1)

    def test_revoke_link_stops_it_resolving_but_keeps_the_row(self):
        db = fresh_db()
        create_link(db, admin_user_id="admin-1", admin_email="admin@example.test", partner_user_id="pu-1", partner_email="tech@example.test", linked_by="admin@example.test", now="2026-08-27T00:00:00")
        revoke_link(db, admin_user_id="admin-1", now="2026-08-27T02:00:00")
        link = get_link(db, admin_user_id="admin-1")
        self.assertIsNotNone(link)
        self.assertEqual(link["revoked_at"], "2026-08-27T02:00:00")

    def test_get_link_for_unknown_admin_is_none(self):
        db = fresh_db()
        self.assertIsNone(get_link(db, admin_user_id="nobody"))


class BridgePartnerIdentityDbTests(unittest.TestCase):
    def _seed_partner_user(self, db, user_id, email, role, partner_id="partner-1", approved=1):
        db.execute("INSERT INTO partner_users(id,email,role,partner_id,customer_id,approved) VALUES(?,?,?,?,?,?)", (user_id, email, role, partner_id, None, approved))
        db.commit()

    def test_full_round_trip_resolves_to_a_live_bridged_identity(self):
        db = fresh_db()
        self._seed_partner_user(db, "pu-1", "tech@example.test", "technician")
        create_link(db, admin_user_id="admin-1", admin_email="admin@example.test", partner_user_id="pu-1", partner_email="tech@example.test", linked_by="admin@example.test", now="2026-08-27T00:00:00")
        identity = bridge_partner_identity(db, admin_user={"id": "admin-1", "role": "administrator", "enabled": True, "email": "admin@example.test"})
        self.assertEqual(identity["role"], "technician")
        self.assertTrue(identity["via_bridge"])

    def test_role_changed_on_the_partner_side_after_linking_loses_bridge_access_immediately(self):
        db = fresh_db()
        self._seed_partner_user(db, "pu-1", "tech@example.test", "technician")
        create_link(db, admin_user_id="admin-1", admin_email="admin@example.test", partner_user_id="pu-1", partner_email="tech@example.test", linked_by="admin@example.test", now="2026-08-27T00:00:00")
        db.execute("UPDATE partner_users SET role='customer_viewer' WHERE id='pu-1'"); db.commit()
        identity = bridge_partner_identity(db, admin_user={"id": "admin-1", "role": "administrator", "enabled": True, "email": "admin@example.test"})
        self.assertIsNone(identity)

    def test_deleted_partner_user_after_linking_loses_bridge_access(self):
        db = fresh_db()
        self._seed_partner_user(db, "pu-1", "tech@example.test", "technician")
        create_link(db, admin_user_id="admin-1", admin_email="admin@example.test", partner_user_id="pu-1", partner_email="tech@example.test", linked_by="admin@example.test", now="2026-08-27T00:00:00")
        db.execute("DELETE FROM partner_users WHERE id='pu-1'"); db.commit()
        identity = bridge_partner_identity(db, admin_user={"id": "admin-1", "role": "administrator", "enabled": True, "email": "admin@example.test"})
        self.assertIsNone(identity)

    def test_unauthenticated_admin_user_never_resolves_a_bridge(self):
        db = fresh_db()
        self._seed_partner_user(db, "pu-1", "tech@example.test", "technician")
        create_link(db, admin_user_id="anonymous", admin_email="", partner_user_id="pu-1", partner_email="tech@example.test", linked_by="x", now="2026-08-27T00:00:00")
        identity = bridge_partner_identity(db, admin_user={"id": "anonymous", "role": "viewer", "enabled": False, "email": ""})
        self.assertIsNone(identity)

    def test_no_link_at_all_resolves_to_none(self):
        db = fresh_db()
        identity = bridge_partner_identity(db, admin_user={"id": "admin-1", "role": "administrator", "enabled": True, "email": "admin@example.test"})
        self.assertIsNone(identity)


if __name__ == "__main__":
    unittest.main()
