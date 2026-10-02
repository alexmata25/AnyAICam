"""RDM-1: focused tests for anyaicam_agent.updater.installer -- candidate
extraction, pointer-file activation (the activation boundary), and
version/staging housekeeping.

All I/O is against real tar files and real directories inside a per-test
temporary directory -- no network, no AWS, no real device files. This
module does real filesystem work by design (it IS the filesystem layer),
so unlike updater/source.py there is no fake/mock double: tests exercise
the real implementation against tempfile.TemporaryDirectory() trees.
"""

import os
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

APPLIANCE_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(APPLIANCE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(APPLIANCE_AGENT_DIR))

from anyaicam_agent.updater.installer import (
    InstallError,
    VersionAlreadyInstalled,
    VersionNotInstalled,
    activate,
    cleanup_orphaned_candidates,
    current_version,
    install_candidate,
    prune_old_versions,
)


def _make_tar(path: Path, entries):
    """entries: list of (name, bytes) for regular files, or
    (name, None, symlink_target) for a symlink entry."""
    with tarfile.open(path, mode="w") as tar:
        for entry in entries:
            if len(entry) == 2:
                name, data = entry
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                import io
                tar.addfile(info, io.BytesIO(data))
            else:
                name, _, link_target = entry
                info = tarfile.TarInfo(name=name)
                info.type = tarfile.SYMTYPE
                info.linkname = link_target
                tar.addfile(info)
    return path


class InstallerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.versions_dir = self.root / "updates" / "versions"
        self.staging_dir = self.root / "updates" / "staging"
        self.pointer_file = self.root / "updates" / "current_version.txt"

    def good_package(self, name="package.tar", entries=None):
        entries = entries if entries is not None else [("bin/app", b"binarycontent"), ("VERSION", b"1.2.0")]
        return _make_tar(self.root / name, entries)


class ValidationTests(InstallerTestCase):
    def test_install_candidate_rejects_empty_version(self):
        package = self.good_package()
        with self.assertRaises(ValueError):
            install_candidate(package, "", self.versions_dir, self.staging_dir)

    def test_install_candidate_rejects_version_with_slash(self):
        package = self.good_package()
        with self.assertRaises(ValueError):
            install_candidate(package, "1.2/0", self.versions_dir, self.staging_dir)

    def test_install_candidate_rejects_dot_dot_version(self):
        package = self.good_package()
        with self.assertRaises(ValueError):
            install_candidate(package, "..", self.versions_dir, self.staging_dir)

    def test_activate_rejects_invalid_version(self):
        with self.assertRaises(ValueError):
            activate("../escape", self.versions_dir, self.pointer_file)


class InstallCandidateSuccessTests(InstallerTestCase):
    def test_extracts_expected_files_and_returns_target_dir(self):
        package = self.good_package()
        target = install_candidate(package, "1.2.0", self.versions_dir, self.staging_dir)
        self.assertEqual(target, self.versions_dir / "1.2.0")
        self.assertEqual((target / "VERSION").read_bytes(), b"1.2.0")
        self.assertEqual((target / "bin" / "app").read_bytes(), b"binarycontent")

    def test_staging_dir_is_empty_after_a_successful_install(self):
        package = self.good_package()
        install_candidate(package, "1.2.0", self.versions_dir, self.staging_dir)
        self.assertEqual(list(self.staging_dir.iterdir()), [])

    def test_two_different_versions_can_both_be_installed(self):
        install_candidate(self.good_package("p1.tar"), "1.0.0", self.versions_dir, self.staging_dir)
        install_candidate(self.good_package("p2.tar"), "1.1.0", self.versions_dir, self.staging_dir)
        self.assertTrue((self.versions_dir / "1.0.0").is_dir())
        self.assertTrue((self.versions_dir / "1.1.0").is_dir())


class InstallCandidateFailureTests(InstallerTestCase):
    def test_already_installed_version_raises_and_leaves_staging_empty(self):
        package = self.good_package()
        install_candidate(package, "1.2.0", self.versions_dir, self.staging_dir)
        with self.assertRaises(VersionAlreadyInstalled):
            install_candidate(package, "1.2.0", self.versions_dir, self.staging_dir)
        self.assertEqual(list(self.staging_dir.iterdir()), [])

    def test_missing_package_file_raises_install_error(self):
        with self.assertRaises(InstallError):
            install_candidate(self.root / "does-not-exist.tar", "1.2.0", self.versions_dir, self.staging_dir)
        self.assertFalse((self.versions_dir / "1.2.0").exists())

    def test_corrupt_non_tar_file_raises_install_error_and_cleans_staging(self):
        bad = self.root / "not-a-tar.tar"
        bad.write_bytes(b"this is not a tar archive")
        with self.assertRaises(InstallError):
            install_candidate(bad, "1.2.0", self.versions_dir, self.staging_dir)
        self.assertFalse((self.versions_dir / "1.2.0").exists())
        self.assertEqual(list(self.staging_dir.iterdir()) if self.staging_dir.exists() else [], [])

    def test_path_traversal_entry_is_rejected_and_nothing_escapes(self):
        package = self.good_package("evil.tar", entries=[("../escaped.txt", b"pwn")])
        with self.assertRaises(InstallError):
            install_candidate(package, "1.2.0", self.versions_dir, self.staging_dir)
        self.assertFalse((self.versions_dir / "1.2.0").exists())
        self.assertFalse((self.staging_dir.parent / "escaped.txt").exists())
        self.assertFalse((self.root / "escaped.txt").exists())
        self.assertEqual(list(self.staging_dir.iterdir()) if self.staging_dir.exists() else [], [])

    def test_absolute_path_entry_is_safely_contained_not_escaped(self):
        # tarfile's filter="data" re-roots absolute-looking entries under
        # the destination rather than raising -- proving containment
        # rather than rejection is the correct expectation here.
        package = self.good_package("abs.tar", entries=[("/etc/evil.txt", b"pwn")])
        target = install_candidate(package, "1.2.0", self.versions_dir, self.staging_dir)
        self.assertTrue((target / "etc" / "evil.txt").exists())
        self.assertFalse(Path("/etc/evil.txt").exists())

    def test_malicious_absolute_symlink_entry_is_rejected(self):
        package = self.good_package("link.tar", entries=[("link", None, "/etc/passwd")])
        with self.assertRaises(InstallError):
            install_candidate(package, "1.2.0", self.versions_dir, self.staging_dir)
        self.assertFalse((self.versions_dir / "1.2.0").exists())
        self.assertEqual(list(self.staging_dir.iterdir()) if self.staging_dir.exists() else [], [])


class CurrentVersionTests(InstallerTestCase):
    def test_returns_empty_string_when_pointer_file_is_missing(self):
        self.assertEqual(current_version(self.pointer_file), "")

    def test_returns_the_activated_version(self):
        install_candidate(self.good_package(), "1.2.0", self.versions_dir, self.staging_dir)
        activate("1.2.0", self.versions_dir, self.pointer_file)
        self.assertEqual(current_version(self.pointer_file), "1.2.0")


class ActivateTests(InstallerTestCase):
    def test_activating_an_uninstalled_version_raises(self):
        with self.assertRaises(VersionNotInstalled):
            activate("1.2.0", self.versions_dir, self.pointer_file)

    def test_first_activation_returns_empty_previous_version(self):
        install_candidate(self.good_package(), "1.0.0", self.versions_dir, self.staging_dir)
        previous = activate("1.0.0", self.versions_dir, self.pointer_file)
        self.assertEqual(previous, "")
        self.assertEqual(current_version(self.pointer_file), "1.0.0")

    def test_second_activation_returns_the_prior_version(self):
        install_candidate(self.good_package("p1.tar"), "1.0.0", self.versions_dir, self.staging_dir)
        install_candidate(self.good_package("p2.tar"), "1.1.0", self.versions_dir, self.staging_dir)
        activate("1.0.0", self.versions_dir, self.pointer_file)
        previous = activate("1.1.0", self.versions_dir, self.pointer_file)
        self.assertEqual(previous, "1.0.0")
        self.assertEqual(current_version(self.pointer_file), "1.1.0")

    def test_no_temporary_sibling_survives_activation(self):
        install_candidate(self.good_package(), "1.0.0", self.versions_dir, self.staging_dir)
        activate("1.0.0", self.versions_dir, self.pointer_file)
        temp_sibling = self.pointer_file.with_suffix(self.pointer_file.suffix + ".tmp")
        self.assertFalse(temp_sibling.exists())

    def test_rollback_is_just_activating_the_previous_version_again(self):
        install_candidate(self.good_package("p1.tar"), "1.0.0", self.versions_dir, self.staging_dir)
        install_candidate(self.good_package("p2.tar"), "1.1.0", self.versions_dir, self.staging_dir)
        activate("1.0.0", self.versions_dir, self.pointer_file)
        activate("1.1.0", self.versions_dir, self.pointer_file)
        self.assertEqual(current_version(self.pointer_file), "1.1.0")

        # "Rollback": call activate() again with the version we came from.
        rolled_back_from = activate("1.0.0", self.versions_dir, self.pointer_file)
        self.assertEqual(rolled_back_from, "1.1.0")
        self.assertEqual(current_version(self.pointer_file), "1.0.0")


class PruneOldVersionsTests(InstallerTestCase):
    def _install_with_mtime(self, version: str, mtime: float):
        target = install_candidate(self.good_package(f"{version}.tar"), version, self.versions_dir, self.staging_dir)
        os.utime(target, (mtime, mtime))
        return target

    def test_missing_versions_dir_returns_empty_list(self):
        self.assertEqual(prune_old_versions(self.versions_dir, self.pointer_file), [])

    def test_empty_versions_dir_returns_empty_list(self):
        self.versions_dir.mkdir(parents=True)
        self.assertEqual(prune_old_versions(self.versions_dir, self.pointer_file), [])

    def test_never_removes_the_active_version_even_if_oldest(self):
        now = time.time()
        self._install_with_mtime("1.0.0", now - 1000)  # oldest, but active
        self._install_with_mtime("1.1.0", now - 500)
        self._install_with_mtime("1.2.0", now)
        activate("1.0.0", self.versions_dir, self.pointer_file)

        removed = prune_old_versions(self.versions_dir, self.pointer_file, keep=0)
        self.assertNotIn("1.0.0", removed)
        self.assertTrue((self.versions_dir / "1.0.0").is_dir())

    def test_keeps_the_n_most_recently_installed_inactive_versions(self):
        now = time.time()
        self._install_with_mtime("1.0.0", now - 300)
        self._install_with_mtime("1.1.0", now - 200)
        self._install_with_mtime("1.2.0", now - 100)
        self._install_with_mtime("1.3.0", now)
        activate("1.3.0", self.versions_dir, self.pointer_file)  # active; never a removal candidate

        removed = prune_old_versions(self.versions_dir, self.pointer_file, keep=1)
        # Active (1.3.0) kept always; of the remaining three inactive
        # ones (1.0.0, 1.1.0, 1.2.0), keep=1 keeps the newest (1.2.0).
        self.assertEqual(set(removed), {"1.0.0", "1.1.0"})
        self.assertTrue((self.versions_dir / "1.2.0").is_dir())
        self.assertTrue((self.versions_dir / "1.3.0").is_dir())
        self.assertFalse((self.versions_dir / "1.0.0").exists())
        self.assertFalse((self.versions_dir / "1.1.0").exists())

    def test_default_keep_is_two(self):
        now = time.time()
        self._install_with_mtime("1.0.0", now - 300)
        self._install_with_mtime("1.1.0", now - 200)
        self._install_with_mtime("1.2.0", now - 100)
        activate("1.2.0", self.versions_dir, self.pointer_file)
        # Only one inactive version (1.0.0, 1.1.0) beyond the active one
        # -- both fit within the default keep=2, so nothing is removed.
        removed = prune_old_versions(self.versions_dir, self.pointer_file)
        self.assertEqual(removed, [])


class CleanupOrphanedCandidatesTests(InstallerTestCase):
    def test_missing_staging_dir_returns_empty_list(self):
        self.assertEqual(cleanup_orphaned_candidates(self.staging_dir), [])

    def test_empty_staging_dir_returns_empty_list(self):
        self.staging_dir.mkdir(parents=True)
        self.assertEqual(cleanup_orphaned_candidates(self.staging_dir), [])

    def test_removes_leftover_candidate_directories(self):
        self.staging_dir.mkdir(parents=True)
        orphan = self.staging_dir / "1.2.0.deadbeef"
        orphan.mkdir()
        (orphan / "partial-file").write_bytes(b"incomplete")

        removed = cleanup_orphaned_candidates(self.staging_dir)
        self.assertEqual(removed, ["1.2.0.deadbeef"])
        self.assertFalse(orphan.exists())

    def test_does_not_touch_versions_dir(self):
        install_candidate(self.good_package(), "1.0.0", self.versions_dir, self.staging_dir)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        (self.staging_dir / "orphan-file").write_bytes(b"debris")

        cleanup_orphaned_candidates(self.staging_dir)
        self.assertTrue((self.versions_dir / "1.0.0").is_dir())


class LifecycleEndToEndTests(InstallerTestCase):
    def test_install_activate_upgrade_and_rollback_sequence(self):
        install_candidate(self.good_package("p1.tar"), "1.0.0", self.versions_dir, self.staging_dir)
        activate("1.0.0", self.versions_dir, self.pointer_file)
        self.assertEqual(current_version(self.pointer_file), "1.0.0")

        install_candidate(self.good_package("p2.tar"), "1.1.0", self.versions_dir, self.staging_dir)
        previous = activate("1.1.0", self.versions_dir, self.pointer_file)
        self.assertEqual(previous, "1.0.0")
        self.assertEqual(current_version(self.pointer_file), "1.1.0")

        # Simulated post-activation health-check failure -> rollback.
        rolled_back_from = activate(previous, self.versions_dir, self.pointer_file)
        self.assertEqual(rolled_back_from, "1.1.0")
        self.assertEqual(current_version(self.pointer_file), "1.0.0")

        # Both version directories survive a rollback -- installer
        # itself never deletes a version; that is prune_old_versions()'s
        # separate, explicit job.
        self.assertTrue((self.versions_dir / "1.0.0").is_dir())
        self.assertTrue((self.versions_dir / "1.1.0").is_dir())


if __name__ == "__main__":
    unittest.main()
