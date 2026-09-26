"""WireGuard gateway -- own interface identity + config rendering (Phase C).

The gateway-side counterpart to appliance-agent's wireguard.py (device-
side keygen/render). Closes the one real gap found on resuming this
work: interface_provider.py's SystemWireGuardInterfaceProvider only
ever manages PEERS on an already-up wg0 (via `wg set`) -- nothing
anywhere brought the gateway's OWN interface up in the first place, the
same way appliance-agent's privileged_watcher.py DISPATCH entries do
for a device via `wg-quick up <rendered wg0.conf>`. This module renders
the same shape of config for the gateway side, for consistency with
the already-tested device-side format and the plan doc Sec 6/9.

`ensure_local_keypair()`/`main()` below are the ONLY new code this
module adds that may run for real -- and even they never call
`wg-quick` themselves (kept out of Python entirely, as an explicit,
auditable shell command the caller runs, matching privileged_watcher.py's
own DISPATCH argv-list transparency). They only ever: (1) generate a
keypair if none exists yet at the configured path (never overwrite one
that's already there -- a re-run of this module must never silently
rotate the gateway's own identity out from under every already-enrolled
appliance), (2) render and write a `wg-quick`-readable config file, and
(3) print the PUBLIC key only -- the private key is written directly to
its file with 0600 permissions and is never returned, logged, or
printed by anything in this module.
"""

from __future__ import annotations

import os
from pathlib import Path

from wireguard_remote import generate_keypair

DEFAULT_KEY_DIR = os.environ.get("ANYAICAM_WIREGUARD_GATEWAY_KEY_DIR", "/etc/anyaicam-wireguard-gateway")
DEFAULT_LISTEN_PORT = int(os.environ.get("ANYAICAM_WIREGUARD_GATEWAY_LISTEN_PORT", "51820"))
# .1 in TUNNEL_CIDR is reserved for the gateway itself -- see
# wireguard_remote.py's own _assign_tunnel_address() docstring. Kept as
# a plain literal here (not re-derived from TUNNEL_CIDR) because this
# module has no database access of its own and must not need any to
# render its own, single, well-known address.
DEFAULT_GATEWAY_ADDRESS = os.environ.get("ANYAICAM_WIREGUARD_GATEWAY_ADDRESS", "10.70.0.1")
DEFAULT_GATEWAY_PREFIX = os.environ.get("ANYAICAM_WIREGUARD_GATEWAY_PREFIX", "16")


def render_gateway_wg_conf(*, private_key: str, address: str, prefix: str, listen_port: int) -> str:
    """Pure function -- the exact text a real `wg-quick up` would read
    for the GATEWAY's own interface. Deliberately has NO [Peer] blocks:
    every appliance peer is added dynamically after the interface is up
    by reconciler.py's own reconcile() (via SystemWireGuardInterfaceProvider.
    add_peer()), which is how a peer set that changes over time (new
    enrollments, revocations, rotations) is meant to be managed for an
    interface that stays up continuously -- static [Peer] blocks in the
    base config would only reflect whatever was enrolled at the moment
    this file was rendered, immediately stale."""
    return (
        '[Interface]\n'
        f'PrivateKey = {private_key}\n'
        f'Address = {address}/{prefix}\n'
        f'ListenPort = {listen_port}\n'
    )


def save_gateway_wg_conf(path: Path, content: str) -> Path:
    """Same atomic write-then-rename + 0600 pattern as
    appliance-agent's wireguard.py:save_wg_conf() -- deliberately
    duplicated rather than shared, for the same cross-package
    independence reason documented in that module."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(content, encoding='utf-8')
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)
    return path


def ensure_local_keypair(key_dir: Path) -> tuple[str, str]:
    """Returns (private_key, public_key). If a private key already
    exists at key_dir/private.key, reuses it unconditionally -- a
    re-run of this function (a container restart, a re-deploy) must
    NEVER silently rotate the gateway's identity, since every
    already-enrolled appliance's own wg0.conf has the OLD public key
    baked in as its [Peer] PublicKey and would be unable to reach a
    gateway that rotated out from under it without a separate,
    deliberate rotation flow (out of scope for this pass). Only
    generates a fresh keypair when the file genuinely does not exist
    yet -- e.g. this gateway's very first real bring-up."""
    key_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_path = key_dir / 'private.key'
    public_path = key_dir / 'public.key'
    if private_path.exists():
        return private_path.read_text(encoding='utf-8').strip(), public_path.read_text(encoding='utf-8').strip()
    private_key, public_key = generate_keypair()
    temporary = private_path.with_suffix('.tmp')
    temporary.write_text(private_key, encoding='utf-8')
    os.chmod(temporary, 0o600)
    temporary.replace(private_path)
    os.chmod(private_path, 0o600)
    public_path.write_text(public_key, encoding='utf-8')
    os.chmod(public_path, 0o644)
    return private_key, public_key


def main() -> None:  # pragma: no cover - real process entry point, not exercised by tests
    """Idempotent prep step: ensure a local keypair exists, render and
    write the gateway's own wg0.conf next to it. Prints ONLY the public
    key -- never the private key, never the full config (which embeds
    the private key) -- so this command is safe to run under any
    logging/output-capturing tooling. Does NOT call `wg-quick` itself;
    that remains an explicit, separate, visible shell command."""
    key_dir = Path(DEFAULT_KEY_DIR)
    private_key, public_key = ensure_local_keypair(key_dir)
    conf_path = save_gateway_wg_conf(
        key_dir / 'wg0.conf',
        render_gateway_wg_conf(
            private_key=private_key, address=DEFAULT_GATEWAY_ADDRESS,
            prefix=DEFAULT_GATEWAY_PREFIX, listen_port=DEFAULT_LISTEN_PORT,
        ),
    )
    print(f"gateway public key: {public_key}")
    print(f"gateway config written: {conf_path}")


if __name__ == "__main__":  # pragma: no cover - real process entry point, not exercised by tests
    main()
