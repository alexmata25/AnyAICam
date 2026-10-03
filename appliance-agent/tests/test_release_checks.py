"""Software Update (2026-10-03): the release checks shared by the agent, the
root applier and the offline signer (anyaicam_agent/updater/release_checks.py)."""
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from anyaicam_agent.updater import release_checks as rc

from software_update_helpers import (BUILD_B, MIGRATIONS_ADDITIVE, MIGRATIONS_DESTRUCTIVE, MIGRATIONS_V1,
                                     release_files, write_tarball)


def _manifest(**overrides):
    base = dict(update_id="1.2.0-bbbbbbbbbbbb", version="1.2.0", build_id=BUILD_B, target="anyaicam-appliance",
                platform="ubuntu", architecture="x86_64", migration_safety="additive", package_size_bytes=10)
    base.update(overrides)
    return SimpleNamespace(**base)


class VersionTests(unittest.TestCase):
    def test_dotted_versions_only(self):
        self.assertEqual(rc.parse_release_version("1.2.0"), (1, 2, 0))
        for bad in ("1", "1.2.0-beta", "v1.2", "", "1..2", "1.2.3.4.5", "../1.2"):
            with self.subTest(bad=bad), self.assertRaises(rc.ReleaseCheckError):
                rc.parse_release_version(bad)

    def test_newer_and_downgrade(self):
        self.assertTrue(rc.is_newer("1.2.0", "1.1.9"))
        self.assertTrue(rc.is_newer("1.10.0", "1.9.0"))
        self.assertFalse(rc.is_newer("1.2.0", "1.2.0"))  # replay
        self.assertFalse(rc.is_newer("1.1.0", "1.2.0"))  # downgrade
        self.assertFalse(rc.is_newer("1.2", "1.2.0"))
        self.assertTrue(rc.is_newer("1.2.0", ""))         # appliance from before release versions
        self.assertFalse(rc.is_newer("1.2.0", "garbage"))  # never guesses

    def test_release_label_round_trip_keeps_the_full_build_on_the_device(self):
        label = rc.release_label("1.2.0", BUILD_B)
        self.assertEqual(label, "1.2.0+" + BUILD_B[:12])
        self.assertEqual(rc.split_release_label(label), ("1.2.0", BUILD_B[:12]))
        self.assertEqual(rc.split_release_label("0.1.0"), ("", ""))


class DeviceCheckTests(unittest.TestCase):
    def check(self, manifest, current="1.1.0", platform="ubuntu", architecture="x86_64"):
        rc.check_manifest_for_device(manifest, target="anyaicam-appliance", platform=platform,
                                     architecture=architecture, current_version=current)

    def test_a_matching_newer_additive_release_passes(self):
        self.check(_manifest())

    def test_each_mismatch_is_refused_with_its_own_reason(self):
        cases = {
            "wrong_target": _manifest(target="other-device"),
            "wrong_platform": _manifest(platform="debian"),
            "wrong_architecture": _manifest(architecture="aarch64"),
            "not_newer": _manifest(version="1.1.0"),
            "unsafe_migrations": _manifest(migration_safety=""),
            "bad_build_id": _manifest(build_id="abc"),
            "bad_update_id": _manifest(update_id="../../etc/passwd"),
        }
        for code, manifest in cases.items():
            with self.subTest(code=code):
                with self.assertRaises(rc.ReleaseCheckError) as raised:
                    self.check(manifest)
                self.assertEqual(raised.exception.code, code)

    def test_insufficient_disk_is_refused(self):
        usage = lambda _path: SimpleNamespace(free=1024)  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(rc.ReleaseCheckError) as raised:
            rc.check_free_space(tmp, rc.required_free_bytes(500 * 1024 ** 2), disk_usage=usage)
        self.assertEqual(raised.exception.code, "insufficient_disk")


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.files = release_files("1.2.0", BUILD_B)

    def _extract_with(self, info, data=None):
        package = write_tarball(self.root / "pkg.tar.gz", self.files, extra_members=[(info, data)])
        with self.assertRaises(rc.ReleaseCheckError) as raised:
            rc.safe_extract(package, self.root / "out")
        self.assertEqual(raised.exception.code, "unsafe_archive")
        self.assertFalse((self.root / "out").exists(), "nothing may be left behind")
        return raised.exception

    def _member(self, name, kind=tarfile.REGTYPE, linkname=""):
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = linkname
        return info

    def test_a_well_formed_package_extracts_and_verifies(self):
        package = write_tarball(self.root / "pkg.tar.gz", self.files)
        root = rc.safe_extract(package, self.root / "out")
        env = rc.verify_release_tree(root, SimpleNamespace(version="1.2.0", build_id=BUILD_B))
        self.assertEqual(env["RELEASE_VERSION"], "1.2.0")

    def test_parent_directory_traversal_is_refused(self):
        info = self._member("payload/../../escape.txt")
        info.size = 1
        self.assertIn("traversal", str(self._extract_with(info, b"x")))

    def test_absolute_paths_are_refused(self):
        info = self._member("/etc/cron.d/evil")
        info.size = 1
        self.assertIn("absolute", str(self._extract_with(info, b"x")))

    def test_symlinks_are_refused_even_pointing_inside(self):
        self.assertIn("link", str(self._extract_with(self._member("payload/link", tarfile.SYMTYPE, "../../../../etc/shadow"))))
        self.assertIn("link", str(self._extract_with(self._member("payload/inside", tarfile.SYMTYPE, "install.sh"))))

    def test_hard_links_are_refused(self):
        self.assertIn("link", str(self._extract_with(self._member("payload/hard", tarfile.LNKTYPE, "/etc/shadow"))))

    def test_devices_and_fifos_are_refused(self):
        for kind in (tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                self.assertIn("special", str(self._extract_with(self._member(f"payload/dev{kind!r}", kind))))

    def test_an_archive_with_too_many_entries_is_refused(self):
        original = rc.MAX_ARCHIVE_MEMBERS
        rc.MAX_ARCHIVE_MEMBERS = 5
        self.addCleanup(setattr, rc, "MAX_ARCHIVE_MEMBERS", original)
        package = write_tarball(self.root / "pkg.tar.gz", self.files)
        with self.assertRaises(rc.ReleaseCheckError):
            rc.safe_extract(package, self.root / "out")

    def test_a_changed_or_missing_file_fails_the_release_tree_check(self):
        package = write_tarball(self.root / "pkg.tar.gz", self.files)
        root = rc.safe_extract(package, self.root / "out")
        (root / "payload/vms/app/main.py").write_text("tampered", encoding="utf-8")
        with self.assertRaises(rc.ReleaseCheckError) as raised:
            rc.verify_release_tree(root, SimpleNamespace(version="1.2.0", build_id=BUILD_B))
        self.assertIn("hash", str(raised.exception))

    def test_release_env_must_name_the_signed_version_and_build(self):
        package = write_tarball(self.root / "pkg.tar.gz", self.files)
        root = rc.safe_extract(package, self.root / "out")
        for manifest in (SimpleNamespace(version="1.3.0", build_id=BUILD_B), SimpleNamespace(version="1.2.0", build_id="c" * 40)):
            with self.subTest(manifest=manifest), self.assertRaises(rc.ReleaseCheckError):
                rc.verify_release_tree(root, manifest)


class MigrationGateTests(unittest.TestCase):
    def _trees(self, current, new):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        (tmp / "current").mkdir()
        (tmp / "new").mkdir()
        (tmp / "current/db_migrations.py").write_text(current, encoding="utf-8")
        (tmp / "new/db_migrations.py").write_text(new, encoding="utf-8")
        return tmp / "current", tmp / "new"

    def test_additive_changes_pass(self):
        self.assertEqual(rc.find_unsafe_migrations(*self._trees(MIGRATIONS_V1, MIGRATIONS_ADDITIVE)), [])

    def test_destructive_changes_are_found(self):
        for statement in ("ALTER TABLE things DROP COLUMN note", "DROP TABLE things", "ALTER TABLE things RENAME TO stuff",
                          "ALTER TABLE things RENAME COLUMN a TO b", "ALTER TABLE things ADD COLUMN must TEXT NOT NULL"):
            with self.subTest(statement=statement):
                self.assertTrue(rc.find_unsafe_migrations(*self._trees(MIGRATIONS_V1, MIGRATIONS_V1 + f"X='{statement}'\n")))
        self.assertTrue(rc.find_unsafe_migrations(*self._trees(MIGRATIONS_V1, MIGRATIONS_DESTRUCTIVE)))

    def test_not_null_with_a_default_is_additive(self):
        new = MIGRATIONS_V1 + "X='ALTER TABLE things ADD COLUMN flag INTEGER NOT NULL DEFAULT 0'\n"
        self.assertEqual(rc.find_unsafe_migrations(*self._trees(MIGRATIONS_V1, new)), [])

    def test_a_destructive_statement_already_in_the_running_release_is_not_new(self):
        self.assertEqual(rc.find_unsafe_migrations(*self._trees(MIGRATIONS_DESTRUCTIVE, MIGRATIONS_DESTRUCTIVE)), [])


class LiveTreeTests(unittest.TestCase):
    """No persistent customer state may sit inside the swapped tree."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.live = self.tmp / "live"
        self.payload = self.tmp / "payload"
        for directory in (self.live / "app", self.payload / "app"):
            directory.mkdir(parents=True)
        for name in ("Dockerfile", "docker-compose.yml"):
            (self.live / name).write_text("x")
            (self.payload / name).write_text("x")

    def test_release_code_and_carried_over_items_are_fine(self):
        (self.live / "mediamtx").mkdir()
        (self.live / "__pycache__").mkdir()
        self.assertEqual(rc.preservation_problems(self.live, self.payload), [])

    def test_legacy_data_inside_the_tree_stops_the_update(self):
        for name in ("recordings", "data"):
            (self.live / name).mkdir()
            (self.live / name / "kept.bin").write_text("customer data")
        (self.live / ".env").write_text("SECRET=1")
        problems = rc.preservation_problems(self.live, self.payload)
        self.assertEqual(len(problems), 3)
        self.assertTrue(all("legacy data" in problem for problem in problems))

    def test_empty_legacy_folders_are_harmless(self):
        (self.live / "recordings").mkdir()
        self.assertEqual(rc.preservation_problems(self.live, self.payload), [])

    def test_anything_unknown_stops_the_update(self):
        (self.live / "customer-notes").mkdir()
        self.assertIn("unknown", rc.preservation_problems(self.live, self.payload)[0])


if __name__ == "__main__":
    unittest.main()
