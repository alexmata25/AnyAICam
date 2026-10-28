"""1.2.7 / Windows 1.3.2 functional Sandbox scenarios F-L / F-H (Blue, 2026-10-21): the harness itself.
installer/windows/sandbox is test tooling only -- never packaged -- and the functional runs are labelled
SYNTHETIC-KEYSET / SIMULATED CAMERA in every report."""
import json
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
SANDBOX = WIN / "sandbox"
FUNCTIONAL = SANDBOX / "functional-in-sandbox.ps1"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_no_sandbox_tooling_is_packaged_by_the_setup():
    iss = _text(WIN / "AnyAiCam-VMS.iss")
    files = iss.split("[Files]", 1)[1].split("\n[", 1)[0]
    assert "sandbox" not in files.lower()
    for name in ("functional-in-sandbox.ps1", "edge-http.py", "onvif-camera-sim.py", "test-cloud.py"):
        assert name not in iss


def test_every_functional_report_is_labelled_synthetic_keyset_simulated_camera():
    script = _text(FUNCTIONAL)
    assert "LABEL: SYNTHETIC-KEYSET / SIMULATED CAMERA -- not production-keyset acceptance, not physical-camera acceptance." in script
    assert 'RESULT: {0} checks, {1} failed (SYNTHETIC-KEYSET / SIMULATED CAMERA)' in script


def test_the_functional_run_refuses_anything_but_the_synthetic_build_and_starts_the_cloud_in_functional_mode():
    script = _text(FUNCTIONAL)
    assert "$settings.keyset_kind -ne 'synthetic-test' -or -not $settings.functional" in script
    start = script.split("function StartCloud {", 1)[1].split("\n}", 1)[0]
    assert "'--functional'" in start and "'--signing-key'" in start


def test_the_functional_run_never_writes_outside_the_sandbox_test_names():
    script = _text(FUNCTIONAL)
    # Only the disposable in-Sandbox cloud; production names are pinned to this VM, as in validate-in-sandbox.ps1.
    assert "$TestPortal = \"https://${TestCloudHost}:8443\"" in script
    for host in ("app.anyaicam.com", "portal.anyaicam.com", "portal-staging.anyaicam.com", "anyaicam.com"):
        assert f"127.0.0.1 {host}" in script
    assert not re.search(r"https://(app|portal|portal-staging)\.anyaicam\.com", script)


def test_simulated_camera_is_loopback_only():
    script = _text(FUNCTIONAL)
    assert "$camHost = '127.0.0.5'" in script
    assert "@('127.0.0.4/30')" in script


def test_event_only_provisioning_and_private_recorder_are_explicit_acceptance_checks():
    script = _text(FUNCTIONAL)
    # 1.3.4.1 reconciliation: provisioning omits the retired recording_mode field, so the stored 'event' mode below
    # proves the appliance's own event-only default rather than an explicitly requested mode.
    provision = next(line for line in script.splitlines() if "/api/customer/cameras/provision" in line)
    assert "recording_mode" not in provision
    assert "recording_mode = 'continuous'" not in script
    assert 'SELECT local_recording_mode FROM cameras' in script
    assert 'continuous customer-facing segment writer detected' in script
    assert 'setup.Length -gt 750000000' in script


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body.get("password") == "right":
            self.send_response(303)
            self.send_header("Location", "/customer-live")
            self.send_header("Set-Cookie", "anyaicam_partner_session=abc; Path=/; HttpOnly")
            self.end_headers()
        else:
            self._json(403, {"detail": "The email or password is incorrect."})

    def do_GET(self):
        if "anyaicam_partner_session=abc" in (self.headers.get("Cookie") or ""):
            self._json(200, {"enrolled": True})
        else:
            self._json(401, {"detail": "Sign in"})

    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _edge(base, *args):
    done = subprocess.run([sys.executable, str(SANDBOX / "edge-http.py"), *args, "--base", base], capture_output=True, text=True, timeout=60)
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_edge_http_keeps_the_session_cookie_across_a_303_without_following_it(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        jar = str(tmp_path / "jar")
        good, bad = tmp_path / "good.json", tmp_path / "bad.json"
        good.write_text('{"password": "right"}', encoding="ascii")
        bad.write_text('{"password": "wrong"}', encoding="ascii")
        assert _edge(base, "GET", "/api", "--jar", jar)["status"] == 401
        denied = _edge(base, "POST", "/login", "--jar", str(tmp_path / "other"), "--json-file", str(bad))
        assert denied["status"] == 403 and denied["body"]["detail"].startswith("The email or password")
        signed = _edge(base, "POST", "/login", "--jar", jar, "--json-file", str(good))
        assert signed["status"] == 303 and signed["location"] == "/customer-live"
        assert _edge(base, "GET", "/api", "--jar", jar) == {"status": 200, "location": None, "content_type": "application/json",
                                                           "length": 18, "body": {"enrolled": True}}
        out = tmp_path / "saved.bin"
        saved = _edge(base, "GET", "/api", "--jar", jar, "--out", str(out))
        assert saved["body"] is None and out.read_bytes() == b'{"enrolled": true}'
        assert _edge("http://127.0.0.1:9", "GET", "/x")["status"] == 0
    finally:
        server.shutdown()
