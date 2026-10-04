"""A supported manual rollback after an in-app update (2026-10-04).

Before this, an in-app update kept the previous code and image but wrote no
rollback point, so `rollback.sh` restored the last *installer* rollback
point instead -- on the Ryzen test appliance that was the build from before
the 1.2.0 repair, not 1.2.1. Now the root applier writes a rollback point
for the release it replaced (code archive, image, database backup, version
and the installed-release record) in root's own directory, and rollback.sh
restores the whole release identity and refuses a stale point."""
import json
import os
import shutil
import subprocess
import tarfile
import textwrap
import unittest
from pathlib import Path

from software_update_helpers import BUILD_A, BUILD_B, BUILD_C
from test_software_update_root_applier import ApplierTestCase, ar

INSTALLER = Path(__file__).resolve().parents[2] / "installer"
BASH = shutil.which("bash")


def values(path: Path) -> dict:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)


class RollbackPointTests(ApplierTestCase):
    def test_a_healthy_update_leaves_a_complete_rollback_point_for_the_replaced_release(self):
        self.stage()
        result = self.applier().apply_staged()
        self.assertEqual(result["state"], "healthy", result.get("error"))

        point = Path(result["rollback_point"])
        latest = self.paths.rollback_dir / "latest.env"
        self.assertEqual(point.parent, self.paths.rollback_dir)
        self.assertEqual(latest.read_text(), point.read_text())
        v = values(point)
        self.assertEqual((v["ROLLBACK_COMMIT"], v["ROLLBACK_VERSION"], v["UPGRADE_TO_COMMIT"]), (BUILD_A, "1.1.0", BUILD_B))
        self.assertEqual(v["ROLLBACK_IMAGE"], f"anyaicam-vms:rollback-{BUILD_A[:12]}")
        self.assertEqual(v["CREATED_BY"], "software_update")
        self.assertTrue(v["ROLLBACK_DATABASE_BACKUP"].endswith(".db"))
        # The saved record is the one from BEFORE the update.
        self.assertEqual(json.loads(Path(v["ROLLBACK_MARKER"]).read_text())["release_version"], "1.1.0")
        # The archive is the replaced release, laid out as rollback.sh expects, without state or secrets.
        with tarfile.open(v["ROLLBACK_CODE_ARCHIVE"]) as archive:
            names = archive.getnames()
            identity = json.loads(archive.extractfile("anyaicam/app/static/release-identity.json").read())
        self.assertEqual(identity["build_id"], BUILD_A)
        self.assertFalse([n for n in names if n.startswith("anyaicam/mediamtx") or "__pycache__" in n])
        # The live release is untouched by writing the point.
        self.assertEqual(self.marker()["release_version"], "1.2.0")

    def test_a_failed_update_writes_no_rollback_point(self):
        self.stage("1.3.0", BUILD_C, app_files={"BROKEN": "fails /health"})
        result = self.applier().apply_staged()
        self.assertEqual(result["state"], "rolled_back")
        self.assertFalse((self.paths.rollback_dir / "latest.env").exists())

    def test_a_rollback_point_problem_never_fails_the_update(self):
        self.stage()
        applier = self.applier()
        applier._archive_previous = lambda archive: (_ for _ in ()).throw(OSError("No space left on device"))
        result = applier.apply_staged()
        self.assertEqual(result["state"], "healthy")
        self.assertTrue(result["rollback_point"].startswith("unavailable: OSError: No space left on device"))
        self.assertFalse((self.paths.rollback_dir / "latest.env").exists())
        self.assertEqual(self.marker()["release_version"], "1.2.0")

    def test_old_points_are_pruned_but_never_the_installers(self):
        self.paths.rollback_dir.mkdir(parents=True)
        installer_point = self.paths.rollback_dir / "rollback-installer-20260101T000000Z.env"
        installer_point.write_text("ROLLBACK_COMMIT=" + "e" * 40 + "\nCREATED_BY=installer\nROLLBACK_CREATED_AT=20260101T000000Z\n")
        builds = [("1.2.0", BUILD_B), ("1.3.0", BUILD_C), ("1.4.0", "d" * 40), ("1.5.0", "f" * 40)]
        for version, build in builds:
            self.stage(version, build)
            self.assertEqual(self.applier().apply_staged()["state"], "healthy")
        mine = [p for p in self.paths.rollback_dir.glob("rollback-*.env") if values(p).get("CREATED_BY") == "software_update"]
        self.assertEqual(len(mine), ar.Applier.ROLLBACK_POINTS_KEPT)
        self.assertEqual(sorted(values(p)["UPGRADE_TO_COMMIT"] for p in mine), sorted([BUILD_C, "d" * 40, "f" * 40]))
        self.assertTrue(installer_point.exists())
        kept_archives = {values(p)["ROLLBACK_CODE_ARCHIVE"] for p in mine}
        self.assertEqual({str(p) for p in self.paths.rollback_dir.glob("vms-code-*.tar.gz")}, kept_archives)
        self.assertEqual(values(self.paths.rollback_dir / "latest.env")["UPGRADE_TO_COMMIT"], "f" * 40)


STUBS = {
    "docker": '#!/usr/bin/env bash\necho "docker $*" >> "$STUB_LOG"\nexit 0\n',
    "systemctl": '#!/usr/bin/env bash\necho "systemctl $*" >> "$STUB_LOG"\n',
}


@unittest.skipUnless(BASH and shutil.which("rsync") and os.name == "posix", "needs bash and rsync on Linux (as on an appliance)")
class RollbackScriptRoundTripTests(ApplierTestCase):
    """The applier's point, restored by the real installer/rollback.sh."""

    def setUp(self):
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in STUBS.items():
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.log = self.tmp / "stub.log"
        self.stage()
        self.assertEqual(self.applier().apply_staged()["state"], "healthy")

    def rollback(self, *args):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", STUB_LOG=str(self.log),
                   ANYAICAM_ROLLBACK_ALLOW_NON_ROOT="1", ANYAICAM_ROLLBACK_VALIDATE_SECONDS="0",
                   VMS_INSTALL_ROOT=str(self.paths.live), VMS_ENV_FILE=str(self.paths.vms_env),
                   VMS_RELEASE_MARKER=str(self.paths.release_marker), ANYAICAM_UPDATE_STATE_DIR=str(self.paths.root_state),
                   VMS_RECORDINGS_DIR=str(self.tmp / "recordings"), ANYAICAM_LEGACY_ROLLBACK_DIR=str(self.tmp / "legacy"))
        return subprocess.run([BASH, str(INSTALLER / "rollback.sh"), "--yes", *args], text=True, capture_output=True, env=env)

    def test_rollback_returns_to_the_release_the_update_replaced(self):
        done = self.rollback()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        identity = json.loads((self.paths.live / "app/static/release-identity.json").read_text())
        self.assertEqual((identity["version"], identity["build_id"]), ("1.1.0", BUILD_A))
        env = values(self.paths.vms_env)
        self.assertEqual((env["ANYAICAM_VERSION"], env["ANYAICAM_BUILD_ID"], env["ANYAICAM_VMS_COMMIT"]), ("1.1.0", BUILD_A, BUILD_A))
        self.assertEqual(env["ANYAICAM_APP_SECRETS"], "keep-me")
        self.assertEqual((self.marker()["release_version"], self.marker()["vms_release_commit"]), ("1.1.0", BUILD_A))
        root_record = json.loads(self.paths.installed_record.read_text())
        self.assertEqual((root_record["release_version"], root_record["vms_release_commit"]), ("1.1.0", BUILD_A))
        self.assertIn(f"docker tag anyaicam-vms:rollback-{BUILD_A[:12]} anyaicam-vms:latest", self.log.read_text())
        self.assertEqual((self.paths.live / "mediamtx" / "mediamtx").read_text(), "binary")

        # And the newer release can be installed again afterwards (root's record says 1.1.0 now).
        self.host.run(["systemctl", "start", "anyaicam-vms.service"], 1)
        self.stage("1.2.0", BUILD_B, manifest_overrides={"update_id": "1.2.0-reinstall"})
        self.assertEqual(self.applier().apply_staged()["state"], "healthy")

    def test_a_point_for_another_release_is_refused_unless_allowed(self):
        self.paths.vms_env.write_text(self.paths.vms_env.read_text().replace(f"ANYAICAM_BUILD_ID={BUILD_B}",
                                                                             f"ANYAICAM_BUILD_ID={BUILD_C}"))
        refused = self.rollback()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("Restoring it would skip releases", refused.stderr)
        self.assertFalse(self.log.exists() and "systemctl stop" in self.log.read_text())
        self.assertEqual(self.marker()["release_version"], "1.2.0")
        allowed = self.rollback("--allow-stale")
        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)
        self.assertEqual(self.marker()["release_version"], "1.1.0")

    def test_a_symlinked_manifest_is_refused(self):
        real = self.paths.rollback_dir / "latest.env"
        planted = self.tmp / "planted.env"
        planted.write_text(real.read_text())
        real.unlink()
        real.symlink_to(planted)
        refused = self.rollback()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("not a regular file", refused.stderr)


if __name__ == "__main__":
    unittest.main()
