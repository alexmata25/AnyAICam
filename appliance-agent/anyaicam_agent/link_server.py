"""Zero-terminal onboarding (2026-10-08): the appliance's own "AnyAiCam Setup"
page, so a customer links the appliance to their AnyAiCam account from a
browser -- no terminal, and no Cloud ID, activation token or claim code to
see, copy, type or relay.

The installer's desktop entry (installer/14-desktop-setup.sh) opens
http://127.0.0.1:8790/ on the appliance's own desktop. "Link this appliance"
asks the agent's headless claim (headless_claim.py) for a current claim code
of THIS appliance's open claim and redirects the browser to

    <portal>/claim#code=<code>

The code travels only in the URL fragment, which browsers never send to a
server; the cloud's /claim page moves it into the tab and out of the address
bar, the customer signs in and confirms the site, and the headless claim
redeems that confirmation exactly as before (device secret, single-use proof,
credential) -- the security machinery underneath is unchanged.

Who can use it -- the same people as a terminal on the appliance:
  * bound to 127.0.0.1 only: nothing on the network can reach it;
  * the Host header must be this loopback address (no DNS rebinding);
  * "Link" is a POST carrying a per-process random token from the page
    (constant-time compare) and, when the browser sends them, a same-origin
    Origin and Sec-Fetch-Site -- a web page in the desktop browser cannot
    trigger it, and could not read the redirect anyway;
  * rate-limited; no-store, same-origin referrer only, no framing, a CSP.
The code is never written to a log, a file or the page.
"""
from __future__ import annotations

import hmac
import html
import json
import os
import secrets
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlsplit

DEFAULT_PORT = 8790
LINK_LIMIT, LINK_WINDOW_SECONDS = 6, 60


class _LoopbackServer(ThreadingHTTPServer):
    # Linux refuses a second listener on the port even with SO_REUSEADDR (which
    # only lets a restarted agent rebind past TIME_WAIT); Windows would let
    # another program bind the same port with it, so never there.
    allow_reuse_address = os.name != 'nt'


def _origin(url: str) -> str:
    parts = urlsplit(str(url or ''))
    return f'{parts.scheme}://{parts.netloc}'


class LinkServer:
    """Serves the local setup page. `headless` returns the agent's current
    HeadlessClaim (or None when the appliance cannot claim itself);
    `linked` says whether the appliance already has its credential."""

    def __init__(self, config, log, headless: Callable[[], object], linked: Callable[[], bool],
                 port: int = DEFAULT_PORT, host: str = '127.0.0.1', clock: Callable[[], float] = time.monotonic):
        if host not in ('127.0.0.1', '::1'):
            raise ValueError('the setup page is served on the loopback interface only')
        self.config, self.log, self.headless, self.linked, self.clock = config, log, headless, linked, clock
        self.token = secrets.token_urlsafe(32)
        self._links = deque()
        self._lock = threading.Lock()
        self.httpd = _LoopbackServer((host, port), self._handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.allowed_hosts = {f'127.0.0.1:{self.port}', f'localhost:{self.port}', f'[::1]:{self.port}'}
        self.thread = None

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> 'LinkServer':
        self.thread = threading.Thread(target=self.httpd.serve_forever, name='anyaicam-link-server', daemon=True)
        self.thread.start()
        self.log.info('AnyAiCam Setup page available on this appliance at http://127.0.0.1:%s/', self.port)
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # ---------------------------------------------------------------- policy
    @property
    def portal(self) -> str:
        return _origin(self.config.portal_url)

    def _allow_link(self) -> bool:
        now = self.clock()
        with self._lock:
            while self._links and now - self._links[0] > LINK_WINDOW_SECONDS:
                self._links.popleft()
            if len(self._links) >= LINK_LIMIT:
                return False
            self._links.append(now)
            return True

    def status(self) -> dict:
        linked = bool(self.linked())
        return {'linked': linked, 'can_link': (not linked) and self.headless() is not None, 'portal': self.portal}

    # ---------------------------------------------------------------- pages
    def page(self) -> str:
        state = self.status()
        portal = html.escape(self.portal, quote=True)
        if state['linked']:
            body = ('<h1>This appliance is linked to your AnyAiCam account</h1>'
                    f'<p><a class="button" href="{portal}/customer/setup">Continue in AnyAiCam</a></p>'
                    '<p class="note">Discover and add your cameras in AnyAiCam. The local VMS on this computer: '
                    '<a href="http://127.0.0.1:8000/">http://127.0.0.1:8000</a></p>')
        elif state['can_link']:
            body = ('<h1>Link this appliance to your AnyAiCam account</h1>'
                    '<p>Sign in to AnyAiCam (or create an account), choose where this appliance is installed, and confirm. '
                    'The appliance links itself &mdash; there is nothing to copy or type.</p>'
                    '<form method="post" action="/link">'
                    f'<input type="hidden" name="token" value="{html.escape(self.token, quote=True)}">'
                    '<button class="button" type="submit">Link this appliance</button></form>'
                    '<p class="note">This page works only on this appliance itself.</p>')
        else:
            body = ('<h1>AnyAiCam Setup is not available yet</h1>'
                    '<p>This appliance is not set up to link itself to an AnyAiCam account (no AnyAiCam cloud address is '
                    'configured, or it is still starting). Please contact AnyAiCam support.</p>')
        return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1"><title>AnyAiCam Setup</title>'
                '<style>body{font-family:system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem;color:#0b1530}'
                '.button{display:inline-block;background:#16778b;color:#fff;border:0;border-radius:10px;padding:.8rem 1.3rem;'
                'font-size:1rem;text-decoration:none;cursor:pointer}.note{color:#64748b;font-size:.9rem}</style>'
                f'</head><body><p class="note">AnyAiCam Setup</p>{body}</body></html>')

    # ---------------------------------------------------------------- HTTP
    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = 'AnyAiCamSetup'
            sys_version = ''

            def log_message(self, fmt, *args):  # request line only (never a query, a body or a code)
                server.log.debug('setup page %s %s', self.command, urlsplit(self.path).path)

            def _send(self, status: int, body: bytes = b'', content_type: str = 'text/html; charset=utf-8',
                      location: str | None = None) -> None:
                self.send_response(status)
                if location:
                    self.send_header('Location', location)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                # same-origin, not no-referrer: with no-referrer a browser sends
                # 'Origin: null' on the page's own form POST. Either way the cloud
                # never gets a referrer from this page.
                self.send_header('Referrer-Policy', 'same-origin')
                self.send_header('X-Frame-Options', 'DENY')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy',
                                 f"default-src 'none'; style-src 'unsafe-inline'; form-action 'self' {server.portal}; "
                                 "frame-ancestors 'none'; base-uri 'none'")
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(body)

            def _host_ok(self) -> bool:
                if self.headers.get('Host', '') in server.allowed_hosts:
                    return True
                self._send(421, b'Misdirected request')
                return False

            def do_GET(self):
                if not self._host_ok():
                    return
                path = urlsplit(self.path).path
                if path == '/':
                    return self._send(200, server.page().encode())
                if path == '/status':
                    return self._send(200, json.dumps(server.status()).encode(), 'application/json')
                return self._send(404, b'Not found')

            do_HEAD = do_GET

            def do_POST(self):
                if not self._host_ok():
                    return
                if urlsplit(self.path).path != '/link':
                    return self._send(404, b'Not found')
                origin = self.headers.get('Origin')
                if origin is not None and origin.lower() not in {f'http://{h}' for h in server.allowed_hosts}:
                    return self._send(403, b'Forbidden')
                if self.headers.get('Sec-Fetch-Site', 'same-origin') not in ('same-origin', 'none'):
                    return self._send(403, b'Forbidden')
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                except ValueError:
                    length = -1
                if length < 0:
                    return self._send(400, b'Bad request')
                if length > 4096:
                    return self._send(413, b'Too large')
                form = parse_qs(self.rfile.read(length).decode('utf-8', 'replace'))
                token = (form.get('token') or [''])[0]
                if not hmac.compare_digest(token.encode(), server.token.encode()):
                    return self._send(403, b'Forbidden')
                if server.linked():
                    return self._send(303, location=f'{server.portal}/customer/setup')
                if not server._allow_link():
                    return self._send(429, b'Too many attempts; wait a minute and try again.')
                claim = server.headless()
                code = claim.link_code() if claim is not None else None
                if not code:
                    page = ('<!doctype html><meta charset="utf-8"><title>AnyAiCam Setup</title>'
                            '<p>AnyAiCam could not be reached just now. Check that this appliance is connected to the '
                            'internet, then <a href="/">try again</a>.</p>').encode()
                    return self._send(503, page)
                server.log.info('Setup page: sent the owner to AnyAiCam to link this appliance.')
                return self._send(303, location=f'{server.portal}/claim#code={code}')

        return Handler
