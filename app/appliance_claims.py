"""Phase 1 of the non-interactive appliance activation system (see
docs/non-interactive-activation-design.md,
docs/non-interactive-activation-state-machine.md, and
docs/non-interactive-activation-phase1-plan.md at commit
899e8d0031e67a4a7f046e37ba7837b56a821cb7 for the full design this
module implements). This is the QR/pull-based, self-service claim
flow -- a device with no admin-pre-created row, no pre-assigned
customer/site, and no credential yet can register itself, wait for a
customer to confirm it in the portal, and redeem a one-time proof for
a permanent credential.

This commit adds claim/complete, the one-time exchange of a confirmed
claim into the existing enrollment/credential mechanism -- completing
the cloud-side half of the Phase 1 plan doc's flow (agent-side
PortalClient methods and their tests land in the next commit; see the
Phase 1 plan doc's 6-commit implementation order and the Phase 1
completion report for the full picture).

Portal-facing endpoints (claim lookup, claim confirm) reuse the exact
identity/permission pattern partner_workspace.py's existing
POST /api/customer/appliances/link already established for "a customer
attaches an appliance to their own account": partner_identity() +
role=='customer_owner' + require_permission(identity,
'appliance.self.link'). No new permission was added -- this is the
same customer action, just reached over a different transport.

Claim-code lookup tradeoff (see Phase 1 plan §8, and re-verified here
against the real code): password hashing in this codebase
(partner_db.verify_password) already does a constant-time digest
compare via hmac.compare_digest, so a single comparison is not a
timing oracle. What IS a deliberate, documented tradeoff is that
_find_claim_by_code() below never does an indexed exact-match lookup
by code -- claim codes are never stored in a form that would allow one
-- so it scans every still-pending, unexpired row and compares hashes
one at a time, returning on the first match or after exhausting the
list. This means the *number of rows scanned* (and therefore wall-clock
time) leaks a coarse signal correlated with "how many claims are
currently pending", not with whether any specific guess was close to a
real code, and returning on first match means a match takes less time
on average than a full scan -- an intentionally bounded, low-volume
tradeoff, not a claim of constant-time behavior. Acceptable at Phase 1
volumes (tens of concurrently pending claims); would need a keyed-HMAC
indexed column if that ever changed.

Deliberately independent of the existing, unmodified admin-driven
paths in appliance_cloud.py (POST /api/appliance/activate, which
requires an appliances row and a pre-minted activation token) and
partner_workspace.py (POST /api/customer/appliances/link, which also
requires a pre-created appliances row scoped to the customer). Both
keep working exactly as before; this module never reads from or
writes to appliance_activation_tokens, and appliance_claims.py's own
new table is the only thing it touches until the one-time INSERT into
the existing appliances table at claim/complete.

Why a new table instead of relaxing appliances.customer_id/site_id's
NOT NULL constraints: see the Phase 1 plan doc §1. In short, a
pre-claim device cannot be represented in appliances (customer_id and
site_id are NOT NULL there, and appliance_activation_tokens is keyed
on an existing appliance_id), so all pre-claim and claim-in-progress
state lives in appliance_claims (this module) instead, and appliances
is only ever INSERTed once a claim actually completes.

Trust model for the three device-facing endpoints (claim/begin,
claim/status, claim/complete): the device has no credential yet, by
definition, so these are protected the same way
appliance_cloud.activate_appliance() already protects its own
unauthenticated cloud_id+token exchange -- rate limiting, unguessable
high-entropy random tokens, and short expiries -- not a bearer
credential. claim_session_id and claim_proof are both treated as
bearer-equivalent secrets: never logged, and matched with
verify_password()'s constant-time comparison exactly like every other
hashed secret in this codebase. This is also why claim_session_id and
claim_code/claim_proof are always carried in a POST body, never a URL
path or query string -- a URL is what every access log line at every
layer (this process, any reverse
proxy, a CDN) is built from, and this repo already has one documented
instance of exactly that mistake for password-reset tokens (see
docs/customer-appliance-readiness-blockers.md). See the Phase 1
completion report for the full explanation of this deviation from the
plan doc's originally path/query-based endpoint shapes.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request

from appliance_cloud import activation_limiter
from appliance_protocol import RateLimiter, decrypt_claim_flow_secret, encrypt_claim_flow_secret
from partner_db import audit, connection, password_hash, require_permission, row, rows, verify_password
from partner_portal import partner_identity

logger = logging.getLogger('anyaicam.appliance_claims')

# Same shape/window as appliance_cloud.activation_limiter -- this flow
# is the self-service sibling of that same activation surface and
# deserves the same throttling posture, not a stricter or looser one
# invented from scratch. claim/complete reuses activation_limiter
# itself (the exact same instance appliance_cloud.activate_appliance()
# already throttles with), since both are, from an abuse-budget
# perspective, "an unauthenticated attempt to redeem a device
# credential from this IP" -- one shared budget, not two independent
# ones an attacker could exhaust separately.
claim_begin_limiter = RateLimiter(10, 300)
claim_status_limiter = RateLimiter(120, 60)
claim_portal_limiter = RateLimiter(30, 60)

CLAIM_SESSION_TTL_MINUTES = 15
CLAIM_PROOF_TTL_MINUTES = 5
# Security-hardening checkpoint (see docs/non-interactive-activation-
# phase1-security-hardening-report.md): the original pattern here --
# any 8-128 char alphanumeric string -- accepted anything, including a
# short, sequential, attacker-guessable device_id. Since claim_begin
# hands the claim_code for a not-yet-provisioned device_id directly to
# whoever calls it first, with no authentication at all, a guessable
# device_id let an unauthenticated attacker open and complete a claim
# for someone else's real appliance before its rightful owner ever
# activated it -- confirmed as a working end-to-end exploit during the
# audit this hardening pass closes.
#
# installer/09-identity.sh is the ONLY place in this repository that
# generates an appliance-local identifier intended for exactly this
# purpose: `appliance_id="$(cat /proc/sys/kernel/random/uuid)"`. Per
# the Linux kernel's own contract for that interface, this is always a
# random (version 4) UUID in canonical lowercase form -- so requiring
# a proper UUIDv4 here matches the only real identifier this
# repository's own installer ever produces, not an arbitrary new
# restriction. This does NOT touch the existing admin-assigned
# `cloud_id` format ("AIC-XXXXXXXX"-style codes) used by the
# unmodified /api/appliance/activate and appliance_cloud.py -- that is
# a completely separate field, validated nowhere near this module, and
# this pattern only ever gates the NEW self-service claim flow's
# device_id parameter.
#
# Version nibble (3rd group, 1st hex digit) must be '4'; variant
# nibble (4th group, 1st hex digit) must be one of 8/9/a/b per RFC
# 4122 -- this is what actually distinguishes UUIDv4 from UUIDv1/v3/v5
# (which share the same 8-4-4-4-12 shape but a different version
# nibble) and from a random string that merely happens to look
# hex-and-hyphen-shaped.
DEVICE_ID_PATTERN = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$')


def _now() -> datetime:
    return datetime.now()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else 'unknown'


def _customer_owner(request: Request) -> dict:
    """Identical check to partner_workspace.py's own private
    customer_owner() closure -- duplicated here rather than imported
    because that helper is a nested closure inside
    register_partner_workspace_routes(), not a module-level export.
    Kept intentionally tiny and identical so it can't drift."""
    identity = partner_identity(request)
    if not identity or identity.get('role') != 'customer_owner':
        raise HTTPException(status_code=403, detail='Customer owner permission required.')
    return identity


def _require_self_link_permission(identity: dict) -> None:
    try:
        require_permission(identity, 'appliance.self.link')
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


def _valid_device_id(device_id: str) -> bool:
    return bool(DEVICE_ID_PATTERN.match(device_id or ''))


def _normalize_device_id(device_id: str) -> str:
    # Accepts either casing (the kernel's uuid interface always emits
    # lowercase, but nothing stops a caller from upper-casing it in
    # transit) and stores/looks up one canonical lowercase form so two
    # requests for "the same" device_id in different casing are always
    # treated as the same device -- validated by _valid_device_id()
    # before this is ever called.
    return device_id.lower()


def _generate_claim_code() -> str:
    # 8 uppercase hex characters (32 bits) -- short enough to type from
    # a small display, long enough combined with claim_begin_limiter's
    # rate limit and the 15-minute expiry that brute-forcing a specific
    # live code through the portal lookup endpoint (added in a later
    # commit) is not practical.
    return secrets.token_hex(4).upper()


def _find_pending_claim_for_device(device_id: str) -> dict | None:
    now_text = _now().isoformat()
    candidates = rows(
        "SELECT * FROM appliance_claims WHERE device_id=? AND status='pending' AND expires_at>? ORDER BY created_at DESC",
        (device_id, now_text),
    )
    return candidates[0] if candidates else None


def _find_claim_by_code(claim_code: str) -> dict | None:
    """Bounded linear scan over pending, unexpired claims -- see this
    module's docstring for why an indexed exact lookup is deliberately
    not used here."""
    now_text = _now().isoformat()
    for candidate in rows("SELECT * FROM appliance_claims WHERE status='pending' AND expires_at>?", (now_text,)):
        if verify_password(claim_code, candidate['claim_code_hash']):
            return candidate
    return None


def _still_valid(timestamp: str | None) -> bool:
    if not timestamp:
        return False
    try:
        return datetime.fromisoformat(timestamp) > _now()
    except (TypeError, ValueError):
        return False


def _expire_if_due(claim: dict) -> dict:
    """Lazy expiry: flips a stale row to 'expired' on the next read
    that touches it, rather than running a separate sweep job (see
    Phase 1 plan §8). Never mutates a row already in a terminal state
    (completed/expired/revoked).

    Handles two distinct expiries, security-hardening checkpoint
    addition for the second one:
      - 'pending' past expires_at -- the original Phase 1 behavior:
        nothing sensitive to clear here (claim_code_hash is a hash,
        not a recoverable secret).
      - 'claimed' past proof_expires_at -- an abandoned claim whose
        confirmed-but-never-redeemed proof would otherwise sit
        encrypted-but-live in the database indefinitely. Transitioning
        it to 'expired' AND nulling claim_proof_encrypted here is what
        actually bounds that secret's at-rest lifetime; leaving the
        row at 'claimed' forever (the original Phase 1 behavior) meant
        the API correctly stopped *serving* the proof once its TTL
        passed, but the database still held a live, recoverable copy
        of it forever. claim_proof_hash is left alone even here (it is
        one-way and needed for audit-trail purposes, matching
        appliance_activation_tokens's own forever-retention of used/
        expired token hashes)."""
    if claim['status'] == 'pending':
        if _still_valid(claim['expires_at']):
            return claim
        with connection() as db:
            db.execute("UPDATE appliance_claims SET status='expired' WHERE id=? AND status='pending'", (claim['id'],))
        claim = dict(claim)
        claim['status'] = 'expired'
        return claim
    if claim['status'] == 'claimed':
        if _still_valid(claim.get('proof_expires_at')):
            return claim
        with connection() as db:
            db.execute(
                "UPDATE appliance_claims SET status='expired',claim_proof_encrypted=NULL,claim_proof_plaintext=NULL WHERE id=? AND status='claimed'",
                (claim['id'],),
            )
        claim = dict(claim)
        claim['status'] = 'expired'
        claim['claim_proof_encrypted'] = None
        claim['claim_proof_plaintext'] = None
        return claim
    return claim


def register_appliance_claim_routes(app: FastAPI) -> None:
    @app.post('/api/appliance/claim/begin')
    def claim_begin(request: Request, payload: dict) -> dict:
        client = _client_ip(request)
        if not claim_begin_limiter.allow(client):
            raise HTTPException(status_code=429, detail='Claim attempt rate exceeded.')
        device_id = str(payload.get('device_id', '')).strip()
        if not _valid_device_id(device_id):
            raise HTTPException(status_code=400, detail='device_id must be a valid UUIDv4.')
        device_id = _normalize_device_id(device_id)
        existing_appliance = row('SELECT id FROM appliances WHERE cloud_id=?', (device_id.upper(),))
        if existing_appliance:
            raise HTTPException(status_code=409, detail='This device is already provisioned. Use the existing activation flow.')
        resumable = _find_pending_claim_for_device(device_id)
        if resumable:
            return {
                'claim_session_id': resumable['claim_session_id'],
                'claim_code': None,
                'expires_at': resumable['expires_at'],
                'poll_interval_seconds': 5,
                'resumed': True,
            }
        claim_id = secrets.token_hex(16)
        claim_session_id = secrets.token_urlsafe(24)
        claim_code = _generate_claim_code()
        now = _now()
        expires_at = (now + timedelta(minutes=CLAIM_SESSION_TTL_MINUTES)).isoformat()
        with connection() as db:
            db.execute(
                'INSERT INTO appliance_claims(id,device_id,claim_session_id,claim_code_hash,status,expires_at,created_at) VALUES(?,?,?,?,?,?,?)',
                (claim_id, device_id, claim_session_id, password_hash(claim_code), 'pending', expires_at, now.isoformat()),
            )
        logger.info('Claim session opened device_id=%s claim_session_id=%s', device_id, claim_session_id)
        return {
            'claim_session_id': claim_session_id,
            'claim_code': claim_code,
            'expires_at': expires_at,
            'poll_interval_seconds': 5,
            'resumed': False,
        }

    @app.post('/api/appliance/claim/status')
    def claim_status(request: Request, payload: dict) -> dict:
        client = _client_ip(request)
        if not claim_status_limiter.allow(client):
            raise HTTPException(status_code=429, detail='Claim status poll rate exceeded.')
        claim_session_id = str(payload.get('claim_session_id', '')).strip()
        if not claim_session_id:
            raise HTTPException(status_code=400, detail='claim_session_id is required.')
        claim = row('SELECT * FROM appliance_claims WHERE claim_session_id=?', (claim_session_id,))
        if not claim:
            # Same generic response as an expired/unknown session --
            # never lets a caller distinguish "wrong id" from "expired"
            # (avoids a session-id enumeration oracle).
            return {'status': 'expired'}
        claim = _expire_if_due(claim)
        response = {'status': claim['status']}
        if claim['status'] == 'claimed' and claim.get('claim_proof_encrypted') and _still_valid(claim.get('proof_expires_at')):
            # Returned on every poll while still valid, never marked
            # "issued" at read time -- consumption happens only at
            # claim/complete, which is what the "idempotent retry"
            # requirement actually depends on. Decrypted from
            # claim_proof_encrypted (security-hardening checkpoint --
            # the original Phase 1 implementation stored this raw in
            # claim_proof_plaintext; see appliance_protocol.
            # decrypt_claim_flow_secret()'s own docstring for why this
            # column is now encrypted instead).
            decrypted = decrypt_claim_flow_secret(claim['claim_proof_encrypted'])
            if decrypted:
                response['claim_proof'] = decrypted
        return response

    # claim_code is a bearer-equivalent secret (see this module's
    # docstring), so -- unlike a normal resource identifier -- it must
    # never appear in a URL: the request path (and query string, just
    # as much) is what every access log line is built from, at every
    # layer between the browser and this process (uvicorn, any reverse
    # proxy, a CDN) regardless of anything this application does, and
    # is what ends up in browser history. This repo already has one
    # documented instance of exactly this mistake (password-reset
    # tokens logged via GET query strings, see
    # docs/customer-appliance-readiness-blockers.md) -- both portal
    # routes below take claim_code in the POST body specifically to
    # avoid repeating it. This is a deviation from the path-based
    # `GET/POST /api/portal/claims/{claim_code}[/confirm]` shape in the
    # Phase 1 plan doc, caught by this module's own automated secret-
    # hygiene test; see the Phase 1 completion report for the full
    # explanation.
    @app.post('/api/portal/claims/lookup')
    def portal_claim_lookup(request: Request, payload: dict) -> dict:
        identity = _customer_owner(request)
        _require_self_link_permission(identity)
        if not claim_portal_limiter.allow(identity.get('email', identity.get('id', 'unknown'))):
            raise HTTPException(status_code=429, detail='Claim lookup rate exceeded.')
        claim_code = str(payload.get('claim_code', '')).strip()
        claim = _find_claim_by_code(claim_code)
        if not claim:
            raise HTTPException(status_code=404, detail='Claim code not found or expired.')
        return {'device_id': claim['device_id'], 'expires_at': claim['expires_at']}

    @app.post('/api/portal/claims/confirm')
    def portal_claim_confirm(request: Request, payload: dict) -> dict:
        identity = _customer_owner(request)
        _require_self_link_permission(identity)
        if not claim_portal_limiter.allow(identity.get('email', identity.get('id', 'unknown'))):
            raise HTTPException(status_code=429, detail='Claim confirmation rate exceeded.')
        claim_code = str(payload.get('claim_code', '')).strip()
        site_id = str(payload.get('site_id', '')).strip()
        if not claim_code:
            raise HTTPException(status_code=400, detail='claim_code is required.')
        if not site_id:
            raise HTTPException(status_code=400, detail='site_id is required.')
        claim = _find_claim_by_code(claim_code)
        if not claim:
            raise HTTPException(status_code=404, detail='Claim code not found or expired.')
        site = row('SELECT id FROM sites WHERE id=? AND customer_id=?', (site_id, identity['customer_id']))
        if not site:
            raise HTTPException(status_code=403, detail='That site does not belong to your account.')
        claim_proof = secrets.token_urlsafe(32)
        # Security-hardening checkpoint: encrypt the proof before it
        # ever touches the database, rather than storing it raw in
        # claim_proof_plaintext (the original Phase 1 column, still
        # present in the schema but never written by this code again).
        # Fails closed -- if ANYAICAM_CLAIM_FLOW_SECRET_KEY isn't
        # configured, this refuses to confirm the claim at all rather
        # than silently falling back to plaintext storage.
        encrypted_proof = encrypt_claim_flow_secret(claim_proof)
        if not encrypted_proof:
            raise HTTPException(status_code=503, detail='Claim confirmation is temporarily unavailable.')
        now = _now()
        proof_expires_at = (now + timedelta(minutes=CLAIM_PROOF_TTL_MINUTES)).isoformat()
        with connection() as db:
            changed = db.execute(
                "UPDATE appliance_claims SET status='claimed',customer_id=?,site_id=?,claimed_by=?,claimed_at=?,claim_proof_hash=?,claim_proof_encrypted=?,proof_expires_at=? WHERE id=? AND status='pending'",
                (identity['customer_id'], site_id, identity.get('email'), now.isoformat(), password_hash(claim_proof), encrypted_proof, proof_expires_at, claim['id']),
            ).rowcount
        if changed != 1:
            raise HTTPException(status_code=409, detail='Claim is no longer pending (already claimed or expired).')
        # The proof itself is never returned to the browser -- only
        # ever delivered to the device via claim/status, so a
        # browser-side leak (XSS, shared screen, browser history)
        # cannot hand out a redeemable credential. It IS held briefly
        # (claim_proof_encrypted, alongside the hash used to verify
        # redemption) in the same appliance_claims row rather than an
        # in-process cache -- durable persistence, as design rule #1 in
        # the state-machine doc requires, and safe across multiple
        # cloud worker processes. claim_complete() below nulls this
        # column out the moment the proof is consumed; _expire_if_due()
        # nulls it too if the claim is instead abandoned past its TTL
        # (security-hardening checkpoint -- the original Phase 1
        # implementation left an abandoned claim's plaintext proof
        # sitting in the database indefinitely). Short TTL and the
        # column's own narrow purpose bound its exposure the same way
        # every other single-use hashed secret in this codebase is
        # bounded.
        audit(identity, 'appliance_claim.confirmed', 'appliance_claim', claim['id'])
        logger.info('Claim confirmed claim_session_id=%s customer_id=%s site_id=%s', claim['claim_session_id'], identity['customer_id'], site_id)
        return {'status': 'claimed', 'device_id': claim['device_id']}

    @app.post('/api/appliance/claim/complete')
    def claim_complete(request: Request, payload: dict) -> dict:
        client = _client_ip(request)
        if not activation_limiter.allow(client):
            raise HTTPException(status_code=429, detail='Claim completion rate exceeded.')
        claim_session_id = str(payload.get('claim_session_id', '')).strip()
        claim_proof = str(payload.get('claim_proof', '')).strip()
        if not claim_session_id or not claim_proof:
            raise HTTPException(status_code=400, detail='claim_session_id and claim_proof are required.')
        claim = row('SELECT * FROM appliance_claims WHERE claim_session_id=?', (claim_session_id,))
        if not claim or claim['status'] != 'claimed' or not claim.get('claim_proof_hash'):
            raise HTTPException(status_code=403, detail='Claim is not ready to be completed.')
        try:
            proof_still_valid = datetime.fromisoformat(claim['proof_expires_at']) > _now()
        except (KeyError, TypeError, ValueError):
            proof_still_valid = False
        if not proof_still_valid or not verify_password(claim_proof, claim['claim_proof_hash']):
            raise HTTPException(status_code=403, detail='Claim proof is invalid or expired.')
        now = _now()
        with connection() as db:
            # Consume the proof atomically -- exactly the same
            # check-rowcount-before-mutating-further-state pattern
            # appliance_cloud.activate_appliance() uses for its own
            # single-use activation token. A second call with the same
            # (now-consumed) proof gets rowcount==0 here and fails
            # closed with 409, never a second appliances row or a
            # second credential.
            changed = db.execute(
                "UPDATE appliance_claims SET status='completed',completed_at=?,claim_proof_encrypted=NULL WHERE id=? AND status='claimed'",
                (now.isoformat(), claim['id']),
            ).rowcount
            if changed != 1:
                raise HTTPException(status_code=409, detail='Claim proof was already used.')
            appliance_id = secrets.token_hex(16)
            cloud_id = claim['device_id'].upper()
            db.execute(
                'INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)',
                (appliance_id, claim['customer_id'], claim['site_id'], cloud_id, now.isoformat()),
            )
            db.execute('UPDATE appliance_claims SET appliance_id=? WHERE id=?', (appliance_id, claim['id']))
            credential = secrets.token_urlsafe(48)
            credential_id = secrets.token_hex(8)
            db.execute(
                'INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at,created_by) VALUES(?,?,?,?,?)',
                (credential_id, appliance_id, password_hash(credential), now.isoformat(), 'claim'),
            )
        appliance = row('SELECT * FROM appliances WHERE id=?', (appliance_id,))
        # Same durable-persistence call activate_appliance() makes as
        # its own last step -- see appliance_activation.py. Untouched,
        # unmodified; this is the entire "one-time exchange into the
        # existing enrollment/credential mechanism" the Phase 1 plan
        # asked for.
        from appliance_activation import ActivationConflict, persist_activation
        try:
            persist_activation(
                appliance_id=appliance_id,
                cloud_id=cloud_id,
                credential=credential,
                customer_id=appliance['customer_id'],
                site_id=appliance['site_id'],
                partner_id=appliance.get('partner_id'),
            )
        except ActivationConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        audit({'email': cloud_id, 'role': 'appliance'}, 'appliance_claim.completed', 'appliance', appliance_id)
        logger.info('Claim completed appliance_id=%s cloud_id=%s', appliance_id, cloud_id)
        return {
            'appliance_id': appliance_id,
            'cloud_id': cloud_id,
            'credential': credential,
            'credential_id': credential_id,
            'partner_id': appliance.get('partner_id'),
            'customer_id': appliance['customer_id'],
            'site_id': appliance['site_id'],
            'message': 'Store this permanent credential securely; it will not be shown again.',
        }
