"""WireGuard gateway -- process entry point (Phase B).

NOT started by anything in this codebase today. No docker-compose
service, systemd unit, or Dockerfile CMD anywhere in this repository
references this module -- see deploy/docker-compose.staging.example.yml
for the exact (commented-out, NOT applied to real staging) service
block a future Phase C/D deployment pass would add. This module exists
so the gateway's own reconciliation loop is real, tested source code
now, not merely a description in the plan doc -- running it for real
against a live interface is deliberately deferred to its own separate
authorization.

## Placement decision (plan doc Sec 19's open item, resolved here)

Runs as a SEPARATE container from the SAME already-built `deploy-portal`
image already used for the customer-facing portal, on the SAME existing
`deploy_default` docker network, on the SAME existing staging EC2
instance (`i-0a082abd812929bb4`) -- chosen as the safest option because
it requires zero new image, zero new build pipeline, and zero new
compute instance; only a second container definition using an image
that already exists and is already deployed regularly. The only new
real-infrastructure surface this eventually needs is one new UDP port
opened on that same EC2 instance's security group for the WireGuard
listener itself, and `--cap-add=NET_ADMIN` (only) on the new
container -- the existing `portal-*` container gets neither. This is
the same "one minimal, reviewable process holds the elevated
capability, nothing else does" shape relay_control.py's own hardware
boundary and webrtc_publisher.py's own loopback-only control surface
already establish elsewhere in this codebase, extended to the gateway.

The alternative considered and rejected for this pass: a wholly new EC2
instance/VPC surface. Rejected because it multiplies the operational
surface (a second instance to patch, monitor, and pay for) for a
Phase B/C/D feature that has not yet had its first real end-to-end test
against a real appliance -- reusing the existing, already-monitored
staging host until real load or isolation requirements say otherwise is
the safer default, and nothing about this module's own code depends on
that choice (it only needs Docker network reachability to the portal's
database and, eventually, real WireGuard interface access -- both
satisfied either way).

## What this module actually does (once started for real)

Polls the database's own current appliance_wireguard_peers rows (an
authoritative, human/UI/API-editable set -- see wireguard_remote.py) on
a fixed interval and drives a WireGuardInterfaceProvider (see
interface_provider.py) to match. Optionally (INTERNAL_PROXY_ENABLED,
off by default) also starts proxy_server.py's internal, docker-network-
only HTTP server on a background thread alongside the reconcile loop --
see that module's own docstring for why this is safe to add. Neither the
reconcile loop nor the proxy server has any concept of a customer,
session, or camera; live_view_wireguard.py (running in the SEPARATE
portal container) owns all of that.
"""

from __future__ import annotations

import logging
import os
import time

from partner_db import connection

from .interface_provider import (
    MockWireGuardInterfaceProvider,
    SystemWireGuardInterfaceProvider,
    WireGuardInterfaceProvider,
)
from .reconciler import desired_peers_from_rows, reconcile

log = logging.getLogger("anyaicam.wireguard_gateway")

RECONCILE_INTERVAL_SECONDS = int(os.environ.get("ANYAICAM_WIREGUARD_GATEWAY_RECONCILE_SECONDS", "30"))

# Explicit, fail-safe provider selection for the real process entry point
# below -- unset/anything-other-than-"mock" keeps today's real default
# (SystemWireGuardInterfaceProvider, requiring a real wg0 interface and
# CAP_NET_ADMIN), so no existing deploy assumption changes. Set only for a
# staging verification pass that needs run_forever()'s own real reconcile
# loop, real DB polling, and real logging exercised over the network
# against the real staging database, with zero real `wg`/`ip` interface
# access -- see docs/wireguard-remote-connectivity-plan.md Sec 21 (Phase C
# staging verification) for why this is the one deploy-time switch that's
# safe to flip on staging before a real interface exists anywhere. Never
# applicable to a production deploy: production is out of scope for this
# whole feature so far.
GATEWAY_PROVIDER_MODE = os.environ.get("ANYAICAM_WIREGUARD_GATEWAY_PROVIDER", "system").strip().lower()

# Off by default -- zero behavior change to this already-live process
# unless explicitly turned on for the Phase C controlled-camera proof
# (see live_view_wireguard.py). Safe to add: proxy_server.py's own module
# docstring covers why this listener is never given a host port mapping,
# so it is reachable only from the existing deploy_default docker
# network the portal container already shares with this one, never the
# host or the internet.
INTERNAL_PROXY_ENABLED = os.environ.get(
    "ANYAICAM_WIREGUARD_GATEWAY_INTERNAL_PROXY_ENABLED", "false"
).strip().lower() == "true"


def _provider_from_env() -> WireGuardInterfaceProvider:
    if GATEWAY_PROVIDER_MODE == "mock":
        log.warning(
            "WireGuard gateway starting with MockWireGuardInterfaceProvider "
            "(ANYAICAM_WIREGUARD_GATEWAY_PROVIDER=mock) -- no real interface "
            "will ever be touched by this process."
        )
        return MockWireGuardInterfaceProvider()
    return SystemWireGuardInterfaceProvider()


def _active_peer_rows(db) -> list[dict]:
    return [
        dict(row) for row in db.execute(
            "SELECT public_key, tunnel_address FROM appliance_wireguard_peers WHERE revoked_at IS NULL"
        ).fetchall()
    ]


def reconcile_once(provider: WireGuardInterfaceProvider) -> None:
    with connection() as db:
        rows = _active_peer_rows(db)
    result = reconcile(desired_peers_from_rows(rows), provider)
    if result.added or result.removed:
        log.info("WireGuard gateway reconciled: +%d -%d peers", len(result.added), len(result.removed))


def run_forever(
    provider: WireGuardInterfaceProvider | None = None,
    *,
    interval: float = RECONCILE_INTERVAL_SECONDS,
    sleep=time.sleep,
    stop_after: int | None = None,
) -> None:
    """stop_after: test-only -- run exactly N reconcile iterations then
    return, instead of looping forever. Never instantiates
    SystemWireGuardInterfaceProvider (the default when provider=None)
    in any test -- every test passes an explicit
    MockWireGuardInterfaceProvider."""
    active_provider = provider or _provider_from_env()
    iterations = 0
    while stop_after is None or iterations < stop_after:
        try:
            reconcile_once(active_provider)
        except Exception:
            log.exception("WireGuard gateway reconciliation failed; will retry next cycle")
        iterations += 1
        if stop_after is not None and iterations >= stop_after:
            break
        sleep(interval)


if __name__ == "__main__":  # pragma: no cover - real process entry point, not exercised by tests
    logging.basicConfig(level=logging.INFO)
    if INTERNAL_PROXY_ENABLED:
        from .proxy_server import start as start_internal_proxy_server
        start_internal_proxy_server()
    run_forever()
