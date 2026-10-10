"""N1/N1b: network preparation must finish before VMS downtime.

These tests source the production deploy_vms() function and use only isolated
temporary paths plus command stubs. The timeout test uses the host's real GNU
timeout and a deliberately stalled Docker client.
"""
import os
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = os.environ.get("TEST_BASH") or shutil.which("bash")
RSYNC = shutil.which("rsync")
TIMEOUT = shutil.which("timeout")
OLD = "a" * 40
NEW = "b" * 40


def bash_path(path: Path) -> str:
    value = path.as_posix()
    if os.name == "nt" and len(value) > 1 and value[1] == ":":
        return "/" + value[0].lower() + value[2:]
    return value


def bash_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


DOCKER_STUB = textwrap.dedent(r'''\
    #!/usr/bin/env bash
    printf 'docker %s\n' "$*" >> "$N1_CALL_LOG"
    case "$1" in
      build)
        case "${N1_BUILD_BEHAVIOR:-success}" in
          fail) echo 'simulated model download failure' >&2; exit 56 ;;
          timeout) sleep 15; exit 0 ;;
          *) exit 0 ;;
        esac ;;
      image) [[ "$2" == inspect ]] && exit 0 ;;
      inspect) echo true; exit 0 ;;
      exec) printf 'online-backup' > "$N1_RECORDINGS/${@: -1}"; exit 0 ;;
      tag) exit 0 ;;
    esac
    exit 0
    ''')

SYSTEMCTL_STUB = textwrap.dedent(r'''\
    #!/usr/bin/env bash
    printf 'systemctl %s\n' "$*" >> "$N1_CALL_LOG"
    case "$1" in
      is-active) exit 0 ;;
      stop|start) exit 0 ;;
    esac
    exit 0
    ''')


class N1InstallerResilienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="anyaicam-n1-tests-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"
        rsync_stub = textwrap.dedent(r'''\
            #!/usr/bin/env bash
            printf 'rsync %s\n' "$*" >> "$N1_CALL_LOG"
            exec "$N1_REAL_RSYNC" "$@"
            ''')
        for name, body in (("docker", DOCKER_STUB), ("systemctl", SYSTEMCTL_STUB), ("rsync", rsync_stub)):
            path = self.bin / name
            path.write_text(body, encoding="utf-8", newline="\n")
            path.chmod(0o755)

        self.install_root = self.tmp / "opt" / "anyaicam"
        (self.install_root / "app").mkdir(parents=True)
        (self.install_root / "app" / "main.py").write_text("old VMS code\n")
        (self.install_root / "recordings").mkdir()
        (self.install_root / "recordings" / "clip.mkv").write_text("camera recording\n")
        (self.install_root / ".env").write_text("CAMERA_SECRET=keep-me\n")
        (self.install_root / "mediamtx").mkdir()
        (self.install_root / "mediamtx" / "mediamtx").write_text("media binary\n")

        self.payload = self.tmp / "payload" / "vms"
        (self.payload / "app").mkdir(parents=True)
        (self.payload / "app" / "main.py").write_text("new VMS code from release payload\n")
        (self.payload / "Dockerfile").write_text("FROM scratch\n")
        (self.payload / "requirements.txt").write_text("fastapi\n")
        (self.payload / "docker-compose.yml").write_text("services: {}\n")

        self.recordings = self.tmp / "var" / "lib" / "anyaicam" / "vms" / "recordings"
        self.recordings.mkdir(parents=True)
        (self.recordings / "partner_portal.db").write_text("live database\n")
        self.data_config = self.tmp / "var" / "lib" / "anyaicam" / "vms" / "data-config"
        self.data_config.mkdir(parents=True)
        (self.data_config / "retained.json").write_text("retained data\n")
        self.env_file = self.tmp / "etc" / "anyaicam" / "vms.env"
        self.env_file.parent.mkdir(parents=True)
        self.env_file.write_text(f"ANYAICAM_VMS_COMMIT={OLD}\nANYAICAM_BUILD_ID={OLD}\nCUSTOM=preserved\n")
        self.release_marker = self.tmp / "etc" / "anyaicam" / "vms_release.json"
        self.release_marker.write_text('{"vms_release_commit":"' + OLD + '"}\n')
        self.installed_marker = self.tmp / "etc" / "anyaicam" / "installed_version"
        self.installed_marker.write_text("1.2.4\n")
        self.accepted_capacity = self.tmp / "var" / "lib" / "anyaicam" / "accepted_capacity.json"
        self.accepted_capacity.write_text('{"accepted":true}\n')
        self.rollback_dir = self.tmp / "var" / "lib" / "anyaicam-update" / "rollback"
        self.rollback_dir.mkdir(parents=True)
        (self.rollback_dir / "latest.env").write_text(f"ROLLBACK_COMMIT={OLD}\n")
        self.latest_before = (self.rollback_dir / "latest.env").read_bytes()
        self.staging = self.tmp / "var" / "lib" / "anyaicam-update" / "installer-staging"

    def env(self, **overrides):
        result = dict(os.environ)
        result.update({
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "N1_CALL_LOG": str(self.log),
            "N1_RECORDINGS": str(self.recordings),
            "N1_REAL_RSYNC": RSYNC,
            "N1_BUILD_BEHAVIOR": "success",
            "ANYAICAM_INSTALLER_STAGING_ROOT": bash_path(self.staging),
            "ANYAICAM_ROLLBACK_DIR": bash_path(self.rollback_dir),
            "VMS_INSTALL_ROOT": bash_path(self.install_root),
            "VMS_PAYLOAD_DIR": bash_path(self.payload),
            "VMS_ENV_FILE": bash_path(self.env_file),
            "VMS_RECORDINGS_DIR": bash_path(self.recordings),
            "VMS_DATA_CONFIG_DIR": bash_path(self.data_config),
            "VMS_RELEASE_MARKER": bash_path(self.release_marker),
            "VMS_RELEASE_COMMIT": NEW,
            "RELEASE_VERSION": "1.2.5",
        })
        result.update(overrides)
        return result

    def bash_prelude(self):
        return textwrap.dedent(f'''\
            set -euo pipefail
            log() {{ printf 'LOG %s\\n' "$*"; }}
            source {bash_quote(bash_path(ROOT / "06-deploy-vms.sh"))}
            VMS_INSTALL_ROOT={bash_quote(bash_path(self.install_root))}
            VMS_PAYLOAD_DIR={bash_quote(bash_path(self.payload))}
            VMS_ENV_FILE={bash_quote(bash_path(self.env_file))}
            VMS_RECORDINGS_DIR={bash_quote(bash_path(self.recordings))}
            VMS_DATA_CONFIG_DIR={bash_quote(bash_path(self.data_config))}
            VMS_RELEASE_MARKER={bash_quote(bash_path(self.release_marker))}
            VMS_RELEASE_COMMIT={NEW}
            RELEASE_VERSION=1.2.5
            migrate_legacy_persistent_data() {{ :; }}
            migrate_legacy_persistent_file() {{ :; }}
            normalize_vms_install_ownership() {{ :; }}
            ensure_vms_env() {{
                printf 'ANYAICAM_VMS_COMMIT=%s\\nANYAICAM_BUILD_ID=%s\\nCUSTOM=preserved\\n' \\
                    "$VMS_RELEASE_COMMIT" "$VMS_RELEASE_COMMIT" > "$VMS_ENV_FILE"
            }}
            install() {{
                local mode=755 path
                while [[ $# -gt 0 ]]; do
                    case "$1" in
                      -d) shift ;;
                      -m) mode="$2"; shift 2 ;;
                      -o|-g) shift 2 ;;
                      *) path="$1"; shift ;;
                    esac
                done
                mkdir -p "$path"
                chmod "$mode" "$path"
            }}
            ''')

    def run_bash(self, script, **overrides):
        return subprocess.run([BASH, "-c", self.bash_prelude() + script], cwd=ROOT,
                              text=True, capture_output=True, env=self.env(**overrides), timeout=20)

    def assert_unchanged_after_rejected_build(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("the running VMS", result.stderr)
        self.assertNotIn("systemctl stop anyaicam-vms.service", self.log.read_text() if self.log.exists() else "")
        self.assertEqual((self.install_root / "app" / "main.py").read_text(), "old VMS code\n")
        self.assertEqual((self.install_root / "recordings" / "clip.mkv").read_text(), "camera recording\n")
        self.assertEqual((self.install_root / ".env").read_text(), "CAMERA_SECRET=keep-me\n")
        self.assertEqual((self.install_root / "mediamtx" / "mediamtx").read_text(), "media binary\n")
        self.assertEqual(self.env_file.read_text(), f"ANYAICAM_VMS_COMMIT={OLD}\nANYAICAM_BUILD_ID={OLD}\nCUSTOM=preserved\n")
        self.assertEqual(self.release_marker.read_text(), '{"vms_release_commit":"' + OLD + '"}\n')
        self.assertEqual(self.installed_marker.read_text(), "1.2.4\n")
        self.assertEqual(self.accepted_capacity.read_text(), '{"accepted":true}\n')
        self.assertEqual((self.rollback_dir / "latest.env").read_bytes(), self.latest_before)
        self.assertEqual(list(self.rollback_dir.glob("rollback-*.env")), [])
        self.assertFalse(list(self.staging.glob("vms.*")) if self.staging.exists() else False)

    @unittest.skipUnless(BASH and RSYNC and TIMEOUT, "bash, rsync and GNU timeout are required")
    def test_failed_model_download_leaves_live_state_and_rollback_untouched(self):
        result = self.run_bash("deploy_vms repair", N1_BUILD_BEHAVIOR="fail")
        self.assert_unchanged_after_rejected_build(result)
        calls = self.log.read_text()
        self.assertIn("docker build -t anyaicam-vms:release-", calls)
        self.assertNotIn("docker tag anyaicam-vms:release-", calls)

    @unittest.skipUnless(os.name != "nt", "GNU timeout behavior is verified on Linux")
    @unittest.skipUnless(BASH and RSYNC and TIMEOUT, "bash, rsync and GNU timeout are required")
    def test_stalled_build_is_bounded_and_does_not_start_downtime(self):
        started = time.monotonic()
        result = self.run_bash("deploy_vms repair", N1_BUILD_BEHAVIOR="timeout", ANYAICAM_VMS_BUILD_TIMEOUT="1")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 8, result.stdout + result.stderr)
        self.assert_unchanged_after_rejected_build(result)
        self.assertIn("timed out", result.stderr)

    @unittest.skipUnless(BASH and RSYNC and TIMEOUT, "bash, rsync and GNU timeout are required")
    def test_two_failed_attempts_then_success_preserves_the_real_rollback_and_customer_data(self):
        result = self.run_bash('''
if deploy_vms repair; then exit 41; fi
if deploy_vms repair; then exit 42; fi
[[ "$(cat "$ROLLBACK_DIR/latest.env")" == "ROLLBACK_COMMIT=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ]]
N1_BUILD_BEHAVIOR=success deploy_vms repair
''', N1_BUILD_BEHAVIOR="fail")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        latest = dict(line.split("=", 1) for line in (self.rollback_dir / "latest.env").read_text().splitlines())
        self.assertEqual(latest["ROLLBACK_COMMIT"], OLD)
        self.assertEqual(latest["UPGRADE_TO_COMMIT"], NEW)
        self.assertNotEqual(latest["ROLLBACK_COMMIT"], latest["UPGRADE_TO_COMMIT"])
        self.assertTrue(Path(latest["ROLLBACK_CODE_ARCHIVE"]).is_file())
        self.assertEqual((self.install_root / "app" / "main.py").read_text(), "new VMS code from release payload\n", result.stdout + result.stderr)
        self.assertEqual((self.install_root / "recordings" / "clip.mkv").read_text(), "camera recording\n")
        self.assertEqual((self.install_root / ".env").read_text(), "CAMERA_SECRET=keep-me\n")
        self.assertEqual((self.install_root / "mediamtx" / "mediamtx").read_text(), "media binary\n")
        self.assertEqual((self.recordings / "partner_portal.db").read_text(), "live database\n")
        self.assertEqual((self.data_config / "retained.json").read_text(), "retained data\n")
        self.assertEqual(self.accepted_capacity.read_text(), '{"accepted":true}\n')
        self.assertIn(f"ANYAICAM_VMS_COMMIT={NEW}", self.env_file.read_text())

        calls = self.log.read_text().splitlines()
        build_at = next(i for i, line in enumerate(calls) if "docker build" in line)
        stop_at = calls.index("systemctl stop anyaicam-vms.service")
        deploy_sync_at = max(i for i, line in enumerate(calls) if line.startswith("rsync "))
        activate_at = next(i for i, line in enumerate(calls) if "docker tag anyaicam-vms:release-" in line)
        self.assertLess(build_at, stop_at)
        self.assertGreater(activate_at, stop_at)
        self.assertGreater(activate_at, deploy_sync_at)
        self.assertNotIn("docker compose build", "\n".join(calls))
        self.assertFalse(list(self.staging.glob("vms.*")))

    @unittest.skipUnless(BASH and RSYNC and TIMEOUT, "bash, rsync and GNU timeout are required")
    def test_clean_install_build_failure_does_not_create_a_rollback_or_install_files(self):
        shutil.rmtree(self.install_root)
        result = self.run_bash("deploy_vms clean", N1_BUILD_BEHAVIOR="fail")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.install_root.exists())
        self.assertFalse(list(self.rollback_dir.glob("rollback-*.env")))
        self.assertEqual((self.rollback_dir / "latest.env").read_bytes(), self.latest_before)
        self.assertNotIn("systemctl stop anyaicam-vms.service", self.log.read_text() if self.log.exists() else "")

class N1bSourceContractTests(unittest.TestCase):
    def test_full_installer_stages_agent_prerequisites_before_vms_mutation_and_uses_offline_install(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        agent_installer = (ROOT / "07-install-agent.sh").read_text(encoding="utf-8")
        child = (ROOT.parent / "appliance-agent" / "scripts" / "install.sh").read_text(encoding="utf-8")
        prepare_agent = installer.index("prepare_agent_install_prerequisites")
        prepare_image = installer.index("prepare_vms_image", prepare_agent)
        provision_dirs = installer.index('provision_users_dirs "$INSTALL_STATE"', prepare_image)
        deploy = installer.index('deploy_vms "$INSTALL_STATE"', provision_dirs)
        self.assertLess(prepare_agent, prepare_image)
        self.assertLess(prepare_image, provision_dirs)
        self.assertLess(provision_dirs, deploy)
        self.assertIn('apt-get update -y || return 1', agent_installer)
        self.assertIn('pip wheel --disable-pip-version-check', agent_installer)
        self.assertIn('--no-index --find-links', child)
        self.assertIn('ANYAICAM_AGENT_WHEELHOUSE="$AGENT_WHEELHOUSE_DIR"', agent_installer)
        self.assertNotIn("docker compose build", (ROOT / "06-deploy-vms.sh").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
