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
            ''')
        result = subprocess.run([BASH, "-c", prelude + script], cwd=ROOT, text=True, capture_output=True, env=self.env(**extra))
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

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
        paths = dict(VMS_INSTALL_ROOT=bash_path(self.install_root), VMS_ENV_FILE=bash_path(self.env_file),
                     VMS_RECORDINGS_DIR=bash_path(self.recordings), ANYAICAM_ROLLBACK_ALLOW_NON_ROOT="1")
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
        kept = [p for p in self.recordings.iterdir() if p.name.startswith("partner_portal-before-rollback-")]
        self.assertEqual([p.read_text() for p in kept], ["database after upgrade"])  # replaced, never deleted

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


if __name__ == "__main__":
    unittest.main()
