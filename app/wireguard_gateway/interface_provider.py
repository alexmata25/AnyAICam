"""WireGuard gateway -- interface control abstraction (Phase B).

Mirrors app/relay_control.py's own RelayProvider/MockRelayProvider
split exactly, same reason: every concept a real WireGuard-interface
integration will eventually need (reading the live peer set, adding a
peer, removing a peer) is modeled here as a plain, hardware/interface-
free interface (WireGuardInterfaceProvider) plus one concrete, fully
safe implementation (MockWireGuardInterfaceProvider) that every test in
this codebase uses, and one real, `wg`-CLI-backed implementation
(SystemWireGuardInterfaceProvider) that is defined here as real,
reviewable source code but is NEVER instantiated or invoked by
anything in this pass -- there is no code path anywhere in this repo
today that constructs a SystemWireGuardInterfaceProvider and calls a
method on it. It exists so Phase C/D's own real rollout has a correct,
already-tested-in-shape implementation to point at, not so it runs now.

Safety posture, deliberately conservative, matching relay_control.py's
own numbered list:

1. MockWireGuardInterfaceProvider NEVER touches a real interface --
   it is a pure in-memory dict, thread-safe, records every call for
   test assertions.
2. SystemWireGuardInterfaceProvider's three methods each shell out to
   exactly one fixed-shape `wg` CLI invocation, with `self.interface`
   the only variable ever interpolated into argv (never derived from
   any network-supplied value -- see reconciler.py for where the
   peer's own public_key/allowed_ip values come from: the database,
   already validated at enroll time by wireguard_remote.py's own
   is_valid_wireguard_public_key(), never from a live request this
   provider handles directly).
3. Every add_peer() call's allowed_ip is expected to already be a
   single appliance's own /32 (see reconciler.py's own
   desired_peers_from_rows()) -- this module does not itself enforce
   that shape; it is enforced structurally by the schema/enrollment
   logic that produces the rows this module is ever fed.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class GatewayPeer:
    """One appliance's WireGuard identity, as the gateway's own
    interface needs to know it -- public key and its exact allowed-IPs
    scope (a single /32, per the plan doc Sec 9's isolation guarantee).
    Never carries a private key; there is no field for one."""

    public_key: str
    allowed_ip: str  # e.g. "10.70.0.5/32"


class WireGuardInterfaceProvider:
    """Abstract interface every gateway backend (mock today, the real
    `wg` CLI later) implements. Callers (reconciler.py) depend on this
    interface only -- never on a concrete provider directly -- so the
    real provider is a drop-in replacement, exactly like RelayProvider."""

    def current_peers(self) -> dict[str, str]:  # pragma: no cover - interface
        """Returns {public_key: allowed_ip} for every peer currently
        configured on the live interface."""
        raise NotImplementedError

    def add_peer(self, peer: GatewayPeer) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def remove_peer(self, public_key: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class MockWireGuardInterfaceProvider(WireGuardInterfaceProvider):
    """Test/development provider: records every call it receives and
    NEVER touches any real interface or subprocess -- there is nothing
    to touch. Every test in this pass uses this provider; none
    constructs SystemWireGuardInterfaceProvider."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._peers: dict[str, str] = {}
        self.add_calls: list[GatewayPeer] = []
        self.remove_calls: list[str] = []

    def current_peers(self) -> dict[str, str]:
        with self._lock:
            return dict(self._peers)

    def add_peer(self, peer: GatewayPeer) -> None:
        with self._lock:
            self._peers[peer.public_key] = peer.allowed_ip
            self.add_calls.append(peer)

    def remove_peer(self, public_key: str) -> None:
        with self._lock:
            self._peers.pop(public_key, None)
            self.remove_calls.append(public_key)

    def seed(self, public_key: str, allowed_ip: str) -> None:
        """Test-only: pre-populates a peer as if it were already
        present on the live interface before a reconcile pass runs,
        for exercising the "no change needed" / "must be removed"
        cases without going through add_peer()."""
        with self._lock:
            self._peers[public_key] = allowed_ip


class SystemWireGuardInterfaceProvider(WireGuardInterfaceProvider):
    """Real, `wg`-CLI-backed implementation. Defined for Phase C/D to
    point at -- NEVER instantiated or invoked by any code path exercised
    in this pass (no test constructs one and calls a method on it;
    nothing in gateway_service.py's own default wiring is reachable
    without an explicit, not-yet-written Phase C/D deployment step).
    Requires the named WireGuard interface to already be up with
    CAP_NET_ADMIN available to this process -- bringing that up for
    real is infrastructure/deployment work, not source code, and is
    exactly the kind of real network/interface change this pass's own
    directive reserves for a separate, explicit go-ahead."""

    def __init__(self, interface: str = "wg0", runner=subprocess.run) -> None:
        self.interface = interface
        self._runner = runner

    def current_peers(self) -> dict[str, str]:
        # `wg show <iface> dump`: first line is the interface's own
        # private-key/public-key/listen-port/fwmark row (4 fields);
        # every subsequent line is one peer (public-key, preshared-key,
        # endpoint, allowed-ips, latest-handshake, transfer-rx,
        # transfer-tx, persistent-keepalive -- 8 fields), tab-separated.
        # Only public-key (0) and allowed-ips (3) are used here.
        result = self._runner(
            ["wg", "show", self.interface, "dump"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        lines = result.stdout.splitlines()
        peers: dict[str, str] = {}
        for line in lines[1:]:
            fields = line.split("\t")
            if len(fields) >= 4:
                peers[fields[0]] = fields[3]
        return peers

    def add_peer(self, peer: GatewayPeer) -> None:
        self._runner(
            ["wg", "set", self.interface, "peer", peer.public_key, "allowed-ips", peer.allowed_ip],
            check=True, timeout=10,
        )

    def remove_peer(self, public_key: str) -> None:
        self._runner(
            ["wg", "set", self.interface, "peer", public_key, "remove"],
            check=True, timeout=10,
        )
