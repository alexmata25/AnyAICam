"""Fresh-install customer launcher; never handles the device secret or proof.

Run as the desktop user. Existing sudo installer/validator and the installed
agent claim CLI do all privileged work. A loopback-only in-memory bridge
passes the printed setup code to the existing authenticated claim page via
postMessage, never a portal URL, clipboard, or persisted browser storage.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import webbrowser

CODE_LINE = re.compile(r'^\s*Claim code:\s+([A-Z0-9]{8})\s*$')
MAX_BRIDGE_SECONDS = 15 * 60

# This document contains no code. The local nonce is scrubbed before any
# navigation and used only in a loopback request header; the code stays in RAM.
BRIDGE_SCRIPT = r"""
const token = location.hash.slice(1);
history.replaceState(null, '', '/');
let portalWindow = null, handoff = null, delivered = false, pending = false;
const challenge = crypto.randomUUID();
const status = document.getElementById('status');
const button = document.getElementById('connect');
fetch('/handoff', {headers: {'X-AnyAiCam-Local-Token': token}, cache: 'no-store'})
  .then(r => {if (!r.ok) throw new Error(); return r.json();})
  .then(data => {handoff = data; button.disabled = false; document.getElementById('manual-code').textContent = data.claim_code;})
  .catch(() => {status.textContent = 'This setup window has expired. Restart setup to try again.';});
button.onclick = () => {
  portalWindow = window.open(handoff.portal_url + '/customer/claim-appliance', '_blank');
  if (!portalWindow) {status.textContent = 'Allow the sign-in window, then click Connect AnyAiCam again.'; return;}
  status.textContent = 'Sign in as the account owner in the new window, then select a location and confirm.';
};
window.addEventListener('message', event => {
  if (!handoff || delivered || event.source !== portalWindow || event.origin !== handoff.portal_url) return;
  if (!event.data || event.data.challenge !== challenge) return;
  if (event.data.type === 'anyaicam-claim-ready') {
    pending = true;
    portalWindow.postMessage({type: 'anyaicam-claim-prefill', challenge, code: handoff.claim_code}, handoff.portal_url);
  } else if (pending && event.data.type === 'anyaicam-claim-prefilled') {
    handoff.claim_code = ''; delivered = true; document.getElementById('manual-code').textContent = 'Code sent to portal.';
    status.textContent = 'Setup code sent. Review your system and location in the sign-in window and confirm.';
  }
});
const timer = setInterval(() => {
  if (delivered) {clearInterval(timer); return;}
  if (portalWindow && !portalWindow.closed && handoff) {
    const message = pending ? {type: 'anyaicam-claim-prefill', challenge, code: handoff.claim_code} : {type: 'anyaicam-claim-probe', challenge};
    portalWindow.postMessage(message, handoff.portal_url);
  }
}, 1000);
"""


def portal_origin(value: str) -> str:
    parts = urlsplit(value.strip())
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or parts.path not in ('', '/')):
        raise ValueError('Enter the HTTPS web address used to sign in to AnyAiCam, without a page path.')
    try:
        parts.port
    except ValueError as error:
        raise ValueError('The sign-in website has an invalid port.') from error
    host = parts.hostname.encode('idna').decode('ascii').lower()
    if any(ch.isspace() or ord(ch) < 32 for ch in host):
        raise ValueError('The sign-in website contains invalid characters.')
    if ':' in host:
        host = '[' + host + ']'
    return 'https://' + host + (':' + str(parts.port) if parts.port not in (None, 443) else '')


class ClaimBridge:
    """Bound to IPv4 loopback; bearer nonce + exact Host gate code reads.

    No CORS, no filesystem persistence, no HTTP access logging. There is no
    listener on the LAN. Closing setup immediately invalidates the nonce.
    """
    def __init__(self, code: str, portal: str, *, lifetime: int = MAX_BRIDGE_SECONDS):
        if not re.fullmatch(r'[A-Z0-9]{8}', code):
            raise ValueError('Invalid setup code.')
        self.portal = portal_origin(portal)
        self.code = code
        self.token = secrets.token_urlsafe(32)
        self.expires = time.monotonic() + min(lifetime, MAX_BRIDGE_SECONDS)
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                expected_host = f'127.0.0.1:{self.server.server_port}'
                if self.headers.get('Host') != expected_host or time.monotonic() >= bridge.expires:
                    self.send_error(403)
                    return
                if self.path == '/':
                    script_hash = base64.b64encode(hashlib.sha256(BRIDGE_SCRIPT.encode()).digest()).decode()
                    body = ('<!doctype html><html lang="en"><meta charset="utf-8">'
                            '<meta name="viewport" content="width=device-width,initial-scale=1">'
                            '<title>Connect AnyAiCam</title><h1>Connect AnyAiCam</h1>'
                            '<p>Keep this window open while you sign in and confirm your system.</p>'
                            '<button id="connect" disabled>Connect AnyAiCam</button>'
                            '<p id="status" role="status">Preparing secure setup...</p>'
                            '<details><summary>Manual connection if the sign-in window cannot connect</summary>'
                            '<p>In AnyAiCam, open Claim an appliance and enter this setup code:</p>'
                            '<p id="manual-code"></p></details>'
                            f'<script>{BRIDGE_SCRIPT}</script></html>').encode()
                    content_type = 'text/html; charset=utf-8'
                    csp = f"default-src 'none'; script-src 'sha256-{script_hash}'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
                elif self.path == '/handoff':
                    supplied = self.headers.get('X-AnyAiCam-Local-Token', '')
                    if not hmac.compare_digest(supplied, bridge.token):
                        self.send_error(403)
                        return
                    # Origin is absent for some same-origin fetches. Never
                    # grant a cross-origin read, even with a correct nonce.
                    if self.headers.get('Origin') not in (None, f'http://{expected_host}'):
                        self.send_error(403)
                        return
                    body = json.dumps({'claim_code': bridge.code, 'portal_url': bridge.portal}).encode()
                    content_type = 'application/json'
                    csp = "default-src 'none'; frame-ancestors 'none'"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', csp)
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def open_browser(self) -> bool:
        # This nonce is NOT the claim code, device secret, session id or proof.
        # A fragment is never sent in HTTP and the bridge immediately scrubs it.
        url = f'http://127.0.0.1:{self.server.server_port}/#{self.token}'
        return webbrowser.open(url, new=1)

    def close(self):
        self.expires = 0
        self.code = ''
        self.token = ''
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def verify_package(root: Path):
    entries = json.loads((root / 'artifact-files.json').read_text(encoding='utf-8'))
    if not isinstance(entries, list) or not entries:
        raise ValueError('The downloaded package has no file verification list.')
    required = {'install.sh', 'validate.sh', 'setup.sh', 'customer_setup.py', 'release.env'}
    verified = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
            raise ValueError('The downloaded package has an invalid file verification list.')
        rel = entry['path']
        path = root / rel
        if (not rel or Path(rel).is_absolute() or '..' in Path(rel).parts
                or path.is_symlink() or not path.resolve().is_relative_to(root.resolve())):
            raise ValueError('The downloaded package contains an unsafe file path.')
        with path.open('rb') as handle:
            actual = hashlib.file_digest(handle, 'sha256').hexdigest()
        if actual != entry.get('sha256'):
            raise ValueError('The downloaded package has changed. Download it again.')
        verified.add(rel)
    if not required <= verified:
        raise ValueError('The downloaded package is incomplete.')


def require_fresh_desktop_install():
    if sys.platform != 'linux' or os.geteuid() == 0:
        raise ValueError('Start setup as your normal Ubuntu desktop user, not as root.')
    os_release = Path('/etc/os-release').read_text(encoding='utf-8')
    if not re.search(r'^ID="?ubuntu"?$', os_release, re.M) or not re.search(r'^VERSION_ID="?24\.04"?$', os_release, re.M):
        raise ValueError('This setup package requires Ubuntu 24.04.')
    if any(p.exists() for p in (Path('/etc/anyaicam/installed_version'),
                               Path('/etc/anyaicam/appliance_identity.json'), Path('/opt/anyaicam'))):
        raise ValueError('AnyAiCam is already installed here. Contact setup help; this launcher only supports a new installation.')


def run_claim(portal: str) -> int:
    command = ['sudo', '-u', 'anyaicam', '/opt/anyaicam-agent/venv/bin/anyaicam-setup',
               '--claim', '--portal-url=' + portal, '--mode=production']
    bridge = None
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    try:
        for line in process.stdout:
            match = CODE_LINE.fullmatch(line.rstrip('\r\n'))
            if match:
                if bridge:
                    bridge.close()
                bridge = None
                try:
                    bridge = ClaimBridge(match[1], portal)
                    opened = bridge.open_browser()
                except Exception:
                    opened = False
                if opened:
                    print('Your browser is ready. Click Connect AnyAiCam, sign in, select a location, and confirm.', flush=True)
                else:
                    # Existing manual recovery remains available; never print
                    # the bridge nonce, device secret, proof or credential.
                    print('Could not open your browser. Sign in to AnyAiCam and open Claim an appliance.', flush=True)
                    print('Setup code: ' + match[1], flush=True)
            else:
                print(line, end='', flush=True)
        return process.wait()
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    finally:
        if bridge:
            bridge.close()
        if process.stdout:
            process.stdout.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Install and connect AnyAiCam on a new Ubuntu computer.')
    parser.add_argument('--portal-url')
    parser.add_argument('--product-mode', choices=('local', 'hybrid'))
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent
    try:
        require_fresh_desktop_install()
        portal = portal_origin(args.portal_url or input('AnyAiCam sign-in website (https://...): ').strip())
        mode = args.product_mode or input('Your AnyAiCam plan (local or hybrid): ').strip().lower()
        if mode not in ('local', 'hybrid'):
            raise ValueError('Choose Local or Hybrid to match your AnyAiCam plan.')
        verify_package(root)
        print('Installing AnyAiCam. Ubuntu may ask for your computer password.', flush=True)
        subprocess.run(['sudo', 'bash', str(root / 'install.sh'), '--product-mode=' + mode], cwd=root, check=True)
        subprocess.run(['sudo', 'bash', str(root / 'validate.sh')], cwd=root, check=True)
        return run_claim(portal)
    except (ValueError, OSError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print('Setup stopped: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
