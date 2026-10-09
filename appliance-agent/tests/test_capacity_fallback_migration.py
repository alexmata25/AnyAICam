"""Signed camera capacity: the one-time, root-owned migration (2026-10-09).

From 1.2.5 the VMS trusts only a camera capacity the cloud signed. On an
upgrade from an earlier release, root carries the capacity the appliance was
running with into /etc/anyaicam-update/vms_capacity_fallback.json, once, bound
to the activation identity, so an offline appliance keeps recording until
its first signed capacity:

  * only from a release before 1.2.5 (or with no recorded release);
  * never overwritten; never from a symlinked, hard-linked, oversized or
    malformed source; only three identity fields, never the credential;
  * Software Update does it before any downtime, and an unsafe existing
    fallback stops the update with nothing changed.
"""
import json
import os
import stat
import unittest
from datetime import datetime, timezone
from pathlib import Path

from test_software_update_root_applier import ApplierTestCase, ar

IDENTITY = {"appliance_id": "appl-1", "cloud_id": "AIC-C814766E", "customer_id": "cust-1",
            "credential": "secret-credential-value", "site_id": "site-1", "partner_id": "p-1",
            "activated_at": "2026-10-01T00:00:00", "activation_version": 1}
NOW = lambda: datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)  # noqa: E731


class Files:
    def __init__(self, root: Path):
        self.target = root / "etc/anyaicam-update/vms_capacity_fallback.json"
        self.legacy = root / "var/lib/anyaicam/vms/data-config/vms_capacity.json"
        self.identity = root / "var/lib/anyaicam/vms/recordings/appliance_identity.json"
        for path in (self.target, self.legacy, self.identity):
            path.parent.mkdir(parents=True, exist_ok=True)

    def capacity(self, value):
        self.legacy.write_text(json.dumps({"camera_slot_quantity": value, "plan_camera_slots": value,
                                           "vms_license_capacity": None}), encoding="utf-8")

    def activated(self, **changes):
        self.identity.write_text(json.dumps(dict(IDENTITY, **changes)), encoding="utf-8")

    def fallback(self):
        return json.loads(self.target.read_text(encoding="utf-8")) if self.target.exists() else None


class MigrationRuleTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import shutil
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.files = Files(self.root)

    def migrate(self, from_version="1.2.4", by="installer"):
        return ar.migrate_capacity_fallback(from_version=from_version, migrated_by=by, target=self.files.target,
                                            legacy=self.files.legacy, identity_file=self.files.identity, now=NOW)

    def test_an_upgrade_from_1_2_4_carries_the_capacity_bound_to_the_identity(self):
        self.files.capacity(3)
        self.files.activated()
        self.assertEqual(self.migrate(), "migrated")
        self.assertEqual(self.files.fallback(), {
            "schema": 1, "camera_slot_quantity": 3, "appliance_id": "appl-1", "cloud_id": "AIC-C814766E",
            "customer_id": "cust-1", "source": "migrated_unsigned_capacity", "migrated_from_version": "1.2.4",
            "migrated_at": "2026-10-09T12:00:00+00:00", "migrated_by": "installer"})
        self.assertNotIn("secret-credential-value", self.files.target.read_text(encoding="utf-8"))
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.files.target).st_mode), 0o644)

    def test_which_releases_migrate(self):
        for version, applies in (("", True), ("1.1.0", True), ("1.2.4", True), ("1.2.4.9", True), ("1.2", True),
                                 ("1.2.5", False), ("1.2.5.0", False), ("1.2.10", False), ("1.3", False),
                                 ("2.0.0", False), ("v1.2.4", False), ("garbage", False)):
            with self.subTest(version=version):
                self.assertEqual(ar.capacity_migration_applies(version), applies)

    def test_a_later_upgrade_never_launders_a_forged_value(self):
        # A fresh 1.2.5 install had no capacity file; the anyaicam user forges
        # one; the next upgrade (from 1.2.5) must not carry it over.
        self.files.capacity(64)
        self.files.activated()
        self.assertEqual(self.migrate(from_version="1.2.5"), "not_applicable")
        self.assertIsNone(self.files.fallback())

    def test_never_overwritten(self):
        self.files.capacity(2)
        self.files.activated()
        self.assertEqual(self.migrate(), "migrated")
        self.files.capacity(64)                      # forged after the migration
        self.assertEqual(self.migrate(), "exists")   # e.g. a re-run, or reinstall after a rollback
        self.assertEqual(self.files.fallback()["camera_slot_quantity"], 2)

    def test_clean_or_unclaimed_appliances_get_no_fallback(self):
        self.assertEqual(self.migrate(), "no_capacity")          # no capacity file at all
        self.files.capacity(2)
        self.assertEqual(self.migrate(), "no_identity")          # never activated
        for broken in ({"appliance_id": ""}, {"cloud_id": None}, {"customer_id": "x" * 300},
                       {"customer_id": "../etc"}, {"appliance_id": 7}):
            with self.subTest(broken=broken):
                self.files.activated(**broken)
                self.assertEqual(self.migrate(), "no_identity")
        self.assertIsNone(self.files.fallback())

    def test_malformed_or_out_of_range_capacity_is_not_carried(self):
        self.files.activated()
        for value in (-1, 10001, 2.5, "3", True, None, [3]):
            with self.subTest(value=value):
                self.files.capacity(value)
                self.assertEqual(self.migrate(), "no_capacity")
        for raw in (b"not json", b"[3]", b"\xff\xfe", b"{}"):
            with self.subTest(raw=raw):
                self.files.legacy.write_bytes(raw)
                self.assertEqual(self.migrate(), "no_capacity")
        self.files.capacity(0)                                    # zero is a real capacity
        self.assertEqual(self.migrate(), "migrated")
        self.assertEqual(self.files.fallback()["camera_slot_quantity"], 0)

    def test_an_oversized_source_is_not_read(self):
        self.files.activated()
        self.files.legacy.write_text('{"camera_slot_quantity": 3, "pad": "' + "x" * 70000 + '"}', encoding="utf-8")
        self.assertEqual(self.migrate(), "no_capacity")

    def test_a_directory_in_place_of_the_source_is_not_read(self):
        self.files.activated()
        self.files.legacy.mkdir()
        self.assertEqual(self.migrate(), "no_capacity")

    @unittest.skipUnless(hasattr(os, "symlink") and os.name == "posix", "POSIX symlinks")
    def test_a_symlinked_source_or_identity_is_never_followed(self):
        real = self.root / "elsewhere.json"
        real.write_text(json.dumps({"camera_slot_quantity": 50}), encoding="utf-8")
        self.files.legacy.symlink_to(real)
        self.files.activated()
        self.assertEqual(self.migrate(), "no_capacity")
        self.files.legacy.unlink()
        self.files.capacity(2)
        self.files.identity.unlink()
        identity = self.root / "identity-elsewhere.json"
        identity.write_text(json.dumps(IDENTITY), encoding="utf-8")
        self.files.identity.symlink_to(identity)
        self.assertEqual(self.migrate(), "no_identity")

    @unittest.skipUnless(os.name == "posix", "POSIX hard links")
    def test_a_hard_linked_source_is_not_read(self):
        real = self.root / "other.json"
        real.write_text(json.dumps({"camera_slot_quantity": 50}), encoding="utf-8")
        os.link(real, self.files.legacy)
        self.files.activated()
        self.assertEqual(self.migrate(), "no_capacity")

    def test_an_unsafe_existing_fallback_is_refused(self):
        self.files.capacity(2)
        self.files.activated()
        self.files.target.mkdir()                      # not a regular file
        with self.assertRaises(ar.TrustAnchorError):
            self.migrate()

    @unittest.skipUnless(os.name == "posix", "POSIX ownership")
    def test_a_fallback_not_owned_by_root_is_refused(self):
        self.files.target.write_text("{}", encoding="utf-8")
        if os.geteuid() == 0:
            os.chown(self.files.target, 4242, 4242)
        with self.assertRaises(ar.TrustAnchorError):
            ar.migrate_capacity_fallback(from_version="1.2.4", migrated_by="installer", target=self.files.target,
                                         legacy=self.files.legacy, identity_file=self.files.identity, posix=True)

    def test_migrated_by_is_fixed(self):
        with self.assertRaises(ValueError):
            self.migrate(by="agent")

    def test_the_installer_entry_point_prints_the_outcome(self):
        # migrate_capacity_fallback_main uses the fixed paths; on this host
        # they do not exist, so it can only report that nothing was carried.
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = ar.migrate_capacity_fallback_main(["1.2.5"])
        self.assertEqual((code, buffer.getvalue().strip()), (0, "not_applicable"))


class SoftwareUpdateMigrationTests(ApplierTestCase):
    """Through the real applier: the installed release is 1.1.0 (marker)."""

    def setUp(self):
        super().setUp()
        self.files = Files(self.tmp)

    def test_an_update_from_before_1_2_5_migrates_before_any_downtime(self):
        self.files.capacity(4)
        self.files.activated()
        seen = []
        original = ar.safe_replace

        def recording(directory, name, data, **kwargs):
            if name == "vms_capacity_fallback.json":
                seen.append(any(call[:2] == ["systemctl", "stop"] for call in self.host.calls))
            return original(directory, name, data, **kwargs)
        self.stage()
        from unittest import mock
        with mock.patch.object(ar, "safe_replace", recording):
            result = self.applier().apply_staged()
        self.assertEqual(result["state"], "healthy", result.get("error"))
        self.assertEqual(result["capacity_fallback"], "migrated")
        self.assertEqual(seen, [False])
        fallback = self.files.fallback()
        self.assertEqual((fallback["camera_slot_quantity"], fallback["migrated_by"], fallback["migrated_from_version"]),
                         (4, "software_update", "1.1.0"))
        self.assertTrue(self.files.legacy.exists(), "the unsigned file stays for a rollback to the old release")

    def test_an_update_from_1_2_5_does_not_migrate(self):
        self.paths.release_marker.write_text(json.dumps({"release_version": "1.2.5", "vms_release_commit": "a" * 40}))
        self.files.capacity(64)
        self.files.activated()
        self.stage(version="1.2.6")
        result = self.applier().apply_staged()
        self.assertEqual((result["state"], result["capacity_fallback"]), ("healthy", "not_applicable"), result.get("error"))
        self.assertIsNone(self.files.fallback())

    def test_an_unsafe_fallback_stops_the_update_with_nothing_changed(self):
        self.files.capacity(4)
        self.files.activated()
        self.files.target.mkdir()
        self.stage()
        result = self.applier().apply_staged()
        self.assertEqual(result["state"], "install_failed", result)
        self.assertFalse(any(call[:2] == ["systemctl", "stop"] for call in self.host.calls), "the VMS was stopped")
        self.assertEqual(self.marker()["release_version"], "1.1.0")

    def test_no_capacity_or_identity_still_updates(self):
        self.stage()
        result = self.applier().apply_staged()
        self.assertEqual((result["state"], result["capacity_fallback"]), ("healthy", "no_capacity"), result.get("error"))


if __name__ == "__main__":
    unittest.main()
