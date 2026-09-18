"""Appliance-side WireGuard identity + config rendering (Phase B).

See docs/wireguard-remote-connectivity-plan.md for the full design;
this module implements the device-side half of Sec 5 (enrollment) and
Sec 6 (key generation/storage). The cloud-side half is
app/wireguard_remote.py -- the two are independent implementations of
the same key FORMAT (X25519, base64), duplicated rather than shared
because this package (anyaicam_agent) and the app/ package are
separate deployables with no dependency on each other (see this
repo's own appliance-agent/pyproject.toml) and must stay that way.

Nothing in this module is called by anything in this codebase's normal
runtime path yet -- see setup_wizard.py's own ANYAICAM_WIREGUARD_ENABLED
gate, which is unset (feature off) by default. Every function here is
directly unit-tested regardless (see tests/test_wireguard_agent.py),
matching this codebase's own "correct and tested before it is ever
turned on" precedent (relay_control.py's own ANYAICAM_FACIAL_ACCESS_
CONTROL_ENABLED flag is the closest analogue).
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .config import load_wireguard_identity, save_wireguard_identity

DEFAULT_PERSISTENT_KEEPALIVE_SECONDS = 25


def generate_keypair() -> tuple[str, str]:
    """Returns (private_key_b64, public_key_b64) -- WireGuard's own
    standard format (raw 32-byte Curve25519 key, base64-encoded),
    identical in shape to app/wireguard_remote.py's own
    generate_keypair(). Generated locally on this device; the private
    key this returns is never transmitted anywhere -- see
    enroll_wireguard() below, which submits only the public half."""
    private_key = X25519PrivateKey.generate()
    private_bytes = private_key.private_bytes_raw()
    public_bytes = private_key.public_key().public_bytes_raw()
    return base64.b64encode(private_bytes).decode('ascii'), base64.b64encode(public_bytes).decode('ascii')


def render_wg_conf(*, private_key: str, tunnel_address: str, gateway_public_key: str, gateway_endpoint: str,
                    persistent_keepalive: int = DEFAULT_PERSISTENT_KEEPALIVE_SECONDS) -> str:
    """Pure function -- the exact text a real `wg-quick up` would read.
    AllowedIPs = 0.0.0.0/0 in the [Peer] block means "route all of this
    interface's own traffic through the tunnel when it's up", standard
    for a single-upstream-peer client config; this does NOT affect the
    appliance's real LAN-facing interface or its normal internet route
    at all -- routing scope is confined to WHATEVER traffic is
    explicitly directed at the wg0 interface itself (see the plan doc
    Sec 9's own "own virtual NIC, own address space" note), never a
    system-wide route change. PersistentKeepalive keeps the NAT mapping
    alive (plan doc Sec 8) -- a standard WireGuard client setting, not
    custom logic."""
    return (
        '[Interface]\n'
        f'PrivateKey = {private_key}\n'
        f'Address = {tunnel_address}/32\n'
        '\n'
        '[Peer]\n'
        f'PublicKey = {gateway_public_key}\n'
        f'Endpoint = {gateway_endpoint}\n'
        'AllowedIPs = 0.0.0.0/0\n'
        f'PersistentKeepalive = {persistent_keepalive}\n'
    )


def save_wg_conf(config, content: str):
    """Same atomic write-then-rename + 0600 pattern as every other
    identity/config file in this package. Returns the path written to."""
    path = config.wireguard_conf_file
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(content, encoding='utf-8')
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)
    return path


def enroll_wireguard(config, portal_client, *, replace_existing: bool = False) -> dict:
    """Ensures a local keypair exists (generating one only if none is
    already saved, or if replace_existing explicitly asks for a fresh
    one -- a routine reconnect must never silently discard and replace
    an existing private key), submits the public key to the cloud
    enroll route via portal_client (a PortalClient -- see portal.py's
    own wireguard_enroll() method, which reuses the existing
    authenticated bearer channel unchanged), and on success writes both
    the identity file and the rendered wg0.conf. Returns the saved
    identity dict.

    Deliberately does NOT queue the wireguard_interface_up privileged
    action itself -- the caller (setup_wizard.py's _finish_enrollment())
    does that separately, exactly mirroring how restart_vms is queued
    as a distinct step after (not inside) the identity-commit logic in
    reenrollment.py. Raises whatever portal_client.wireguard_enroll()
    raises (PortalError) on any failure -- the caller is responsible
    for treating that as non-fatal to the overall activation, matching
    restart_service()'s own established "failure here is a warning,
    never fatal" precedent, since WireGuard is fully additive (plan doc
    Sec 18: existing paths must keep working regardless)."""
    existing = load_wireguard_identity(config)
    if existing and not replace_existing:
        private_key = existing['private_key']
        public_key = existing['public_key']
    else:
        private_key, public_key = generate_keypair()
    response = portal_client.wireguard_enroll(public_key, replace_existing=replace_existing)
    identity = {
        'private_key': private_key,
        'public_key': public_key,
        'tunnel_address': response['tunnel_address'],
        'gateway_public_key': response['gateway_public_key'],
        'gateway_endpoint': response['gateway_endpoint'],
        'status': response['status'],
    }
    save_wireguard_identity(config, identity)
    save_wg_conf(config, render_wg_conf(
        private_key=private_key, tunnel_address=identity['tunnel_address'],
        gateway_public_key=identity['gateway_public_key'], gateway_endpoint=identity['gateway_endpoint'],
    ))
    return identity
