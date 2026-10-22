"""Windows 1.3.3 TEST candidate acceptance (Blue, 2026-10-21): the harness additions. installer/windows/sandbox is test
tooling only, never packaged.

* sandbox-proxy.py: headless Edge renders the REAL VMS pages with the owner's session (cookie from the edge-http.py
  LWP jar), WHEP offers are forwarded to the VMS MediaMTX unchanged, and the page's result + frame are saved;
* functional-in-sandbox.ps1: the U132 upgrade / rollback flow and the D1/D2/O3/pricing checks are wired where the
  checklist says (BLUE-WINDOWS-1.3.3-READINESS.md section 3)."""
import base64
import json
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / "installer" / "windows" / "sandbox"
SCRIPT = (SANDBOX / "functional-in-sandbox.ps1").read_text(encoding="utf-8-sig")


class _Stub(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def _reply(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _Stub.seen.append(("GET", self.path, self.headers.get("Cookie")))
        self._reply(200, b"<html>subscription page</html>", "text/html")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _Stub.seen.append(("POST", self.path, body.decode()))
        self._reply(201, b"v=0\r\nanswer\r\n", "application/sdp")


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture()
def proxy(tmp_path):
    vms, mtx = _serve(_Stub), _serve(_Stub)
    jar = tmp_path / "owner.cookies"
    jar.write_text('#LWP-Cookies-2.0\nSet-Cookie3: anyaicam_partner_session="tok123"; path="/"; domain="127.0.0.1"; path_spec; discard; version=0\n',
                   encoding="utf-8")
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    results = tmp_path / "results"
    process = subprocess.Popen([sys.executable, str(SANDBOX / "sandbox-proxy.py"), "--jar", str(jar), "--results", str(results),
                                "--port", str(port), "--vms", f"http://127.0.0.1:{vms.server_address[1]}",
                                "--mediamtx", f"http://127.0.0.1:{mtx.server_address[1]}"])
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/__whep.html", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    _Stub.seen = []
    yield base, results
    process.kill()
    vms.shutdown()
    mtx.shutdown()


def test_pages_are_forwarded_to_the_vms_with_the_owner_session(proxy):
    base, _ = proxy
    page = urllib.request.urlopen(base + "/subscription-portal", timeout=5).read()
    assert page == b"<html>subscription page</html>"
    assert ("GET", "/subscription-portal", "anyaicam_partner_session=tok123") in _Stub.seen


def test_whep_offers_go_to_the_mediamtx_path_unchanged(proxy):
    base, _ = proxy
    request = urllib.request.Request(base + "/__whep/cam-1", data=b"v=0\r\noffer\r\n", method="POST", headers={"Content-Type": "application/sdp"})
    answer = urllib.request.urlopen(request, timeout=5)
    assert answer.status == 201 and b"answer" in answer.read()
    assert ("POST", "/cam-1/whep", "v=0\r\noffer\r\n") in _Stub.seen


def test_the_whep_page_and_its_result_and_frame_are_saved(proxy):
    base, results = proxy
    page = urllib.request.urlopen(base + "/__whep.html?path=cam-1", timeout=5).read().decode()
    assert "RTCPeerConnection" in page and "/__whep/" in page and "recvonly" in page
    urllib.request.urlopen(urllib.request.Request(base + "/__result", data=json.dumps({"ok": True}).encode(), method="POST"), timeout=5)
    png = b"\x89PNG\r\n\x1a\nfake"
    urllib.request.urlopen(urllib.request.Request(base + "/__frame", data=b"data:image/png;base64," + base64.b64encode(png), method="POST"), timeout=5)
    assert json.loads((results / "webrtc-result.json").read_text()) == {"ok": True}
    assert (results / "webrtc-frame.png").read_bytes() == png


def test_the_proxy_listens_on_loopback_only():
    source = (SANDBOX / "sandbox-proxy.py").read_text(encoding="utf-8")
    assert 'ThreadingHTTPServer(("127.0.0.1", args.port)' in source


# ---------------------------------------------------------------- functional-in-sandbox.ps1 wiring
def test_upgrade_runs_install_the_previous_setup_first_and_upgrade_after_the_camera_records():
    assert "$firstSetup = if ($upgrade) { $previousSetup } else { $setup }" in SCRIPT
    upgrade = SCRIPT.index("# ---------------------------------------------------------------- UPGRADE 1.3.2")
    assert SCRIPT.index("Check 'edge sync: the camera reaches the VMS") < upgrade < SCRIPT.index("# ---------------------------------------------------------------- live view")
    block = SCRIPT[upgrade:SCRIPT.index("# ---------------------------------------------------------------- live view")]
    for expected in ("upgrade (before): 1.3.2 recorded", "upgrade: the setup under test runs over 1.3.2", "are kept",
                     "the clips recorded by 1.3.2 are still listed and play", "(downgrade) and changes nothing",
                     'SubscriptionCheck "$mode-after-upgrade"'):
        assert expected in block, expected


def test_fix_forward_runs_only_in_the_local_upgrade_scenario_before_the_revocation():
    # Owner policy 2026-10-22: fix-forward; the 1.3.2 setup must be refused over the newer data and change nothing,
    # then the setup under test installs again with identity, link and recordings kept. No version-marker reset.
    start = SCRIPT.index("if ($upgrade -and $mode -eq 'local') {")
    assert start < SCRIPT.index("[void](Cloud POST '/test/revoke-appliance' -Owner)")
    block = SCRIPT[start:SCRIPT.index("[void](Cloud POST '/test/revoke-appliance' -Owner)")]
    assert "rollback: uninstalling the new version" in block
    assert "fix-forward: the 1.3.2 setup is refused over the newer data folder and changes nothing" in block
    assert "if ($rbI.ExitCode -eq 0) { throw '1.3.2 installed over the newer data folder' }" in block
    assert "fix-forward: the setup under test installs again (exit 0)" in block
    assert "Start-Process -FilePath $setup.FullName" in block
    assert "the accepted 1.3.2 installs again" not in SCRIPT
    assert "ANYAICAM_WINDOWS_SETUP_VERSION" not in block  # never edits the version marker


def test_the_mode_check_looks_at_the_running_vms_not_only_the_mode_file():
    # D3 on 126863e3: product_mode.json said hybrid while the running VMS kept the Local flags.
    assert "$proc = WaitFor 180 { VmsStartedAfterModeSaved }" in SCRIPT
    assert r"uvicorn\s+main:app" in SCRIPT
    assert "$started -gt $saved" in SCRIPT


def test_webrtc_waits_for_the_camera_path_and_requires_one_vms_mediamtx():
    assert "/v3/paths/get/$camId" in SCRIPT and "$pth.ready" in SCRIPT
    assert "if ($mtx -ne 1)" in SCRIPT


def test_revocation_treats_an_unreachable_vms_as_inconclusive_not_accepted():
    block = SCRIPT[SCRIPT.index("Check 'revoked appliance: the cloud refuses it"):]
    block = block[:block.index("Info 'revoked appliance")]
    assert "inconclusive: the VMS is not healthy" in block
    assert "[int]$script:lastLogin.status -eq 0" in block


def test_the_d2_checks_follow_recording_motion_every_switch_and_offline():
    for call in ("PlaylistFresh 'recording'", "PlaylistFresh 'motion'", "MotionReaderRtsp 'motion'",
                 'PlaylistFresh "after-switch-$Target"', 'MotionReaderRtsp "after-switch-$Target"', "PlaylistFresh 'offline'"):
        assert call in SCRIPT, call


def test_moq_and_webrtc_are_checked_in_hybrid_and_after_every_switch_to_hybrid():
    assert "if ($mode -eq 'hybrid') { MoqCheck $mode; WebRtcCheck $mode }" in SCRIPT
    assert 'if ($Target -eq \'hybrid\') { MoqCheck "after the switch to hybrid"; WebRtcCheck "after-switch-hybrid" }' in SCRIPT
    assert "--allow-loopback-in-peer-connection" in SCRIPT


def test_pricing_is_checked_on_the_version_under_test_only():
    assert "if (-not $upgrade) { SubscriptionCheck $mode }" in SCRIPT
    check = SCRIPT[SCRIPT.index("function SubscriptionCheck"):SCRIPT.index("function UpgradeState")]
    for amount in ("'$7.99'", "'$4.99'", "'$39.99'", "'$49.99'", "'$69.99'"):
        assert amount in check
    assert "--screenshot=$png" in check


def test_saved_pages_are_included_in_the_secrets_scan():
    assert "'.log', '.txt', '.json', '.m3u8', '.html'" in SCRIPT
