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

import hashlib
import json
import logging
import re
import secrets
from datetime import datetime, timedelta
from html import escape
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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
# Security-hardening checkpoint, hardening item 3: how long a retry of
# claim/complete can still recover the original credential after a
# successful completion whose response the device never saw. Same
# order of magnitude as the other windows in this flow -- long enough
# to cover a realistic retry (a reboot, a dropped connection), short
# enough to bound how long a recoverable (if encrypted) copy of a live
# credential sits in the database.
CREDENTIAL_RECOVERY_TTL_MINUTES = 15
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


def _seconds_until(moment: str) -> int:
    """Seconds left before `moment` on THIS server's clock, so the appliance
    can say "expires in N minutes" whatever its own clock or time zone."""
    try:
        return max(0, int((datetime.fromisoformat(str(moment)) - _now()).total_seconds()))
    except (TypeError, ValueError):
        return 0


# Headless label claim (2026-10-07). A shipped appliance has no screen to
# show claim_code on, so installer/09-identity.sh mints a second code when
# the unit is imaged and it is printed on the unit's label (text and QR).
# The appliance never keeps that code: it keeps only its verifier,
# sha256(LABEL_VERIFIER_PREFIX + code), sends it with claim/begin, and the
# cloud stores only password_hash(verifier). A customer types or scans the
# label code in the portal; the cloud recomputes the verifier and matches
# it against pending claims. Possession of the box and its label is what
# proves the right to claim it -- the same model as a router's or camera's
# printed setup code. 12 Crockford base32 characters = 60 bits; together
# with claim_portal_limiter and the requirement that the appliance itself
# is online with a pending claim, guessing one is not practical, and a
# leaked database holds only PBKDF2 hashes of a 60-bit secret's digest.
LABEL_ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'  # Crockford base32: no I, L, O, U
LABEL_LENGTH = 12
LABEL_VERIFIER_PREFIX = 'anyaicam-label-claim-v1:'
LABEL_VERIFIER_PATTERN = re.compile(r'^[0-9a-f]{64}$')
_LABEL_LOOKALIKES = str.maketrans({'O': '0', 'I': '1', 'L': '1'})


def normalize_label_code(text: str) -> str | None:
    """The canonical 12-character form of a label code as a person may
    type it (any case, spaces or dashes, O for 0, I or L for 1), or None."""
    code = re.sub(r'[\s-]', '', str(text or '')).upper().translate(_LABEL_LOOKALIKES)
    if len(code) != LABEL_LENGTH or any(ch not in LABEL_ALPHABET for ch in code):
        return None
    return code


def label_verifier(code: str) -> str:
    """What the appliance sends for its label code (installer/09-identity.sh
    computes the same value with sha256sum)."""
    return hashlib.sha256((LABEL_VERIFIER_PREFIX + code).encode('ascii')).hexdigest()


def _label_verifier_from(payload: dict) -> str | None:
    """claim/begin's optional label_verifier: absent for an appliance claimed
    from its own terminal, a 64-hex sha256 otherwise."""
    value = payload.get('label_verifier')
    if value is None or value == '':
        return None
    value = str(value).strip().lower()
    if not LABEL_VERIFIER_PATTERN.match(value):
        raise HTTPException(status_code=400, detail='label_verifier must be a 64-character hex SHA-256.')
    return value


def _find_claim_by_label(label_code: str) -> dict | None:
    """Same bounded scan as _find_claim_by_code(), over pending claims whose
    appliance sent a label verifier."""
    code = normalize_label_code(label_code)
    if not code:
        return None
    verifier = label_verifier(code)
    now_text = _now().isoformat()
    for candidate in rows("SELECT * FROM appliance_claims WHERE status='pending' AND expires_at>? AND label_verifier_hash IS NOT NULL", (now_text,)):
        if verify_password(verifier, candidate['label_verifier_hash']):
            return candidate
    return None


def _find_claim_for_portal(payload: dict) -> dict | None:
    """A portal request names the appliance by its label code (label_code)
    or by the code its own terminal showed (claim_code)."""
    if str(payload.get('label_code', '') or '').strip():
        return _find_claim_by_label(str(payload['label_code']))
    claim_code = str(payload.get('claim_code', '')).strip()
    return _find_claim_by_code(claim_code) if claim_code else None


def _generate_claim_code() -> str:
    # 8 uppercase hex characters (32 bits) -- short enough to type from
    # a small display, long enough combined with claim_begin_limiter's
    # rate limit and the 15-minute expiry that brute-forcing a specific
    # live code through the portal lookup endpoint (added in a later
    # commit) is not practical.
    return secrets.token_hex(4).upper()


DEVICE_SECRET_MIN_LENGTH = 32


def _device_secret(payload: dict) -> str:
    """The claiming appliance's possession secret (2026-10-01 security fix).

    claim_begin() used to hand an existing pending claim's claim_session_id
    to anyone presenting the device's UUID, and claim_status()/
    claim_complete() trusted that session id alone -- so whoever learned a
    device UUID could poll the owner-confirmed proof and take the permanent
    appliance credential. The appliance now generates a random secret per
    claim and keeps it in its own 0600 claim-state file; the cloud stores
    only its hash, and resume, status and complete all require it."""
    secret = str(payload.get('device_secret', '') or '')
    if len(secret) < DEVICE_SECRET_MIN_LENGTH or len(secret) > 256:
        raise HTTPException(status_code=400, detail='device_secret is required (update the AnyAiCam agent).')
    return secret


def _possesses(claim: dict, secret: str) -> bool:
    return bool(claim.get('device_secret_hash')) and verify_password(secret, claim['device_secret_hash'])


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


def _recover_completed_result(claim: dict) -> dict | None:
    """Security-hardening checkpoint, hardening item 3: lets a retried
    claim/complete (same claim_session_id, same claim_proof, called
    again after the original successful completion) recover the exact
    same activation result -- appliance_id, cloud_id, credential,
    credential_id, partner_id, customer_id, site_id -- instead of
    either 403ing forever (the original Phase 1 behavior: the device's
    response was lost, and the one-time-shown credential was then
    permanently unrecoverable) or minting a second credential (which
    would violate "only one appliance credential is created").

    Returns None if there is nothing left to recover -- either the
    recovery window has passed, or no recovery material was ever
    stored (e.g. ANYAICAM_CLAIM_FLOW_SECRET_KEY was unset at the
    original completion, in which case retry-recovery was never
    possible for this claim, but the original completion itself was
    never blocked on that -- see the success path below). Lazily nulls
    the encrypted material once it's past its own recovery window,
    bounding its at-rest lifetime the same way abandoned claim proofs
    are bounded by _expire_if_due()."""
    if not claim.get('completed_credential_encrypted'):
        return None
    if not _still_valid(claim.get('credential_recovery_expires_at')):
        with connection() as db:
            db.execute('UPDATE appliance_claims SET completed_credential_encrypted=NULL WHERE id=?', (claim['id'],))
        return None
    decrypted = decrypt_claim_flow_secret(claim['completed_credential_encrypted'])
    if not decrypted:
        return None
    try:
        recovered = json.loads(decrypted)
    except json.JSONDecodeError:
        return None
    appliance = row('SELECT * FROM appliances WHERE id=?', (claim.get('appliance_id'),))
    if not appliance:
        return None
    return {
        'appliance_id': claim['appliance_id'],
        'cloud_id': appliance['cloud_id'],
        'credential': recovered.get('credential'),
        'credential_id': recovered.get('credential_id'),
        'partner_id': appliance.get('partner_id'),
        'customer_id': appliance['customer_id'],
        'site_id': appliance['site_id'],
        'message': 'Store this permanent credential securely; it will not be shown again.',
    }


def register_appliance_claim_routes(app: FastAPI, shell: Callable | None = None) -> None:
    """`shell` is optional and only used by the customer-facing claim
    page (Phase 2A) -- every device-facing and JSON portal route below
    is unaffected by whether it's supplied, matching this codebase's
    existing convention (register_appliance_cloud_routes' own
    `current_user` parameter works the same way)."""
    @app.post('/api/appliance/claim/begin')
    def claim_begin(request: Request, payload: dict) -> dict:
        client = _client_ip(request)
        if not claim_begin_limiter.allow(client):
            raise HTTPException(status_code=429, detail='Claim attempt rate exceeded.')
        device_id = str(payload.get('device_id', '')).strip()
        if not _valid_device_id(device_id):
            raise HTTPException(status_code=400, detail='device_id must be a valid UUIDv4.')
        device_id = _normalize_device_id(device_id)
        device_secret = _device_secret(payload)
        verifier = _label_verifier_from(payload)
        existing_appliance = row('SELECT id FROM appliances WHERE cloud_id=?', (device_id.upper(),))
        if existing_appliance:
            raise HTTPException(status_code=409, detail='This device is already provisioned. Use the existing activation flow.')
        resumable = _find_pending_claim_for_device(device_id)
        if resumable:
            # Only the appliance that opened this claim may resume it: a
            # UUID alone never returns the session.
            if not _possesses(resumable, device_secret):
                raise HTTPException(status_code=409, detail='A claim is already in progress for this device.')
            # Only a hash of the code is stored, so a resumed claim gets a
            # fresh code (2026-10-02): once the screen that showed the first
            # one was gone -- a closed terminal, a reboot -- the customer had
            # nothing to enter and a new claim was refused until this one
            # expired. The previous code stops working; same expiry.
            claim_code = _generate_claim_code()
            with connection() as db:
                db.execute("UPDATE appliance_claims SET claim_code_hash=? WHERE id=? AND status='pending'",
                           (password_hash(claim_code), resumable['id']))
                if verifier:
                    db.execute("UPDATE appliance_claims SET label_verifier_hash=? WHERE id=? AND status='pending'",
                               (password_hash(verifier), resumable['id']))
            logger.info('Claim session resumed with a new code claim_id=%s', resumable['id'])
            return {
                'claim_session_id': resumable['claim_session_id'],
                'claim_code': claim_code,
                'expires_at': resumable['expires_at'],
                'expires_in_seconds': _seconds_until(resumable['expires_at']),
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
                'INSERT INTO appliance_claims(id,device_id,claim_session_id,claim_code_hash,status,expires_at,created_at,device_secret_hash,label_verifier_hash) VALUES(?,?,?,?,?,?,?,?,?)',
                (claim_id, device_id, claim_session_id, password_hash(claim_code), 'pending', expires_at, now.isoformat(), password_hash(device_secret),
                 password_hash(verifier) if verifier else None),
            )
        # Never log the session id: it is a bearer value for this claim.
        logger.info('Claim session opened claim_id=%s', claim_id)
        return {
            'claim_session_id': claim_session_id,
            'claim_code': claim_code,
            'expires_at': expires_at,
            'expires_in_seconds': CLAIM_SESSION_TTL_MINUTES * 60,
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
        device_secret = _device_secret(payload)
        claim = row('SELECT * FROM appliance_claims WHERE claim_session_id=?', (claim_session_id,))
        if not claim or not _possesses(claim, device_secret):  # same answer as an unknown session
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
        claim = _find_claim_for_portal(payload)
        if not claim:
            raise HTTPException(status_code=404, detail='Claim code not found or expired.')
        return {'device_id': claim['device_id'], 'expires_at': claim['expires_at']}

    @app.post('/api/portal/claims/confirm')
    def portal_claim_confirm(request: Request, payload: dict) -> dict:
        identity = _customer_owner(request)
        _require_self_link_permission(identity)
        if not claim_portal_limiter.allow(identity.get('email', identity.get('id', 'unknown'))):
            raise HTTPException(status_code=429, detail='Claim confirmation rate exceeded.')
        site_id = str(payload.get('site_id', '')).strip()
        if not str(payload.get('claim_code', '') or '').strip() and not str(payload.get('label_code', '') or '').strip():
            raise HTTPException(status_code=400, detail='claim_code or label_code is required.')
        if not site_id:
            raise HTTPException(status_code=400, detail='site_id is required.')
        claim = _find_claim_for_portal(payload)
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
        logger.info('Claim confirmed claim_id=%s customer_id=%s site_id=%s', claim['id'], identity['customer_id'], site_id)
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
        device_secret = _device_secret(payload)
        claim = row('SELECT * FROM appliance_claims WHERE claim_session_id=?', (claim_session_id,))
        if (not claim or not _possesses(claim, device_secret) or not claim.get('claim_proof_hash')
                or not verify_password(claim_proof, claim['claim_proof_hash'])):
            raise HTTPException(status_code=403, detail='Claim is not ready to be completed.')
        if claim['status'] == 'completed':
            # Retry-safety path (hardening item 3): the proof already
            # verified above against this exact row's own hash (never
            # cleared on completion -- only claim_proof_encrypted is),
            # so this is confirmed to be a genuine retry of THIS same
            # claim, not a guess. Recover the original result rather
            # than creating a second credential or leaving the device
            # permanently unable to finish enrollment.
            recovered = _recover_completed_result(claim)
            if recovered is None:
                raise HTTPException(status_code=409, detail='Claim was already completed and its credential can no longer be recovered.')
            return recovered
        if claim['status'] != 'claimed':
            raise HTTPException(status_code=403, detail='Claim is not ready to be completed.')
        # claim_proof itself was already verified against claim_proof_hash
        # above (before the status branch), so only expiry remains to
        # check here.
        if not _still_valid(claim.get('proof_expires_at')):
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
            # Hardening item 3: best-effort, not a gate on completion
            # itself -- unlike confirm's fail-closed encryption
            # requirement, a retry-recovery feature must never block
            # the primary credential-issuance transaction. If the key
            # is unset here, the completion still succeeds; a retry in
            # that edge case simply finds nothing to recover and falls
            # back to the pre-hardening 409 behavior.
            recovery_ciphertext = encrypt_claim_flow_secret(json.dumps({'credential': credential, 'credential_id': credential_id}))
            recovery_expires_at = (now + timedelta(minutes=CREDENTIAL_RECOVERY_TTL_MINUTES)).isoformat() if recovery_ciphertext else None
            db.execute(
                'UPDATE appliance_claims SET completed_credential_encrypted=?,credential_recovery_expires_at=? WHERE id=?',
                (recovery_ciphertext, recovery_expires_at, claim['id']),
            )
        appliance = row('SELECT * FROM appliances WHERE id=?', (appliance_id,))
        # Same durable-persistence call activate_appliance() makes as its
        # own last step -- see appliance_activation.py. Only meaningful
        # for a genuinely single-tenant process (RUNTIME_ROLE edge/
        # combined); a cloud deployment claims many independent
        # appliances from one shared process, so it must skip this
        # entirely -- see local_activation_tracking_applies()'s own
        # docstring. Confirmed live on anyaicam-staging (2026-09-12):
        # without this gate, a real, independent Ryzen claim completed
        # successfully in the transaction above (appliances/
        # appliance_credentials rows both committed) but the response
        # below was then discarded with a 409 "already activated as
        # AIC-C90CF0C9" -- an unrelated EARLIER activation test's local
        # identity file on this same shared staging process, which has
        # nothing to do with the device actually being claimed. The
        # claim itself was already durably completed by that point;
        # only this now-skipped call ever stood in the way of returning
        # its result.
        from appliance_activation import ActivationConflict, local_activation_tracking_applies, persist_activation
        if local_activation_tracking_applies():
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

    if shell is None:
        return

    # Phase 2A: the smallest customer-facing page that can drive the
    # existing JSON APIs above -- enter a code, look it up, review the
    # device_id, pick one of this customer's own sites, confirm. No QR
    # scanning, no polling for the device to finish (that happens on
    # the appliance's own terminal, not here), reusing this codebase's
    # existing page_shell/CSS conventions unchanged (same
    # .action-button/.ghost-button/.panel/.health-detail classes
    # partner_workspace.py's customer pages already use). The shell's
    # own global fetch wrapper (see page_shell's own <script> in
    # main.py) attaches X-CSRF-Token to same-origin POSTs automatically,
    # so the two fetch() calls below need no special CSRF handling,
    # matching every other customer-facing action button in this
    # codebase (e.g. partner_workspace.py's link_customer_appliance
    # button).
    @app.get('/customer/claim-appliance', response_class=HTMLResponse)
    def customer_claim_appliance_page(request: Request):
        identity = partner_identity(request)
        if not identity:
            return RedirectResponse('/partner-login', status_code=303)
        if identity.get('role') != 'customer_owner':
            raise HTTPException(status_code=403, detail='Customer owner permission required.')
        sites = rows('SELECT * FROM sites WHERE customer_id=?', (identity['customer_id'],))
        # 2026-10-08: from /customer/setup Step 2 (?return=setup) the done
        # step leads back to setup; otherwise to the dashboard.
        back_href, back_label = ('/customer/setup', 'Continue setup') if request.query_params.get('return') == 'setup' else ('/', 'Go to your dashboard')
        site_options = ''.join(f'<option value="{escape(s["id"],quote=True)}">{escape(s["name"])}</option>' for s in sites) or '<option value="">No sites on this account yet</option>'
        content = f'''<header class="topbar"><div><p class="eyebrow">Add an appliance</p><h1>Claim an appliance</h1></div></header>
        <section class="panel">
          <div id="claim-step-code">
            <p>Enter the claim code your appliance shows while it waits to be linked: on its screen during setup, on the last page of the Windows installer, or the claim code printed on its label (or scan the label's QR code with your phone). A new appliance can take a few minutes to start after it is plugged into power and your network.</p>
            <label>Claim code<input id="claim-code-input" maxlength="20" autocapitalize="characters" autocomplete="off" spellcheck="false" placeholder="Claim code"></label>
            <button class="action-button" id="claim-lookup-button">Look up</button>
            <p id="claim-lookup-message" class="health-detail"></p>
          </div>
          <div id="claim-step-confirm" hidden>
            <p>Appliance found: <strong id="claim-device-id"></strong></p>
            <label>Site<select id="claim-site-select">{site_options}</select></label>
            <button class="action-button" id="claim-confirm-button">Confirm claim</button>
            <button class="ghost-button" id="claim-cancel-button">Start over</button>
            <p id="claim-confirm-message" class="health-detail"></p>
          </div>
          <div id="claim-step-done" hidden>
            <p>Claim confirmed. The appliance will finish activating automatically within a few seconds.</p>
            <a class="action-button" id="claim-done-continue" href="{back_href}">{back_label}</a>
          </div>
        </section>'''
        scripts = '''<script>
        // A label code is 12 characters (dashes/spaces ignored); the code an
        // appliance's own terminal shows is 8. Sent in the POST body only.
        function claimBody(){
          const raw=document.getElementById('claim-code-input').value.trim().toUpperCase(),compact=raw.replace(/[\\s-]/g,'');
          return compact.length===12?{label_code:compact}:{claim_code:raw};
        }
        // From the label QR via /claim (kept in this tab only, never in a URL).
        try{const saved=sessionStorage.getItem('anyaicam.claimLabel');if(saved){sessionStorage.removeItem('anyaicam.claimLabel');document.getElementById('claim-code-input').value=saved}}catch(e){}
        document.getElementById('claim-lookup-button').onclick=async()=>{
          const message=document.getElementById('claim-lookup-message');
          message.textContent='';
          const response=await fetch('/api/portal/claims/lookup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(claimBody())}),body=await response.json();
          if(!response.ok){message.textContent=response.status===404?'No appliance is waiting with that code. Check the code, and that the appliance is plugged into power and your network (it can take a few minutes to start).':(body.detail||'Claim code not found or expired.');return}
          // A readable name, not the raw device UUID (2026-10-02).
          document.getElementById('claim-device-id').textContent='AnyAiCam appliance (ID ending '+String(body.device_id||'').replace(/-/g,'').slice(-6).toUpperCase()+')';
          document.getElementById('claim-step-code').hidden=true;
          document.getElementById('claim-step-confirm').hidden=false;
        };
        document.getElementById('claim-cancel-button').onclick=()=>{
          document.getElementById('claim-step-confirm').hidden=true;
          document.getElementById('claim-step-code').hidden=false;
          document.getElementById('claim-code-input').value='';
          document.getElementById('claim-confirm-message').textContent='';
        };
        document.getElementById('claim-confirm-button').onclick=async()=>{
          const siteId=document.getElementById('claim-site-select').value,message=document.getElementById('claim-confirm-message'),button=document.getElementById('claim-confirm-button');
          if(!siteId){message.textContent='Select a site first.';return}
          button.disabled=true;
          const response=await fetch('/api/portal/claims/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.assign(claimBody(),{site_id:siteId}))}),body=await response.json();
          if(!response.ok){button.disabled=false;message.textContent=body.detail||'Could not confirm the claim.';return}
          document.getElementById('claim-step-confirm').hidden=true;
          document.getElementById('claim-step-done').hidden=false;
          showToast('Appliance claim confirmed.');
        };
        </script>'''
        return shell('Claim appliance', 'users', content, scripts)

    # The label's QR code opens /claim#label=XXXX-XXXX-XXXX. The code is in
    # the fragment, which browsers never send to a server (so it is in no
    # access log), but a fragment does not survive the sign-in redirect --
    # so this public page moves it into this tab's sessionStorage, removes
    # it from the address bar and history, and continues to the claim page
    # (through sign-in if needed), which picks it up. Typed by hand,
    # app.anyaicam.com/claim simply leads to the claim page.
    @app.get('/claim', response_class=HTMLResponse)
    def claim_label_entry():
        page = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Claim your AnyAiCam appliance</title></head><body>
<p>Opening AnyAiCam&hellip; <a href="/customer/claim-appliance">Continue</a></p>
<script>
(function(){
  var match=/(?:^|[#&])label=([0-9A-Za-z -]{12,20})(?:&|$)/.exec(location.hash||'');
  if(match){try{sessionStorage.setItem('anyaicam.claimLabel',decodeURIComponent(match[1]).toUpperCase())}catch(e){}}
  try{history.replaceState(null,'',location.pathname)}catch(e){}
  location.replace('/customer/claim-appliance');
})();
</script></body></html>'''
        return HTMLResponse(page, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
