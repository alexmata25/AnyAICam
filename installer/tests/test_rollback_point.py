"""Automatic rollback point on repair/upgrade, and rollback.sh (2026-09-25).

Before this, a repair rebuilt anyaicam-vms:latest in place and rsync
--delete replaced the code: the previous release survived only if someone
tagged and archived it by hand first. docker/systemctl are stubs that log
their calls; nothing here needs Docker, root, or a real appliance."""
import os
import shutil
import subprocess
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = os.environ.get("TEST_BASH") or shutil.which("bash")
OLD = "a" * 40


def bash_path(path) -> str:
    """A path as bash sees it: on Windows Git Bash a drive path becomes
    /c/... (GNU tar reads "C:" as a remote host); unchanged elsewhere."""
    text = Path(path).as_posix()
    if os.name == "nt" and len(text) > 1 and text[1] == ":":
        text = "/" + text[0].lower() + text[2:]
    return text
NEW = "b" * 40


def from_bash(text: str) -> Path:
    """The reverse of bash_path(), for paths bash wrote into a manifest."""
    if os.name == "nt" and len(text) > 2 and text[0] == "/" and text[2] == "/":
        text = text[1].upper() + ":" + text[2:]
    return Path(text)

DOCKER_STUB = textwrap.dedent('''\
    #!/usr/bin/env bash
    echo "docker $*" >> "$STUB_LOG"
    case "$1 $2" in
      "image inspect") [[ "${FAKE_IMAGE_EXISTS:-1}" == "1" ]] && exit 0 || exit 1 ;;
      "tag "*) [[ "${FAKE_TAG_FAILS:-0}" == "1" ]] && exit 1 || exit 0 ;;
    esac
    if [[ "$1" == "inspect" ]]; then echo "${FAKE_RUNNING:-true}"; exit 0; fi
    if [[ "$1" == "exec" ]]; then
      name="${@: -1}"; printf 'online-backup' > "$FAKE_RECORDINGS/$name"; exit 0
    fi
    exit 0
    ''')
SYSTEMCTL_STUB = '#!/usr/bin/env bash\necho "systemctl $*" >> "$STUB_LOG"\n'


@unittest.skipUnless(BASH, "bash is required")
class RollbackPointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in (("docker", DOCKER_STUB), ("systemctl", SYSTEMCTL_STUB)):
            path = self.bin / name
            path.write_text(body, newline="\n")
            path.chmod(0o755)
        self.install_root = self.tmp / "opt" / "anyaicam"
        (self.install_root / "app" / "static" / "hls").mkdir(parents=True)
        (self.install_root / "app" / "main.py").write_text("OLD RELEASE CODE\n")
        (self.install_root / "app" / "static" / "hls" / "camera1.ts").write_text("live segment")
        (self.install_root / "app" / "auto.key").write_text("PRIVATE KEY")
        (self.install_root / "recordings").mkdir()
        (self.install_root / "recordings" / "legacy.mkv").write_text("footage")
        (self.install_root / ".env").write_text("SECRET=1\n")
        self.recordings = self.tmp / "recordings"
        self.recordings.mkdir()
        (self.recordings / "partner_portal.db").write_text("live database")
        self.env_file = self.tmp / "vms.env"
        self.env_file.write_text(f"ANYAICAM_VMS_COMMIT={OLD}\nANYAICAM_BUILD_ID={OLD}\nOTHER=kept\n")
        self.rollback_dir = self.tmp / "rollback"
        self.log = self.tmp / "stub.log"
        self.marker = self.tmp / "vms_release.json"
        self.marker.write_text('{"vms_release_commit": "%s", "release_version": "1.2.1", "installer_version": "1.2.1"}' % OLD)

    def env(self, **extra):
        env = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}", STUB_LOG=str(self.log),
                   FAKE_RECORDINGS=str(self.recordings), ANYAICAM_ROLLBACK_DIR=bash_path(self.rollback_dir))
        env.update(extra)
        return env

    def run_bash(self, script, success=True, **extra):
        prelude = textwrap.dedent(f'''\
            set -euo pipefail
            log() {{ echo "LOG: $*"; }}
            source ./06-deploy-vms.sh
            VMS_INSTALL_ROOT="{bash_path(self.install_root)}"
            VMS_ENV_FILE="{bash_path(self.env_file)}"
            VMS_RECORDINGS_DIR="{bash_path(self.recordings)}"
            VMS_DATA_CONFIG_DIR="{bash_path(self.tmp / "data-config")}"
            VMS_RELEASE_COMMIT={NEW}
            VMS_RELEASE_MARKER="{bash_path(self.marker)}"
            ''')
        result = subprocess.run([BASH, "-c", prelude + script], cwd=ROOT, text=True, capture_output=True, env=self.env(**extra))
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def rollback_paths(self, **extra):
        state = self.tmp / "update-state"
        state.mkdir(exist_ok=True)
        paths = dict(VMS_INSTALL_ROOT=bash_path(self.install_root), VMS_ENV_FILE=bash_path(self.env_file),
                     VMS_RECORDINGS_DIR=bash_path(self.recordings), ANYAICAM_ROLLBACK_ALLOW_NON_ROOT="1",
                     VMS_RELEASE_MARKER=bash_path(self.marker), ANYAICAM_UPDATE_STATE_DIR=bash_path(state),
                     ANYAICAM_LEGACY_ROLLBACK_DIR=bash_path(self.tmp / "legacy-rollback"),
                     ANYAICAM_ROLLBACK_VALIDATE_SECONDS="0")
        paths.update(extra)
        return paths

    def manifest(self):
        return dict(line.split("=", 1) for line in (self.rollback_dir / "latest.env").read_text().splitlines())

    def test_an_upgrade_tags_the_image_archives_the_code_and_backs_up_the_database_online(self):
        self.run_bash("create_rollback_point")
        values = self.manifest()
        self.assertEqual(values["ROLLBACK_COMMIT"], OLD)
        self.assertEqual(values["UPGRADE_TO_COMMIT"], NEW)
        self.assertEqual(values["ROLLBACK_IMAGE"], f"anyaicam-vms:rollback-{OLD[:12]}")
        self.assertIn(f"docker tag anyaicam-vms:latest anyaicam-vms:rollback-{OLD[:12]}", self.log.read_text())
        backup = from_bash(values["ROLLBACK_DATABASE_BACKUP"])
        self.assertEqual(backup.read_text(), "online-backup")  # through the running container, not a raw copy
        self.assertTrue(backup.name.startswith(f"partner_portal-pre-{NEW[:12]}-"))
        with tarfile.open(from_bash(values["ROLLBACK_CODE_ARCHIVE"])) as archive:
            names = archive.getnames()
        self.assertIn("anyaicam/app/main.py", names)
        for excluded in ("anyaicam/recordings/legacy.mkv", "anyaicam/.env", "anyaicam/app/auto.key", "anyaicam/app/static/hls/camera1.ts"):
            self.assertNotIn(excluded, names)
        self.assertEqual((self.recordings / "partner_portal.db").read_text(), "live database")  # untouched

    def test_a_stopped_vms_is_backed_up_by_copying_the_database_and_its_wal(self):
        (self.recordings / "partner_portal.db-wal").write_text("wal pages")
        self.run_bash("create_rollback_point", FAKE_RUNNING="false")
        backup = from_bash(self.manifest()["ROLLBACK_DATABASE_BACKUP"])
        self.assertEqual(backup.read_text(), "live database")
        self.assertEqual(Path(str(backup) + "-wal").read_text(), "wal pages")

    def test_no_previous_image_or_database_is_recorded_as_none_not_a_failure(self):
        (self.recordings / "partner_portal.db").unlink()
        self.run_bash("create_rollback_point", FAKE_IMAGE_EXISTS="0")
        values = self.manifest()
        self.assertEqual((values["ROLLBACK_IMAGE"], values["ROLLBACK_DATABASE_BACKUP"]), ("none", "none"))

    def test_the_upgrade_stops_before_changing_anything_when_no_rollback_point_can_be_made(self):
        result = self.run_bash('''
VMS_PAYLOAD_DIR="$(mktemp -d)"; mkdir -p "$VMS_PAYLOAD_DIR/app"
migrate_legacy_persistent_data() { :; }; migrate_legacy_persistent_file() { :; }
install() { echo "INSTALL-REACHED"; return 0; }
deploy_vms existing
''', success=False, FAKE_TAG_FAILS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not create a rollback point", result.stderr)
        self.assertNotIn("INSTALL-REACHED", result.stdout)
        self.assertEqual((self.install_root / "app" / "main.py").read_text(), "OLD RELEASE CODE\n")

    def test_a_clean_install_takes_no_rollback_point(self):
        self.run_bash('''
VMS_PAYLOAD_DIR="$(mktemp -d)"; mkdir -p "$VMS_PAYLOAD_DIR/app"
migrate_legacy_persistent_data() { :; }; migrate_legacy_persistent_file() { :; }
create_rollback_point() { echo "ROLLBACK-CALLED"; }
install() { exit 0; }
deploy_vms clean
''')
        self.assertFalse((self.rollback_dir / "latest.env").exists())

    @unittest.skipUnless(shutil.which("rsync"), "rsync is required to run rollback.sh")
    def test_rollback_sh_restores_code_image_and_commit_and_keeps_the_database_unless_asked(self):
        self.run_bash("create_rollback_point")
        (self.install_root / "app" / "main.py").write_text("NEW RELEASE CODE\n")
        (self.recordings / "partner_portal.db").write_text("database after upgrade")
        self.env_file.write_text(f"ANYAICAM_VMS_COMMIT={NEW}\nANYAICAM_BUILD_ID={NEW}\nOTHER=kept\n")
        paths = self.rollback_paths()
        result = subprocess.run([BASH, "./rollback.sh", "--yes"], cwd=ROOT, text=True, capture_output=True, env=self.env(**paths))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.install_root / "app" / "main.py").read_text(), "OLD RELEASE CODE\n")
        self.assertIn(f"docker tag anyaicam-vms:rollback-{OLD[:12]} anyaicam-vms:latest", self.log.read_text())
        self.assertIn("systemctl stop anyaicam-vms.service", self.log.read_text())
        self.assertIn(f"ANYAICAM_VMS_COMMIT={OLD}", self.env_file.read_text())
        self.assertIn("OTHER=kept", self.env_file.read_text())
        self.assertEqual((self.recordings / "partner_portal.db").read_text(), "database after upgrade")
        self.assertEqual((self.install_root / "recordings" / "legacy.mkv").read_text(), "footage")
        self.assertEqual((self.install_root / ".env").read_text(), "SECRET=1\n")

        result = subprocess.run([BASH, "./rollback.sh", "--yes", "--restore-database"], cwd=ROOT, text=True, capture_output=True, env=self.env(**paths))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.recordings / "partner_portal.db").read_text(), "online-backup")
        # Replaced, never deleted -- and kept root-only beside the rollback
        # points, not in the service user's recordings folder (2026-10-06).
        kept = list(self.rollback_dir.glob("replaced-database-*/partner_portal.db"))
        self.assertEqual([p.read_text() for p in kept], ["database after upgrade"])
        self.assertFalse([p for p in self.recordings.iterdir() if "before-rollback" in p.name])

    def test_rollback_sh_refuses_a_missing_or_incomplete_rollback_point(self):
        env = self.env(ANYAICAM_ROLLBACK_ALLOW_NON_ROOT="1")
        missing = subprocess.run([BASH, "./rollback.sh", "--yes", bash_path(self.tmp / "nope.env")], cwd=ROOT, text=True, capture_output=True, env=env)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("manifest not found", missing.stderr)
        bad = self.tmp / "bad.env"
        bad.write_text("ROLLBACK_COMMIT=not-a-commit\n")
        refused = subprocess.run([BASH, "./rollback.sh", "--yes", bash_path(bad)], cwd=ROOT, text=True, capture_output=True, env=env)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(self.log.read_text() if self.log.exists() else "", "")  # nothing stopped or tagged

    # ------------------------------------------------------------------ release identity (2026-10-04)

    def test_the_rollback_point_records_the_release_version_and_the_installed_record(self):
        self.env_file.write_text(f"ANYAICAM_VERSION=1.2.1\nANYAICAM_VMS_COMMIT={OLD}\nANYAICAM_BUILD_ID={OLD}\n")
        self.run_bash("create_rollback_point")
        values = self.manifest()
        self.assertEqual((values["ROLLBACK_VERSION"], values["CREATED_BY"]), ("1.2.1", "installer"))
        saved = from_bash(values["ROLLBACK_MARKER"])
        self.assertEqual(saved.parent, self.rollback_dir)
        self.assertIn('"release_version": "1.2.1"', saved.read_text())

    def test_by_default_rollback_points_live_in_roots_update_state_directory(self):
        state = self.tmp / "update-state-default"
        result = self.run_bash('echo "DIR=$ROLLBACK_DIR"', ANYAICAM_ROLLBACK_DIR="", UPDATE_STATE_DIR=bash_path(state))
        self.assertIn(f"DIR={bash_path(state)}/rollback", result.stdout)

    @unittest.skipUnless(shutil.which("rsync"), "rsync is required to run rollback.sh")
    def test_rollback_sh_restores_the_version_and_the_installed_record(self):
        self.env_file.write_text(f"ANYAICAM_VERSION=1.2.1\nANYAICAM_VMS_COMMIT={OLD}\nANYAICAM_BUILD_ID={OLD}\nOTHER=kept\n")
        self.run_bash("create_rollback_point")
        # Upgraded to 1.2.2.
        self.env_file.write_text(f"ANYAICAM_VERSION=1.2.2\nANYAICAM_VMS_COMMIT={NEW}\nANYAICAM_BUILD_ID={NEW}\nOTHER=kept\n")
        self.marker.write_text('{"vms_release_commit": "%s", "release_version": "1.2.2"}' % NEW)
        paths = self.rollback_paths()
        result = subprocess.run([BASH, "./rollback.sh", "--yes"], cwd=ROOT, text=True, capture_output=True, env=self.env(**paths))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        env = self.env_file.read_text()
        self.assertIn("ANYAICAM_VERSION=1.2.1", env)
        self.assertIn(f"ANYAICAM_BUILD_ID={OLD}", env)
        self.assertIn("OTHER=kept", env)
        self.assertIn('"release_version": "1.2.1"', self.marker.read_text())
        root_record = from_bash(paths["ANYAICAM_UPDATE_STATE_DIR"]) / "installed_release.json"
        self.assertIn(f'"vms_release_commit": "{OLD}"', root_record.read_text())

    @unittest.skipUnless(shutil.which("rsync"), "rsync is required to run rollback.sh")
    def test_rolling_back_to_a_release_without_a_version_drops_the_version_key(self):
        self.marker.unlink()
        self.run_bash("create_rollback_point")
        self.env_file.write_text(f"ANYAICAM_VERSION=1.2.0\nANYAICAM_VMS_COMMIT={NEW}\nANYAICAM_BUILD_ID={NEW}\n")
        paths = self.rollback_paths()
        state = from_bash(paths["ANYAICAM_UPDATE_STATE_DIR"])
        (state / "installed_release.json").write_text('{"release_version": "1.2.0"}')
        result = subprocess.run([BASH, "./rollback.sh", "--yes"], cwd=ROOT, text=True, capture_output=True, env=self.env(**paths))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("ANYAICAM_VERSION=", self.env_file.read_text())
        self.assertFalse((state / "installed_release.json").exists())
        self.assertIn(f'"vms_release_commit": "{OLD}"', self.marker.read_text())

    def test_rollback_sh_refuses_a_point_taken_for_a_different_running_release(self):
        self.run_bash("create_rollback_point")
        other = "c" * 40
        self.env_file.write_text(f"ANYAICAM_VMS_COMMIT={other}\nANYAICAM_BUILD_ID={other}\n")
        result = subprocess.run([BASH, "./rollback.sh", "--yes"], cwd=ROOT, text=True, capture_output=True,
                                env=self.env(**self.rollback_paths()))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Restoring it would skip releases", result.stderr)
        self.assertNotIn("systemctl stop", self.log.read_text() if self.log.exists() else "")
        self.assertEqual((self.install_root / "app" / "main.py").read_text(), "OLD RELEASE CODE\n")

    def test_rollback_sh_falls_back_to_the_legacy_location_only_when_nothing_newer_exists(self):
        legacy = self.tmp / "legacy-rollback"
        legacy.mkdir()
        (legacy / "latest.env").write_text("ROLLBACK_COMMIT=not-valid\n")
        paths = self.rollback_paths(ANYAICAM_ROLLBACK_DIR=bash_path(self.tmp / "empty-rollback"))
        result = subprocess.run([BASH, "./rollback.sh", "--yes"], cwd=ROOT, text=True, capture_output=True, env=self.env(**paths))
        self.assertIn("Using the legacy rollback point", result.stdout)
        self.assertIn("no valid ROLLBACK_COMMIT", result.stderr)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(BASH, "bash is required")
class StaleInstallDirTests(unittest.TestCase):
    """(2026-09-30) The Ryzen's hand-made app-pre-event-mode-pilot-20260919T223956Z
    backup made every upgrade print "cannot delete non-empty directory" (it holds a
    recordings/ folder, which the mirror protects). Such folders are now moved out,
    contents intact, instead of lingering or being deleted."""
    setUp = RollbackPointTests.setUp
    env = RollbackPointTests.env
    run_bash = RollbackPointTests.run_bash

    def _relocate(self, payload_names=()):
        payload = self.tmp / "payload"
        payload.mkdir(exist_ok=True)
        for name in payload_names:
            (payload / name).mkdir()
        return self.run_bash(f'VMS_PAYLOAD_DIR="{bash_path(payload)}"\nROLLBACK_DIR="{bash_path(self.rollback_dir)}"\nrelocate_stale_install_dirs\n')

    def test_a_stale_backup_is_moved_out_with_its_contents(self):
        stale = self.install_root / "app-pre-event-mode-pilot-20260919T223956Z"
        (stale / "recordings").mkdir(parents=True)
        (stale / "note.txt").write_text("kept")
        result = self._relocate()
        self.assertFalse(stale.exists())
        moved = list((self.rollback_dir / "stale-install-dirs").glob("app-pre-event-mode-pilot-20260919T223956Z-moved-*"))
        self.assertEqual(len(moved), 1)
        self.assertEqual((moved[0] / "note.txt").read_text(), "kept")
        self.assertTrue((moved[0] / "recordings").is_dir())
        self.assertIn("Moved stale backup directory", result.stdout)

    def test_release_folders_and_unrelated_folders_stay(self):
        (self.install_root / "tools-pre-built").mkdir()        # part of the release payload
        (self.install_root / "custom").mkdir()                  # not backup-named
        self._relocate(payload_names=("tools-pre-built",))
        self.assertTrue((self.install_root / "tools-pre-built").is_dir())
        self.assertTrue((self.install_root / "custom").is_dir())
        self.assertTrue((self.install_root / "recordings" / "legacy.mkv").exists())
        self.assertFalse((self.rollback_dir / "stale-install-dirs").exists())

    def test_nothing_to_move_is_a_no_op(self):
        self._relocate()
        self.assertFalse((self.rollback_dir / "stale-install-dirs").exists())
        self.assertEqual((self.install_root / "app" / "main.py").read_text(), "OLD RELEASE CODE\n")
