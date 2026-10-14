"""Regression coverage for Docker daemon recovery and Vosk model fetching."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER_DIR = Path(__file__).resolve().parents[1]
DOCKER_SETUP = INSTALLER_DIR / "04-docker-setup.sh"
BASH = shutil.which("bash") or "bash"


class DockerSetupSourceTests(unittest.TestCase):
    def test_existing_docker_installation_does_not_skip_daemon_readiness(self) -> None:
        source = DOCKER_SETUP.read_text(encoding="utf-8")
        existing_path = source.split("if command -v docker", 1)[1].split("fi", 1)[0]
        self.assertNotIn("return 0", existing_path)
        self.assertIn("ensure_docker_daemon_ready", source)

    def test_clean_install_checks_daemon_after_engine_packages_are_installed(self) -> None:
        source = DOCKER_SETUP.read_text(encoding="utf-8")
        package_install = source.index("apt-get install -y docker-ce")
        readiness = source.index("ensure_docker_daemon_ready\n", package_install)
        self.assertLess(package_install, readiness)
        self.assertIn("systemctl enable --now docker.service", source)
        self.assertIn("systemctl enable --now docker.socket", source)
        self.assertIn("docker info", source)
        self.assertIn("did not become ready after 10 attempts", source)

    def test_both_vosk_downloads_retry_and_keep_the_pinned_checksum(self) -> None:
        expected_checksum = "30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498"
        for filename in ("Dockerfile", "Dockerfile.production"):
            with self.subTest(filename=filename):
                text = (REPO_ROOT / filename).read_text(encoding="utf-8")
                download = text.split('"https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"', 1)[0]
                download = download[download.rfind("curl "):]
                self.assertIn("--retry 5", download)
                self.assertIn("--retry-all-errors", download)
                self.assertIn("--retry-max-time 3600", download)
                self.assertIn("--connect-timeout 30", download)
                self.assertIn("--max-time 1800", download)
                self.assertIn(expected_checksum, text)
                self.assertLess(text.index("vosk-model.zip\""), text.index("sha256sum -c -", text.index(expected_checksum)))
                self.assertLess(text.index("sha256sum -c -", text.index(expected_checksum)), text.index("extractall('/opt/anyaicam-stt-model')"))


@unittest.skipUnless(os.name != "nt", "systemd and POSIX shell integration tests require Linux")
class DockerDaemonRecoveryTests(unittest.TestCase):
    def run_recovery(self, docker_initially_ready: bool, *, daemon_recovers: bool = True) -> subprocess.CompletedProcess[str]:
        script = r'''
set -euo pipefail
source "$DOCKER_SETUP"
log() { printf 'LOG:%s\n' "$*"; }
systemctl() {
    printf '%s\n' "$*" >> "$SYSTEMCTL_LOG"
    case "$*" in
        "cat docker.socket") return 0 ;;
        "enable --now docker.socket") touch "$SOCKET_STARTED" ;;
    esac
    return 0
}
docker() {
    if [[ "$1" == info ]]; then
        [[ "$DOCKER_INITIAL_READY" == true ]] && return 0
        [[ -f "$SOCKET_STARTED" && "$DAEMON_RECOVERS" == true ]] && return 0
        return 1
    fi
    return 0
}
sleep() { :; }
ensure_docker_daemon_ready
'''
        with tempfile.TemporaryDirectory(prefix="docker-daemon-test-") as temporary:
            env = dict(os.environ)
            env.update(
                DOCKER_SETUP=str(DOCKER_SETUP),
                DOCKER_INITIAL_READY="true" if docker_initially_ready else "false",
                DAEMON_RECOVERS="true" if daemon_recovers else "false",
                SYSTEMCTL_LOG=str(Path(temporary) / "systemctl.log"),
                SOCKET_STARTED=str(Path(temporary) / "socket-started"),
            )
            result = subprocess.run([BASH, "-c", script], env=env, text=True, capture_output=True, check=False)
            if Path(env["SYSTEMCTL_LOG"]).exists():
                result.systemctl_log = Path(env["SYSTEMCTL_LOG"]).read_text(encoding="utf-8").splitlines()
            else:
                result.systemctl_log = []
            return result

    def test_existing_working_daemon_is_checked_and_enabled_without_restart(self) -> None:
        result = self.run_recovery(True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.systemctl_log, ["enable docker.service"])

    def test_unavailable_daemon_starts_socket_before_service_and_recovers(self) -> None:
        result = self.run_recovery(False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLess(result.systemctl_log.index("enable --now docker.socket"), result.systemctl_log.index("enable --now docker.service"))

    def test_unrecoverable_daemon_fails_with_actionable_message(self) -> None:
        result = self.run_recovery(False, daemon_recovers=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not become ready after 10 attempts", result.stderr)
        self.assertIn("systemctl status docker docker.socket", result.stderr)


if __name__ == "__main__":
    unittest.main()
