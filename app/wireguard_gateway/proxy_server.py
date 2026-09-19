"""WireGuard gateway -- internal proxy HTTP server (Phase C, controlled
camera1-only proof).

Wraps proxy.py's forward() in a tiny HTTP server so the SEPARATE portal
process/container can ask this gateway process to fetch bytes from an
appliance over the tunnel, without the two processes sharing any Python
state -- they are different containers by design (see gateway_service.py's
own placement-decision docstring). Not started by anything unless
ANYAICAM_WIREGUARD_GATEWAY_INTERNAL_PROXY_ENABLED=true (default: unset/off,
zero behavior change to the already-live, reconcile-only gateway process).

Reachability is the only access control this server has today, by design,
matching the docker-network-only reachability of every other internal
control surface already in this codebase (webrtc_publisher.py's MediaMTX
apiAddress/webrtcAddress at 127.0.0.1, one layer down): this server is
never given a `-p`/compose `ports` mapping to the host, so nothing outside
the existing `deploy_default` docker network can ever reach it -- the
published UDP 51820 WireGuard listener is a completely separate socket on
a completely separate process. The caller (live_view_wireguard.py, running
in the SEPARATE portal container) is the ONLY thing responsible for
re-checking live_view_sessions/customer authorization before ever calling
this -- exactly the same division of responsibility proxy.py's own
docstring already establishes for its own forward() function. This server
has no concept of a customer, a session, or a camera, and must never be
given one -- it only ever answers "fetch me this tunnel-address:port/path",
nothing else.

Deliberately narrow, so this can never become an open SSRF pivot into the
rest of the docker network or the internet even if the calling portal
process were ever compromised: only forwards to an address inside the
configured tunnel CIDR (ANYAICAM_WIREGUARD_TUNNEL_CIDR) on a small
allow-listed port set (ANYAICAM_WIREGUARD_GATEWAY_PROXY_PORTS, default
"8000" -- the VMS app's own port, already 0.0.0.0-bound on every appliance
host today, serving the same local HLS/recordings bytes a LAN viewer
already gets). The browser never sees any of this: it never receives a
tunnel address, a gateway hostname, or a port -- see live_view_wireguard.py
for the portal-relative URLs it actually gets instead.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .proxy import GatewayProxyError, forward

log = logging.getLogger("anyaicam.wireguard_gateway.proxy_server")


def _tunnel_cidr() -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    return ipaddress.ip_network(os.environ.get("ANYAICAM_WIREGUARD_TUNNEL_CIDR", "10.70.0.0/16"))


def _allowed_ports() -> set[int]:
    return {
        int(value) for value in os.environ.get("ANYAICAM_WIREGUARD_GATEWAY_PROXY_PORTS", "8000").split(",")
        if value.strip()
    }


def tunnel_address_allowed(value: str, cidr: ipaddress.IPv4Network | ipaddress.IPv6Network | None = None) -> bool:
    cidr = cidr if cidr is not None else _tunnel_cidr()
    try:
        return ipaddress.ip_address(value) in cidr
    except ValueError:
        return False


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        log.info("%s - %s", self.address_string(), format % args)

    def do_GET(self):  # noqa: N802 - stdlib method name
        parsed = urlparse(self.path)
        if parsed.path != "/appliance-fetch":
            self._reject(404, b"not found")
            return
        query = parse_qs(parsed.query)
        tunnel_address = (query.get("tunnel") or [""])[0]
        port_raw = (query.get("port") or [""])[0]
        target_path = (query.get("path") or [""])[0]
        if not tunnel_address_allowed(tunnel_address):
            self._reject(400, b"tunnel address outside configured tunnel CIDR")
            return
        try:
            port = int(port_raw)
        except ValueError:
            port = -1
        if port not in _allowed_ports():
            self._reject(400, b"port not in the configured allow-list")
            return
        if not target_path.startswith("/"):
            self._reject(400, b"path must start with '/'")
            return
        try:
            status, body = forward(tunnel_address, port, target_path)
        except GatewayProxyError as error:
            self._reject(502, str(error).encode())
            return
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body)

    def _reject(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body)


def start(bind_host: str = "0.0.0.0", bind_port: int = 8098) -> ThreadingHTTPServer:
    """Starts the internal proxy server on a background thread and returns
    the server object (caller owns its lifetime -- call .shutdown() to
    stop cleanly, e.g. in a test's own teardown). Only ever called from
    gateway_service.py's real process entry point, and only when
    ANYAICAM_WIREGUARD_GATEWAY_INTERNAL_PROXY_ENABLED=true. bind_host
    defaults to 0.0.0.0 because that is still only the container's own
    network namespace on the deploy_default docker network -- this
    process is never given a host port mapping, which is what actually
    keeps it unreachable from outside that one docker network."""
    server = ThreadingHTTPServer((bind_host, bind_port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(
        "WireGuard gateway internal proxy server listening on %s:%d "
        "(docker-network-only -- never published to the host)",
        bind_host, bind_port,
    )
    return server
