"""WireGuard gateway -- peer reconciliation (Phase B).

Reconciles the database's own set of active `appliance_wireguard_peers`
rows (the authoritative record of who SHOULD be a peer -- see
wireguard_remote.py) against a WireGuardInterfaceProvider's live peer
set (who the interface currently thinks IS a peer), and drives the
provider to match. Pure diff logic, fully unit-testable with
MockWireGuardInterfaceProvider and no real interface anywhere.

This is also where revocation (plan doc Sec 11) actually takes effect
on the gateway side: revoke_peer()/the enroll route's own
replace_existing path only ever update the database (a peer's
revoked_at being set means it simply stops appearing in the "desired"
set the next time this module runs) -- reconcile() is what turns that
into a real `wg set ... remove` call. Until Phase C/D ever runs this
for real against a live interface, revocation is fully correct and
tested at the database layer (see test_wireguard_remote.py) but has no
live-interface effect, which is expected and safe: there is no live
interface for it to affect.
"""

from __future__ import annotations

from dataclasses import dataclass

from .interface_provider import GatewayPeer, WireGuardInterfaceProvider


@dataclass(frozen=True)
class ReconcileResult:
    added: list[str]
    removed: list[str]


def desired_peers_from_rows(rows: list[dict]) -> dict[str, str]:
    """rows: appliance_wireguard_peers rows already filtered to
    revoked_at IS NULL by the caller (see gateway_service.py's own
    _active_peer_rows()). Maps public_key -> "<tunnel_address>/32" --
    the same single-appliance /32 scoping the plan doc Sec 9 requires
    structurally, derived directly from each row's own tunnel_address
    column rather than re-parsed from anywhere else."""
    return {row["public_key"]: f"{row['tunnel_address']}/32" for row in rows}


def reconcile(desired: dict[str, str], provider: WireGuardInterfaceProvider) -> ReconcileResult:
    """Adds any desired peer missing (or present with the wrong
    allowed_ip -- treated as add, which overwrites) from the live
    interface, and removes any live peer no longer desired (revoked, or
    never enrolled at all). Order is add-then-remove so a peer that is
    simultaneously being re-added under a rotated key (plan doc Sec 11:
    a brief overlap window during rotation is normal) is never left
    without connectivity even for the instant between the two calls."""
    current = provider.current_peers()
    added: list[str] = []
    for public_key, allowed_ip in desired.items():
        if current.get(public_key) != allowed_ip:
            provider.add_peer(GatewayPeer(public_key=public_key, allowed_ip=allowed_ip))
            added.append(public_key)
    removed: list[str] = []
    for public_key in current:
        if public_key not in desired:
            provider.remove_peer(public_key)
            removed.append(public_key)
    return ReconcileResult(added=added, removed=removed)
