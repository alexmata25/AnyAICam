"""WireGuard direct remote-connectivity -- Phase A2 (cloud-side foundation).

See docs/wireguard-remote-connectivity-plan.md for the full architecture
audit and design this module implements the first slice of. Scope of
THIS module, deliberately: the data model's own logic (peer enrollment,
tenant-scoped lookup, revocation) and the one cloud-side route an
appliance calls to enroll a public key. It does NOT start a real
WireGuard interface anywhere, does not touch the appliance-agent package,
and does not add a portal-facing UI -- those are later phases (the plan's
Sec 20, Phase B/C), each requiring their own separate authorization and
check-in.

Key handling (the one property this module is most load-bearing for):
**a WireGuard private key is generated on the device and never crosses
this module's own request/response contract in either direction.**
`generate_keypair()` below exists only so this module's own tests (and,
later, the appliance-agent package once Phase B builds it) have one
shared, correct implementation of WireGuard's own key format -- it is
never called by any route in this module, and this module's database
schema (appliance_wireguard_peers, see db_migrations.py) has no column
that could hold a private key even if a caller mistakenly tried to send
one; enroll_peer() below only ever reads a `public_key` field out of an
incoming payload, and never persists any other field from it.

Authorization model: enrollment reuses appliance_cloud.authenticate_
appliance() unchanged -- the same bearer+nonce+timestamp channel every
other appliance route in this codebase already uses (see this module's
own docstring precedent audit in the plan doc, Sec 2d). No new appliance
identity or credential system exists here. A WireGuard peer row is
always scoped to the authenticated appliance's own id and customer_id,
read from the `appliances` row authenticate_appliance() already resolved
-- never accepted from the request body, so a compromised or malicious
payload can never enroll a peer against a different appliance's identity.
"""
from __future__ import annotations

import base64
import ipaddress
import os
import uuid
from datetime import datetime

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI, HTTPException, Request

from appliance_cloud import authenticate_appliance
from partner_db import connection

# Configurable, matching this codebase's established env-var-with-a-safe-
# default convention (e.g. ANYAICAM_LIVE_STUN_SERVERS) rather than a
# hardcoded literal. See the plan doc Sec 9 for why this specific /16 was
# chosen (outside every common home-LAN default range).
TUNNEL_CIDR = os.environ.get("ANYAICAM_WIREGUARD_TUNNEL_CIDR", "10.70.0.0/16").strip()
# The gateway's own identity/endpoint -- unset by default, matching this
# codebase's established fail-closed-when-unconfigured convention (e.g.
# appliance_cloud.py's RECORDING_UPLOAD_ROLE_ARN/RECORDING_S3_BUCKET: "If
# none of these three are configured, recording_upload_credentials() below
# still fails closed with 503 regardless of which flag authorized the
# request"). Real values are cloud-infrastructure configuration, out of
# scope for this pass -- see the plan doc Sec 19's open infrastructure
# item.
GATEWAY_PUBLIC_KEY = os.environ.get("ANYAICAM_WIREGUARD_GATEWAY_PUBLIC_KEY", "").strip()
GATEWAY_ENDPOINT = os.environ.get("ANYAICAM_WIREGUARD_GATEWAY_ENDPOINT", "").strip()


def generate_keypair() -> tuple[str, str]:
    """Returns (private_key_b64, public_key_b64) in WireGuard's own
    standard format -- a raw 32-byte Curve25519 key, base64-encoded
    (the same format `wg genkey`/`wg pubkey` produce), so a value this
    function returns is directly usable in a real `wg` config once
    Phase B exists, with no re-encoding. Never called by any route in
    this module -- see module docstring. Exists here (not yet in the
    appliance-agent package, which has no cryptography dependency of
    its own today) purely so this module's tests exercise the exact
    real key format enroll_peer() must validate against, rather than a
    hand-typed fixture that might not match what a real device
    produces."""
    private_key = X25519PrivateKey.generate()
    private_bytes = private_key.private_bytes_raw()
    public_bytes = private_key.public_key().public_bytes_raw()
    return base64.b64encode(private_bytes).decode("ascii"), base64.b64encode(public_bytes).decode("ascii")


def is_valid_wireguard_public_key(value) -> bool:
    """A WireGuard public key is always exactly 32 raw bytes, standard
    base64-encoded (44 characters including one trailing '=' pad).
    Deliberately strict -- this is the one field this module ever
    persists from an appliance-submitted payload, so it is validated
    against the real format, not merely checked non-empty."""
    if not isinstance(value, str) or len(value) != 44:
        return False
    try:
        decoded = base64.b64decode(value, validate=True)
    except Exception:
        return False
    return len(decoded) == 32


def _assign_tunnel_address(db) -> str:
    """Picks the lowest not-currently-active address in TUNNEL_CIDR,
    skipping the network address (reserved, unassignable) and .1 (the
    gateway itself -- see the plan doc Sec 9). A revoked peer's old
    address is free to be reassigned (the partial unique index in
    db_migrations.py only constrains active rows), so this always
    re-checks the live set rather than tracking a monotonic counter,
    keeping the address space compact rather than growing unbounded
    across churn."""
    network = ipaddress.ip_network(TUNNEL_CIDR, strict=False)
    taken = {
        row["tunnel_address"]
        for row in db.execute("SELECT tunnel_address FROM appliance_wireguard_peers WHERE revoked_at IS NULL").fetchall()
    }
    reserved = {str(network.network_address), str(network.network_address + 1)}
    for address in network.hosts():
        text = str(address)
        if text in reserved or text in taken:
            continue
        return text
    raise HTTPException(status_code=503, detail="No WireGuard tunnel addresses remain available.")


def enroll_peer(db, *, appliance_id: str, customer_id: str, public_key: str, now: datetime) -> dict:
    """Core enrollment logic, independent of the HTTP route below so
    it's directly unit-testable. Returns the full row dict for the
    newly created peer. Deliberately allows more than one active row
    per appliance (mirrors appliance_credentials' own multi-row
    rotation shape -- see the plan doc Sec 11): a second enrollment
    call for an appliance that already has one is a normal rotation,
    never rejected or silently overwritten. Re-submitting the exact
    same public_key (a reconnect/retry, not a rotation) is idempotent
    -- returns the existing row rather than violating the public_key
    unique index or creating a duplicate."""
    existing = db.execute(
        "SELECT * FROM appliance_wireguard_peers WHERE public_key=? AND revoked_at IS NULL",
        (public_key,),
    ).fetchone()
    if existing:
        if existing["appliance_id"] != appliance_id:
            # A public key is only ever generated on one device (see
            # generate_keypair()'s own real-CSPRNG randomness) -- a
            # collision against a DIFFERENT appliance's active key is
            # not a legitimate reconnect under any real scenario, and
            # must never silently attribute one appliance's tunnel
            # identity to another.
            raise HTTPException(status_code=409, detail="This public key is already enrolled to a different appliance.")
        return dict(existing)

    tunnel_address = _assign_tunnel_address(db)
    peer_id = uuid.uuid4().hex[:16]
    db.execute(
        "INSERT INTO appliance_wireguard_peers(id,appliance_id,customer_id,public_key,tunnel_address,"
        "gateway_public_key,gateway_endpoint,status,last_handshake_at,created_at,revoked_at,revoked_reason) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            peer_id, appliance_id, customer_id, public_key, tunnel_address,
            GATEWAY_PUBLIC_KEY, GATEWAY_ENDPOINT, "enrolled", None, now.isoformat(), None, None,
        ),
    )
    return dict(db.execute("SELECT * FROM appliance_wireguard_peers WHERE id=?", (peer_id,)).fetchone())


def revoke_peer(db, *, peer_id: str, reason: str, now: datetime) -> bool:
    """Marks one peer row revoked -- never deletes it, matching this
    project's own never-rewrite-history convention (see the plan doc
    Sec 11). Returns False (a no-op, not an error) for an unknown id
    or an already-revoked row, matching this codebase's established
    idempotent-duplicate-action convention (e.g. live_view_sessions.
    stop_live_view()'s own already-terminal no-op). The caller is
    responsible for also removing this peer from the live gateway
    WireGuard config (Phase B) -- this function only ever updates the
    database's own record of what SHOULD be enrolled."""
    result = db.execute(
        "UPDATE appliance_wireguard_peers SET status='revoked',revoked_at=?,revoked_reason=? "
        "WHERE id=? AND revoked_at IS NULL",
        (now.isoformat(), reason, peer_id),
    )
    return result.rowcount > 0


def revoke_all_peers_for_appliance(db, *, appliance_id: str, reason: str, now: datetime) -> int:
    """Bulk revoke, for the hardware-replacement case (plan doc Sec 12):
    a re-enrollment event should invalidate every previously-active
    peer for this appliance_id before/alongside enrolling the
    replacement's own fresh keypair. Returns the number of rows
    actually revoked (0 is a normal, valid outcome for an appliance
    that never enrolled a WireGuard peer at all)."""
    result = db.execute(
        "UPDATE appliance_wireguard_peers SET status='revoked',revoked_at=?,revoked_reason=? "
        "WHERE appliance_id=? AND revoked_at IS NULL",
        (now.isoformat(), reason, appliance_id),
    )
    return result.rowcount


def active_peers_for_appliance(db, appliance_id: str) -> list[dict]:
    return [
        dict(item) for item in db.execute(
            "SELECT * FROM appliance_wireguard_peers WHERE appliance_id=? AND revoked_at IS NULL ORDER BY created_at",
            (appliance_id,),
        ).fetchall()
    ]


def register_wireguard_remote_appliance_routes(app: FastAPI) -> None:
    @app.post("/api/appliance/wireguard/enroll")
    def wireguard_enroll(request: Request, payload: dict) -> dict:
        appliance = authenticate_appliance(request)
        if not GATEWAY_PUBLIC_KEY or not GATEWAY_ENDPOINT:
            # Fail closed exactly like recording_upload_credentials()'s own
            # unconfigured-infrastructure case (appliance_cloud.py) -- an
            # appliance that calls this before the gateway itself has been
            # provisioned gets a clear, retryable 503, never a crash or a
            # half-populated peer row.
            raise HTTPException(status_code=503, detail="WireGuard gateway is not configured on this environment.")
        public_key = payload.get("public_key")
        if not is_valid_wireguard_public_key(public_key):
            raise HTTPException(status_code=400, detail="public_key must be a valid WireGuard public key.")
        # Deliberately reads ONLY public_key (and, below, the boolean
        # replace_existing) out of the payload -- any other field (a
        # stray "private_key", "tunnel_address", etc.) is silently
        # ignored, never persisted, matching enroll_peer()'s own narrow
        # contract (see module docstring). This is a second, independent
        # layer of defense against a private key ever reaching this
        # database, on top of the schema itself having no column that
        # could hold one.
        #
        # replace_existing (plan doc Sec 12): set by the appliance-agent
        # only when this enroll call is part of a coordinated_reenroll()
        # (hardware replacement / re-claim under the same appliance_id,
        # per reenrollment.py) -- never by a routine reconnect. Revokes
        # every OTHER currently-active peer for this appliance, so a
        # replaced device's stale tunnel identity never lingers as a
        # second live peer the gateway would otherwise keep accepting
        # traffic from.
        #
        # Order matters here: enroll_peer() runs FIRST, revocation
        # second, deliberately -- the public_key unique index is global,
        # not scoped to active rows (see db_migrations.py), so revoking
        # the caller's OWN previous row before enroll_peer() re-checks
        # for an existing active row would, on the narrow edge case of a
        # client resubmitting an already-revoked key, make enroll_peer()
        # try to INSERT a second row with a public_key value that
        # already exists (now revoked) and hit that unique index instead
        # of hitting its own intended "different appliance" 409 check.
        # Enrolling first means enroll_peer()'s existing idempotency
        # check (WHERE public_key=? AND revoked_at IS NULL) always sees
        # accurate state, and the revocation below explicitly excludes
        # the row just enrolled/confirmed so it can never revoke the
        # peer it was just asked to keep.
        replace_existing = bool(payload.get("replace_existing"))
        now = datetime.now()
        with connection() as db:
            peer = enroll_peer(
                db, appliance_id=appliance["id"], customer_id=appliance["customer_id"],
                public_key=public_key, now=now,
            )
            if replace_existing:
                db.execute(
                    "UPDATE appliance_wireguard_peers SET status='revoked',revoked_at=?,revoked_reason=? "
                    "WHERE appliance_id=? AND revoked_at IS NULL AND id!=?",
                    (now.isoformat(), "reenrolled", appliance["id"], peer["id"]),
                )
        return {
            "tunnel_address": peer["tunnel_address"],
            "gateway_public_key": peer["gateway_public_key"],
            "gateway_endpoint": peer["gateway_endpoint"],
            "status": peer["status"],
        }
