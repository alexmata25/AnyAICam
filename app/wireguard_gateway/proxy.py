"""WireGuard gateway -- appliance proxy (Phase B).

The routing/proxy half of the plan doc's Sec 3 architecture decision:
once an appliance's tunnel is up, the gateway reaches that appliance's
already-loopback-bound local surfaces (MediaMTX's WHEP endpoint today;
the VMS's local HLS/API elsewhere) over the tunnel's private address,
and hands the result to an already-authorized browser session the same
way the existing relay path already does -- FastAPI/the portal process
itself still never touches a media byte directly; this module is the
one place that does, running only inside the separate gateway process
(see gateway_service.py), never inside app/main.py's own process.

Deliberately has ZERO WireGuard-specific code: it only ever dials a
plain `http://<ip>:<port><path>` URL. This is what makes it fully
testable against an ordinary loopback HTTP server with no real tunnel
involved at all -- from this module's own point of view, a WireGuard
tunnel address is indistinguishable from any other reachable IP, which
is exactly the structural property the plan doc's core architecture
decision (Sec 3) depends on: nothing about "how the bytes got
routable" needs to appear in the code that fetches them.

Whatever future Phase C caller wires this into a real customer-facing
flow MUST re-check live_view_sessions authorization first, exactly
like every other transport already does (plan doc Sec 4) -- this
module has no authorization concept of its own and must never be
treated as one.
"""

from __future__ import annotations

import urllib.error
import urllib.request


class GatewayProxyError(RuntimeError):
    pass


def forward(
    tunnel_address: str,
    port: int,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    timeout: float = 10,
) -> tuple[int, bytes]:
    """Fetches one request over the tunnel and returns (status_code,
    body_bytes). Raises GatewayProxyError only for a network-level
    failure (tunnel down, appliance unreachable, timeout) -- an HTTP
    error response FROM the appliance (4xx/5xx) is returned normally,
    exactly like the caller would see from a direct connection, never
    converted into an exception."""
    if not path.startswith("/"):
        raise ValueError("path must start with '/'")
    url = f"http://{tunnel_address}:{port}{path}"
    request = urllib.request.Request(url, data=body, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise GatewayProxyError(f"Could not reach appliance over the tunnel: {error}") from error
