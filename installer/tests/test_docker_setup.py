"""Regression coverage for Docker daemon recovery and Vosk model fetching.

The Docker tests source the real installer/04-docker-setup.sh and replace
only the external commands (apt-get, systemctl, docker, sleep) with shell
functions, so they exercise the shipped control flow rather than its text.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER_DIR = Path(__file__).resolve().parents[1]
DOCKER_SETUP = INSTALLER_DIR / "04-docker-setup.sh"
BASH = shutil.which("bash") or "bash"

VOSK_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
VOSK_SHA256 = "30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498"

# Shell stubs shared by every scenario. Each call is appended to $CALL_LOG.
# DOCKER_STATE drives `docker info`:
#   ready      - the daemon answers immediately
#   recovers   - the daemon answers once docker.service has been started
#   down       - the daemon never answers
# SERVICE_UNIT=absent simulates a daemon not managed by docker.service
# (e.g. the Docker snap); SOCKET_UNIT=absent a non-socket-activated install.
STUBS = r'''
set -euo pipefail
source "$DOCKER_SETUP"
log() { printf 'LOG:%s\n' "$*"; }
sleep() { :; }
apt-get() { printf 'apt-get %s\n' "$*" >> "$CALL_LOG"; }
systemctl() {
    printf 'systemctl %s\n' "$*" >> "$CALL_LOG"
    case "$*" in
        "cat docker.socket") [[ "$SOCKET_UNIT" == present ]] ;;
        "cat docker.service") [[ "$SERVICE_UNIT" == present ]] ;;
        "enable docker.service")
            [[ "$SERVICE_UNIT" == present ]] || { echo "Unit file docker.service does not exist." >&2; return 1; } ;;
        "enable --now docker.service") touch "$SERVICE_STARTED" ;;
        *) return 0 ;;
    esac
}
docker() {
    printf 'docker %s\n' "$*" >> "$CALL_LOG"
    if [[ "$1" == info ]]; then
        case "$DOCKER_STATE" in
            ready) return 0 ;;
            recovers) [[ -f "$SERVICE_STARTED" ]] ;;
            *) return 1 ;;
        esac
        return
    fi
    return 0
}
'''


@unittest.skipUnless(os.name != "nt", "sources the installer under a POSIX bash")
class DockerDaemonRecoveryTests(unittest.TestCase):
    def run_scenario(
        self,
        entrypoint: str,
        *,
        docker_state: str,
        service_unit: str = "present",
        socket_unit: str = "present",
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory(prefix="docker-daemon-test-") as temporary:
            call_log = Path(temporary) / "calls.log"
            env = dict(os.environ)
            env.update(
                DOCKER_SETUP=str(DOCKER_SETUP),
                CALL_LOG=str(call_log),
                SERVICE_STARTED=str(Path(temporary) / "service-started"),
                DOCKER_STATE=docker_state,
                SERVICE_UNIT=service_unit,
                SOCKET_UNIT=socket_unit,
            )
            result = subprocess.run(
                [BASH, "-c", STUBS + entrypoint + "\n"],
                env=env, text=True, capture_output=True, check=False,
            )
            calls = call_log.read_text(encoding="utf-8").splitlines() if call_log.exists() else []
            return result, calls

    def systemctl_calls(self, calls: list[str]) -> list[str]:
        return [call.removeprefix("systemctl ") for call in calls if call.startswith("systemctl ")]

    def test_working_daemon_is_enabled_without_restart(self) -> None:
        result, calls = self.run_scenario("ensure_docker_daemon_ready", docker_state="ready")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.systemctl_calls(calls), ["cat docker.service", "enable docker.service"])

    def test_working_daemon_without_docker_service_unit_is_accepted(self) -> None:
        # Docker snap: `docker info` works, but there is no docker.service.
        result, calls = self.run_scenario(
            "ensure_docker_daemon_ready", docker_state="ready", service_unit="absent"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("enable docker.service", self.systemctl_calls(calls))
        self.assertIn("not managed by docker.service", result.stdout)

    def test_unavailable_daemon_starts_socket_before_service_and_recovers(self) -> None:
        result, calls = self.run_scenario("ensure_docker_daemon_ready", docker_state="recovers")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        systemctl = self.systemctl_calls(calls)
        self.assertLess(systemctl.index("daemon-reload"), systemctl.index("enable --now docker.socket"))
        self.assertLess(systemctl.index("unmask docker.socket"), systemctl.index("enable --now docker.socket"))
        self.assertLess(systemctl.index("enable --now docker.socket"), systemctl.index("enable --now docker.service"))
        self.assertEqual(calls[-1], "docker info")

    def test_service_without_socket_unit_is_started_directly(self) -> None:
        result, calls = self.run_scenario(
            "ensure_docker_daemon_ready", docker_state="recovers", socket_unit="absent"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        systemctl = self.systemctl_calls(calls)
        self.assertNotIn("enable --now docker.socket", systemctl)
        self.assertIn("enable --now docker.service", systemctl)

    def test_unrecoverable_daemon_fails_with_actionable_message(self) -> None:
        result, calls = self.run_scenario("ensure_docker_daemon_ready", docker_state="down")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not become ready after 30 attempts", result.stderr)
        self.assertIn("systemctl status docker docker.socket", result.stderr)
        self.assertIn("journalctl -u docker", result.stderr)
        # 1 initial probe + 30 readiness attempts.
        self.assertEqual(calls.count("docker info"), 31)

    def test_failure_aborts_docker_setup_under_errexit(self) -> None:
        # install.sh calls docker_setup as a plain statement; a daemon that
        # never answers must stop the installer, not fall through.
        result, _ = self.run_scenario(
            "docker_setup; echo REACHED_AFTER_DOCKER_SETUP", docker_state="down"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("REACHED_AFTER_DOCKER_SETUP", result.stdout)

    def test_existing_docker_installation_still_checks_daemon_readiness(self) -> None:
        # docker + compose already present: no Docker packages are installed,
        # but the stopped daemon is still recovered and verified.
        result, calls = self.run_scenario("docker_setup", docker_state="recovers")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse([call for call in calls if call.startswith("apt-get install") and "docker-ce" in call])
        self.assertIn("enable --now docker.service", self.systemctl_calls(calls))
        self.assertIn("Docker daemon is ready", result.stdout)

    def test_clean_install_checks_readiness_after_the_package_branch(self) -> None:
        # The clean-install branch writes real apt files, so check its shape
        # through bash's normalized function listing instead of executing it:
        # the readiness check must follow the closing `fi` of the install
        # branch, i.e. run for both clean installs and existing Docker.
        listing = subprocess.run(
            [BASH, "-c", 'source "$1"; declare -f docker_setup', "_", str(DOCKER_SETUP)],
            text=True, capture_output=True, check=True,
        ).stdout.splitlines()
        body = [line.strip() for line in listing]
        install = next(i for i, line in enumerate(body) if line.startswith("apt-get install") and "docker-ce" in line)
        closing_fi = next(i for i in range(install, len(body)) if body[i] == "fi;")
        self.assertEqual(body[closing_fi + 1], "ensure_docker_daemon_ready;")
        self.assertFalse([line for line in body if line.startswith("return 0")])


class VoskDownloadTests(unittest.TestCase):
    def vosk_run_instruction(self, filename: str) -> str:
        text = (REPO_ROOT / filename).read_text(encoding="utf-8").replace("\r\n", "\n")
        # One Dockerfile instruction: RUN plus its backslash continuations.
        for match in re.finditer(r"^RUN (?:.*\\\n)*.*$", text, flags=re.MULTILINE):
            if VOSK_URL in match.group(0):
                return match.group(0)
        self.fail(f"{filename}: no RUN instruction downloads {VOSK_URL}")

    def test_both_dockerfiles_bound_retry_and_abort_stalled_transfers(self) -> None:
        for filename in ("Dockerfile", "Dockerfile.production"):
            with self.subTest(filename=filename):
                run = self.vosk_run_instruction(filename)
                curl = run[run.index("curl "):run.index(VOSK_URL)]
                flags = dict(re.findall(r"(--[a-z-]+)(?: (\d+))?", curl))
                self.assertIn("--retry-all-errors", flags)
                self.assertGreaterEqual(int(flags["--retry"]), 3)
                self.assertLessEqual(int(flags["--connect-timeout"]), 60)
                # A stall is detected in minutes, not after --max-time.
                self.assertGreater(int(flags["--speed-limit"]), 0)
                self.assertLessEqual(int(flags["--speed-time"]), 120)
                # Retrying must fit in the installer's 5400 s build timeout.
                self.assertLessEqual(int(flags["--retry-max-time"]) + int(flags["--max-time"]), 3600)
                self.assertIn(" -f", curl)

    def test_both_dockerfiles_verify_the_pinned_checksum_before_extracting(self) -> None:
        for filename in ("Dockerfile", "Dockerfile.production"):
            with self.subTest(filename=filename):
                run = self.vosk_run_instruction(filename)
                download = run.index(VOSK_URL)
                verify = run.index(f'echo "{VOSK_SHA256}  /tmp/vosk-model.zip" | sha256sum -c -')
                extract = run.index("extractall('/opt/anyaicam-stt-model')")
                self.assertLess(download, verify)
                self.assertLess(verify, extract)
                # Steps are chained with && so a checksum mismatch fails the build.
                between = run[verify:run.index("python -c", verify)]
                self.assertNotIn("||", between)
                self.assertNotIn(";", between)


if __name__ == "__main__":
    unittest.main()
