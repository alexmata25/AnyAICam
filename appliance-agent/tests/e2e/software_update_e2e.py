#!/usr/bin/env python3
"""Disposable Software Update E2E (2026-10-03). Run ONLY inside a throwaway
Linux container as root (run_software_update_e2e.sh does that: docker run
--rm --network none). It uses the real paths (/opt/anyaicam,
/etc/anyaicam-update, /var/lib/anyaicam-update) and the real components:

  * installer/12-update-signing-key.sh provisions the trust anchor
  * scripts/lib-privileged-watcher.sh installs the watcher + root applier
  * the agent's OwnerUpdate stages a release AS THE UNPRIVILEGED anyaicam USER
  * the real privileged_watcher.py dispatches 'apply_release'
  * the real apply_release.py runs under /usr/bin/python3 -I, verifies the
    signature with the system openssl, swaps /opt/anyaicam, validates, and
    rolls back

Stand-ins: `docker` (no image work) and `systemctl` (start = run the VMS
from whatever code is in /opt/anyaicam/app NOW, like a fresh container
whose /app is the bind-mounted tree; the process chdirs into it, so -- like
a bind mount -- a running VMS keeps serving its own tree until restarted),
and `systemd-run` (runs the command in the foreground).

Proves: old version/build running -> update -> new version/build running ->
bad release -> automatic rollback -> old version/build running again ->
manual `sudo anyaicam-rollback` -> the release before the update running, with
its identity restored -> the newer release installs again; plus
the unprivileged user cannot touch root's code, key or state, a planted
symlink cannot redirect a root write, and a forged release changes nothing.
"""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SRC = Path(os.environ.get("E2E_SRC", "/src"))
WORK = Path("/work")
RESULTS = []


def step(text):
    print(f"\n=== {text}", flush=True)


def check(condition, text):
    RESULTS.append((bool(condition), text))
    print(f"  [{'PASS' if condition else 'FAIL'}] {text}", flush=True)
    if not condition:
        raise SystemExit(f"E2E FAILED: {text}")


def sh(*argv, user=None, env=None, check_rc=True):
    if user:
        argv = ("runuser", "-u", user, "--") + argv
    done = subprocess.run(argv, capture_output=True, text=True, env=env)
    if check_rc and done.returncode != 0:
        raise SystemExit(f"command failed: {argv}\n{done.stdout}\n{done.stderr}")
    return done


def get(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8000{path}", timeout=3) as response:
            return response.status, response.read().decode()
    except Exception as error:  # noqa: BLE001
        return 0, str(error)


def serving():
    status, version = get("/version")
    _, tree = get("/static/release-identity.json")
    version = json.loads(version) if status == 200 else {}
    try:
        tree = json.loads(tree)
    except ValueError:
        tree = {}
    return version.get("version"), version.get("build_id"), tree.get("build_id")


SERVE = r'''
import json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
os.chdir(Path(__file__).resolve().parent)   # like a bind mount: keep THIS tree while running
ENV = dict(l.split("=", 1) for l in Path("/etc/anyaicam/vms.env").read_text().splitlines() if "=" in l)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/version":
            self.send(200, {"product": "AnyAiCam VMS", "version": ENV.get("ANYAICAM_VERSION", "0.9.0"),
                            "build_id": ENV.get("ANYAICAM_BUILD_ID", "local")})
        elif self.path == "/health":
            self.send(500 if Path("BROKEN").exists() else 200, {"status": "ok"})
        elif self.path == "/ready":
            self.send(200, {"self_test": {"ok": True}})
        elif self.path.startswith("/static/") and Path("static", self.path[8:]).is_file():
            self.raw(200, Path("static", self.path[8:]).read_bytes())
        else:
            self.raw(404, b"")
    def send(self, code, obj):
        self.raw(code, json.dumps(obj, separators=(",", ":")).encode())
    def raw(self, code, body):
        self.send_response(code); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *args):
        pass
ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
'''

SYSTEMCTL = r'''#!/usr/bin/env bash
echo "systemctl $*" >> /var/log/e2e-commands.log
if [[ "$2" == "anyaicam-vms.service" ]]; then
  case "$1" in
    start)
      nohup /usr/bin/python3 /opt/anyaicam/app/serve.py >>/var/log/e2e-vms.log 2>&1 &
      echo $! > /run/e2e-vms.pid
      for i in $(seq 50); do /usr/bin/python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/ready',timeout=1)" 2>/dev/null && exit 0; sleep 0.1; done
      exit 0 ;;
    stop)
      [[ -f /run/e2e-vms.pid ]] && kill "$(cat /run/e2e-vms.pid)" 2>/dev/null
      for i in $(seq 50); do /usr/bin/python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/ready',timeout=1)" 2>/dev/null || exit 0; sleep 0.1; done
      exit 0 ;;
  esac
fi
exit 0
'''
DOCKER = '#!/usr/bin/env bash\necho "docker $*" >> /var/log/e2e-commands.log\nexit 0\n'
SYSTEMD_RUN = r'''#!/usr/bin/env bash
echo "systemd-run $*" >> /var/log/e2e-commands.log
while [[ "$1" == --* ]]; do shift; done
exec "$@"
'''


def install_tool(name, body):
    path = Path("/usr/local/sbin") / name
    path.write_text(body)
    path.chmod(0o755)


def lf_copy(source: Path, target: Path):
    """The worktree is checked out with CRLF on Windows; bash needs LF."""
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    for path in target.rglob("*"):
        if path.is_file() and path.suffix in (".sh", ".py", ".service", ".path"):
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))


def main():
    if os.geteuid() != 0 or not Path("/.dockerenv").exists():
        raise SystemExit("Run only as root inside a disposable container (run_software_update_e2e.sh).")

    step("Disposable host: users, directories, stand-in tools")
    WORK.mkdir()
    lf_copy(SRC / "appliance-agent", WORK / "appliance-agent")
    lf_copy(SRC / "installer", WORK / "installer")
    agent = WORK / "appliance-agent"
    sys.path[:0] = [str(agent), str(agent / "tests")]
    from software_update_helpers import (BUILD_A, BUILD_B, BUILD_C, generate_keypair, manifest_for, release_files,
                                         sign, write_tarball)
    sh("useradd", "--system", "--home", "/var/lib/anyaicam", "--shell", "/usr/sbin/nologin", "anyaicam")
    for directory in ("/etc/anyaicam", "/var/lib/anyaicam", "/var/lib/anyaicam/vms/recordings", "/var/log/anyaicam"):
        Path(directory).mkdir(parents=True, exist_ok=True)
    sh("install", "-d", "-m", "0755", "-o", "root", "-g", "root", "/opt/anyaicam-agent")   # as scripts/install.sh now does
    Path("/etc/systemd/system").mkdir(parents=True, exist_ok=True)
    if not Path("/usr/bin/python3").exists():
        os.symlink("/usr/local/bin/python3", "/usr/bin/python3")
    install_tool("systemctl", SYSTEMCTL)
    install_tool("docker", DOCKER)
    install_tool("systemd-run", SYSTEMD_RUN)
    os_id = next(line[3:].strip('"') for line in Path("/etc/os-release").read_text().splitlines() if line.startswith("ID="))
    arch = os.uname().machine

    step("Offline signing key (private half stays in /root/offline) + real installer key provisioning")
    private_key, public_pem = generate_keypair()
    Path("/root/offline").mkdir(mode=0o700)
    payload = WORK / "payload"
    (payload / "keys").mkdir(parents=True)
    (payload / "keys/update-signing-public-key.pem").write_bytes(public_pem)
    sha = hashlib.sha256(public_pem).hexdigest()
    sh("bash", "-c", f'set -e; log(){{ echo "$*"; }}; PAYLOAD_DIR={payload}; UPDATE_SIGNING_KEY_SHA256={sha}; '
                     f'source {WORK}/installer/12-update-signing-key.sh; provision_update_signing_key')
    key = Path("/etc/anyaicam-update/trusted_signing_key.pem")
    check(key.stat().st_uid == 0 and oct(key.stat().st_mode & 0o777) == "0o644", "trust anchor is root:root 0644")
    check(Path("/var/lib/anyaicam-update").stat().st_uid == 0, "root update state directory is root-owned")

    step("Real watcher + root applier installation (scripts/lib-privileged-watcher.sh)")
    sh("bash", "-c", f"source {agent}/scripts/lib-privileged-watcher.sh; install_privileged_watcher {agent}")
    privileged = Path("/opt/anyaicam-agent/privileged")
    check(privileged.stat().st_uid == 0 and oct(privileged.stat().st_mode & 0o777) == "0o700", "privileged/ is root 0700")
    check((privileged / "apply_release.py").stat().st_uid == 0, "apply_release.py is root-owned")

    step("Release 1.1.0 (build A) installed and running")
    live = Path("/opt/anyaicam")
    for relative, data in release_files("1.1.0", BUILD_A, app_files={"serve.py": SERVE}).items():
        if relative.startswith("payload/vms/"):
            target = live / relative[len("payload/vms/"):]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    (live / "mediamtx").mkdir()
    (live / "mediamtx/mediamtx").write_text("installer-provisioned binary")
    Path("/etc/anyaicam/vms.env").write_text(f"ANYAICAM_ENV=production\nANYAICAM_APP_SECRETS=keep-me\nANYAICAM_VERSION=1.1.0\n"
                                             f"ANYAICAM_BUILD_ID={BUILD_A}\nANYAICAM_VMS_COMMIT={BUILD_A}\n")
    marker = {"vms_release_commit": BUILD_A, "release_version": "1.1.0", "installer_version": "1.1.0"}
    Path("/etc/anyaicam/vms_release.json").write_text(json.dumps(marker))
    Path("/var/lib/anyaicam/vms/recordings/partner_portal.db").write_text("customer database")
    sh("chown", "-R", "anyaicam:anyaicam", "/etc/anyaicam", "/var/lib/anyaicam", "/var/log/anyaicam")
    sh("systemctl", "start", "anyaicam-vms.service")
    check(serving() == ("1.1.0", BUILD_A, BUILD_A), "OLD: /version and the served tree are 1.1.0 / build A")

    step("The unprivileged anyaicam user cannot touch root's code, key or state")
    probes = {
        "replace the root applier": "open('/opt/anyaicam-agent/privileged/apply_release.py','a')",
        "rename privileged/": "import os; os.rename('/opt/anyaicam-agent/privileged','/opt/anyaicam-agent/x')",
        "replace the trust anchor": "open('/etc/anyaicam-update/trusted_signing_key.pem','w')",
        "write a root result": "open('/var/lib/anyaicam-update/results/forged.json','w')",
        "write root's installed record": "open('/var/lib/anyaicam-update/installed_release.json','w')",
        "write the live application": "open('/opt/anyaicam/app/main.py','a')",
    }
    for label, code in probes.items():
        done = sh("python3", "-c", code, user="anyaicam", check_rc=False)
        check(done.returncode != 0 and ("Permission" in done.stderr or "denied" in done.stderr), f"anyaicam cannot {label}")

    def publish(version, build, app_files, signer=None, update_id=None):
        label = update_id or version
        package = write_tarball(WORK / f"release-{label}.tar.gz", release_files(version, build, app_files=app_files))
        manifest = manifest_for(package, version=version, build_id=build, platform=os_id, architecture=arch,
                                update_id=update_id)
        offer = WORK / f"offer-{label}"
        offer.mkdir()
        (offer / "manifest.json").write_text(json.dumps(manifest))
        (offer / "manifest.sig").write_bytes(base64.b64encode(sign(signer or private_key, manifest)))
        shutil.copy(package, offer / "package.tar.gz")
        sh("chmod", "-R", "a+rX", str(WORK))
        return manifest, offer

    def stage_as_agent(manifest, offer):
        """OwnerUpdate.stage() exactly as the agent runs it: as anyaicam."""
        snippet = f'''
import base64, json, sys
from pathlib import Path
sys.path[:0] = ["{agent}"]
from anyaicam_agent.camera_binding import atomic_write_json
from anyaicam_agent.config import AgentConfig
from anyaicam_agent.updater.history import UpdateHistory
from anyaicam_agent.updater.owner_update import OwnerUpdate
from anyaicam_agent.updater.verify import PackageVerifier
config = AgentConfig(state_dir="/var/lib/anyaicam", config_dir="/etc/anyaicam", log_dir="/var/log/anyaicam")
offer = Path("{offer}")
class Source:
    def check_for_manifest(self, *a):
        return json.loads((offer / "manifest.json").read_text()), base64.b64decode((offer / "manifest.sig").read_bytes())
    def download_package(self, manifest, destination):
        Path(destination).write_bytes((offer / "package.tar.gz").read_bytes())
def queue(kind, payload):
    atomic_write_json(config.pending_actions_dir / f"{{kind}}.json", {{"type": kind, "command_id": payload["update_id"]}})
manifest = json.loads((offer / "manifest.json").read_text())
result = OwnerUpdate(config, history=UpdateHistory(config.update_history_file), verifier=PackageVerifier(config.trusted_public_key_file),
                     source=Source(), queue_privileged_action=queue).stage(
    {{"update_id": manifest["update_id"], "version": manifest["version"], "sha256": manifest["sha256"], "confirmed": True,
      "requested_by": "owner@example.test"}})
print(json.dumps(result.as_dict()))
'''
        done = sh("python3", "-c", snippet, user="anyaicam")
        return json.loads(done.stdout.strip().splitlines()[-1])

    def run_watcher():
        sh("python3", str(privileged / "watcher.py"), "--grace-seconds", "0")

    def root_result(update_id):
        path = Path(f"/var/lib/anyaicam-update/results/{update_id}.json")
        return json.loads(path.read_text()), path.stat()

    def relay_as_agent(b_id='', c_id=''):
        """ResultRelay.poll(), as the agent's every-cycle relay runs it: as anyaicam."""
        relay = f'''
import json, sys
sys.path[:0] = ["{agent}"]
from anyaicam_agent.config import AgentConfig
from anyaicam_agent.updater.apply_results import ResultRelay
from anyaicam_agent.updater.history import UpdateHistory
config = AgentConfig(state_dir="/var/lib/anyaicam", config_dir="/etc/anyaicam", log_dir="/var/log/anyaicam")
reported = []
ResultRelay(config, history=UpdateHistory(config.update_history_file), report=reported.append).poll()
history = UpdateHistory(config.update_history_file)
print(json.dumps({{"reported": [r.state.value for r in reported],
                  "b": (history.get("{b_id}") or {{}}).get("state"), "c": (history.get("{c_id}") or {{}}).get("state")}}))
'''
        return json.loads(sh("python3", "-c", relay, user="anyaicam").stdout.strip().splitlines()[-1])

    step("A planted symlink: anyaicam points the release marker at a root-only file")
    victim = Path("/root/victim-secret")
    victim.write_text("root-only content")
    victim.chmod(0o600)
    sh("python3", "-c", "import os; os.remove('/etc/anyaicam/vms_release.json'); os.symlink('/root/victim-secret','/etc/anyaicam/vms_release.json')",
       user="anyaicam")

    step("UPDATE to 1.2.0 (build B): staged by the agent, activated by the root applier")
    manifest_b, offer_b = publish("1.2.0", BUILD_B, {"serve.py": SERVE})
    staged = stage_as_agent(manifest_b, offer_b)
    check(staged["state"] == "activation_requested", f"agent staged and requested activation ({staged['state']} {staged['error']})")
    staged_dir = Path(f"/var/lib/anyaicam/updates/staged/{manifest_b['update_id']}")
    check(staged_dir.stat().st_uid == Path("/var/lib/anyaicam").stat().st_uid, "staged files are owned by anyaicam")
    check(Path("/var/lib/anyaicam/updates/current_version.txt").exists() is False, "the legacy pointer was never written")
    run_watcher()
    result, info = root_result(manifest_b["update_id"])
    check(result["state"] == "healthy", f"root applier result: {result['state']} {result.get('error', '')}")
    check(info.st_uid == 0, "the result file is root-owned")
    check(serving() == ("1.2.0", BUILD_B, BUILD_B), "NEW: /version and the served tree are 1.2.0 / build B")
    check(json.loads(Path("/opt/anyaicam.previous/app/static/release-identity.json").read_text())["build_id"] == BUILD_A,
          "the previous release is kept whole at /opt/anyaicam.previous")
    check((live / "mediamtx/mediamtx").read_text() == "installer-provisioned binary", "mediamtx carried into the new tree")
    check(victim.read_text() == "root-only content", "the planted symlink did not redirect the root write")
    check(not Path("/etc/anyaicam/vms_release.json").is_symlink(), "the marker is a regular file again")
    check(json.loads(Path("/etc/anyaicam/vms_release.json").read_text())["release_version"] == "1.2.0", "marker: 1.2.0")
    check(relay_as_agent(manifest_b['update_id'])['b'] == 'healthy', 'the agent relays B as healthy (its every-cycle relay)')
    check("ANYAICAM_APP_SECRETS=keep-me" in Path("/etc/anyaicam/vms.env").read_text(), "vms.env secrets kept")
    check(Path("/var/lib/anyaicam/vms/recordings/partner_portal.db").read_text() == "customer database", "database untouched")

    step("BAD RELEASE 1.3.0 (build C): fails validation (this waits out the applier's real 300 s window)")
    manifest_c, offer_c = publish("1.3.0", BUILD_C, {"serve.py": SERVE, "BROKEN": "health fails"})
    check(stage_as_agent(manifest_c, offer_c)["state"] == "activation_requested", "agent staged 1.3.0")
    started = time.time()
    run_watcher()
    result, _ = root_result(manifest_c["update_id"])
    check(result["state"] == "rolled_back", f"root applier result: {result['state']} ({result.get('error', '')[:120]})")
    check("health_check_failed" in result["error"], "the reason is the failed health check")
    check(serving() == ("1.2.0", BUILD_B, BUILD_B), "OLD AGAIN: /version and the served tree are 1.2.0 / build B")
    check(json.loads(Path("/etc/anyaicam/vms_release.json").read_text())["release_version"] == "1.2.0",
          "the marker never claimed 1.3.0")
    check(json.loads(Path("/opt/anyaicam.failed/app/static/release-identity.json").read_text())["build_id"] == BUILD_C,
          "the failed tree is set aside for inspection")
    check(f"ANYAICAM_BUILD_ID={BUILD_B}" in Path("/etc/anyaicam/vms.env").read_text(), "vms.env identity restored to B")
    print(f"  (rollback completed {time.time() - started:.0f} s after activation was requested)")

    step("The agent relays the outcomes (as anyaicam) and they survive a restart")
    relayed = relay_as_agent(manifest_b['update_id'], manifest_c['update_id'])
    check(relayed["b"] == "healthy" and relayed["c"] == "rolled_back", f"agent history: B={relayed['b']} C={relayed['c']}")
    sh("systemctl", "stop", "anyaicam-vms.service")
    sh("systemctl", "start", "anyaicam-vms.service")
    check(serving() == ("1.2.0", BUILD_B, BUILD_B), "after a VMS restart it is still 1.2.0 / build B")

    step("A forged release (attacker key) changes nothing")
    attacker, _ = generate_keypair()
    manifest_x, offer_x = publish("9.9.9", BUILD_C, {"serve.py": SERVE}, signer=attacker, update_id="forged-9-9-9")
    agent_view = stage_as_agent(manifest_x, offer_x)
    check(agent_view["state"] == "rejected" and "bad_signature" in agent_view["error"], "the agent refuses it")
    # Plant it directly, bypassing the agent: the root applier must refuse it too.
    forged = Path("/var/lib/anyaicam/updates/staged/forged-9-9-9")
    sh("python3", "-c", f'''
import shutil, json; from pathlib import Path
d = Path("{forged}"); d.mkdir(parents=True)
for n in ("manifest.json", "manifest.sig", "package.tar.gz"): shutil.copy("{offer_x}/" + n, d / n)
(d / "request.json").write_text(json.dumps({{"requested_by": "$(reboot) ../../etc/shadow"}}))
Path("/var/lib/anyaicam/pending_actions/apply_release.json").write_text(json.dumps({{"type": "apply_release", "command_id": "x"}}))
''', user="anyaicam")
    run_watcher()
    result, _ = root_result("forged-9-9-9")
    check(result["state"] == "rejected" and "bad_signature" in result["error"], "the root applier refuses it")
    check(serving() == ("1.2.0", BUILD_B, BUILD_B), "still 1.2.0 / build B")
    commands = Path("/var/log/e2e-commands.log").read_text()
    check("reboot" not in commands and "shadow" not in commands, "nothing from the request reached a command")

    step("MANUAL ROLLBACK: sudo anyaicam-rollback returns to the release the in-app update replaced")
    if not shutil.which("rsync"):
        check(False, "rsync is installed (every appliance has it; this E2E image needs it for rollback.sh)")
    else:
        sh("bash", "-c", f'set -e; log(){{ echo "$*"; }}; INSTALLER_DIR={WORK}/installer; '
                         f'source {WORK}/installer/06-deploy-vms.sh; install_rollback_tool')
        tool = Path("/usr/local/sbin/anyaicam-rollback")
        check(tool.stat().st_uid == 0 and oct(tool.stat().st_mode & 0o777) == "0o755", "the rollback command is installed root:root 0755")
        points = Path("/var/lib/anyaicam-update/rollback")
        check(points.stat().st_uid == 0 and oct(points.stat().st_mode & 0o777) == "0o750", "rollback points live in a root-only directory")
        latest = dict(line.split("=", 1) for line in (points / "latest.env").read_text().splitlines() if "=" in line)
        check((latest["ROLLBACK_COMMIT"], latest["ROLLBACK_VERSION"], latest["UPGRADE_TO_COMMIT"]) == (BUILD_A, "1.1.0", BUILD_B),
              "the newest point is for 1.1.0, written when 1.2.0 was installed (the failed 1.3.0 wrote none)")
        probe = sh("python3", "-c", "open('/var/lib/anyaicam-update/rollback/latest.env','a')", user="anyaicam", check_rc=False)
        check(probe.returncode != 0, "anyaicam cannot touch the rollback points")
        done = sh(str(tool), "--yes", check_rc=False)
        check(done.returncode == 0, f"anyaicam-rollback completed and validated ({done.stdout.strip().splitlines()[-1][:120] if done.stdout.strip() else done.stderr[-160:]})")
        check(serving() == ("1.1.0", BUILD_A, BUILD_A), "ROLLED BACK: /version and the served tree are 1.1.0 / build A")
        marker_now = json.loads(Path("/etc/anyaicam/vms_release.json").read_text())
        root_record = json.loads(Path("/var/lib/anyaicam-update/installed_release.json").read_text())
        check(marker_now["release_version"] == "1.1.0" and root_record["release_version"] == "1.1.0",
              "the installed-release record is 1.1.0 again, for the agent and for root")
        env_now = Path("/etc/anyaicam/vms.env").read_text()
        check(f"ANYAICAM_VERSION=1.1.0" in env_now and f"ANYAICAM_BUILD_ID={BUILD_A}" in env_now and "ANYAICAM_APP_SECRETS=keep-me" in env_now,
              "vms.env identity is 1.1.0 / build A; secrets kept")
        check(Path("/var/lib/anyaicam/vms/recordings/partner_portal.db").read_text() == "customer database", "database untouched")
        check((live / "mediamtx/mediamtx").read_text() == "installer-provisioned binary", "mediamtx kept")
        again = sh(str(tool), "--yes", check_rc=False)
        check(again.returncode == 0 and serving() == ("1.1.0", BUILD_A, BUILD_A),
              "running it again is harmless: still 1.1.0 / build A")
        Path("/etc/anyaicam/vms.env").write_text(env_now.replace(f"ANYAICAM_BUILD_ID={BUILD_A}", f"ANYAICAM_BUILD_ID={BUILD_C}"))
        stale = sh(str(tool), "--yes", check_rc=False)
        check(stale.returncode != 0 and "skip releases" in stale.stderr and serving() == ("1.1.0", BUILD_A, BUILD_A),
              "a point that belongs to neither the running build nor its own is refused, nothing changed")
        Path("/etc/anyaicam/vms.env").write_text(env_now)

        step("RE-UPDATE after the rollback: 1.2.0 installs again through the owner flow")
        manifest_r, offer_r = publish("1.2.0", BUILD_B, {"serve.py": SERVE}, update_id="1.2.0-reinstall")
        staged_r = stage_as_agent(manifest_r, offer_r)
        check(staged_r["state"] == "activation_requested", f"agent staged 1.2.0 again ({staged_r['state']} {staged_r['error']})")
        run_watcher()
        result, _ = root_result(manifest_r["update_id"])
        check(result["state"] == "healthy", f"root applier result: {result['state']} {result.get('error', '')}")
        check(serving() == ("1.2.0", BUILD_B, BUILD_B), "1.2.0 / build B serving again")

    passed = sum(1 for ok, _ in RESULTS if ok)
    print(f"\nE2E RESULT: {passed}/{len(RESULTS)} checks passed")


if __name__ == "__main__":
    main()
