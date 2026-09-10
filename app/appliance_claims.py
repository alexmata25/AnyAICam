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

This commit adds only the device-facing claim/begin and claim/status
routes -- a device can open a claim session and poll it, but nothing
yet lets a customer actually confirm one, so every claim in this
commit alone stays pending until it expires. The portal lookup/confirm
routes and claim/complete follow in later commits; see the Phase 1
plan doc's 6-commit implementation order and the Phase 1 completion
report for the full picture.

Deliberately independent of the existing, unmodified admin-driven
paths in appliance_cloud.py (POST /api/appliance/activate, which
requires an appliances row and a pre-minted activation token) and
partner_workspace.py (POST /api/customer/appliances/link, which also
requires a pre-created appliances row scoped to the customer). Both
keep working exactly as before; this module never reads from or
writes to appliance_activation_tokens, and appliance_claims.py's own
new table is the only thing it touches until the one-time INSERT into
the existing appliances table at claim/complete (added in a later
commit).

Why a new table instead of relaxing appliances.customer_id/site_id's
NOT NULL constraints: see the Phase 1 plan doc §1. In short, a
pre-claim device cannot be represented in appliances (customer_id and
site_id are NOT NULL there, and appliance_activation_tokens is keyed
on an existing appliance_id), so all pre-claim and claim-in-progress
state lives in appliance_claims (this module) instead, and appliances
is only ever INSERTed once a claim actually completes.

Trust model for the device-facing endpoints: the device has no
credential yet, by definition, so these are protected the same way
appliance_cloud.activate_appliance() already protects its own
unauthenticated cloud_id+token exchange -- rate limiting, unguessable
high-entropy random tokens, and short expiries -- not a bearer
credential. claim_session_id and claim_proof are both treated as
bearer-equivalent secrets: never logged, and matched with
verify_password()'s constant-time comparison exactly like every other
hashed secret in this codebase. This is also why claim_session_id
(here) and claim_code/claim_proof (added in later commits) are always
carried in a POST body, never a URL path or query string -- a URL is
what every access log line at every layer (this process, any reverse
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

from appliance_protocol import RateLimiter
from partner_db import connection, password_hash, row, rows

logger = logging.getLogger('anyaicam.appliance_claims')

# Same shape/window as appliance_cloud.activation_limiter -- this flow
# is the self-service sibling of that same activation surface and
# deserves the same throttling posture, not a stricter or looser one
# invented from scratch.
claim_begin_limiter = RateLimiter(10, 300)
claim_status_limiter = RateLimiter(120, 60)

CLAIM_SESSION_TTL_MINUTES = 15
DEVICE_ID_PATTERN = re.compile(r'^[A-Za-z0-9._:-]{8,128}$')


def _now() -> datetime:
    return datetime.now()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else 'unknown'


def _valid_device_id(device_id: str) -> bool:
    return bool(DEVICE_ID_PATTERN.match(device_id or ''))


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


def _expire_if_due(claim: dict) -> dict:
    """Lazy expiry: flips a pending row to expired on the next read
    that touches it, rather than running a separate sweep job (see
    Phase 1 plan §8). Never mutates a non-pending row."""
    if claim['status'] != 'pending':
        return claim
    try:
        still_valid = datetime.fromisoformat(claim['expires_at']) > _now()
    except ValueError:
        still_valid = False
    if still_valid:
        return claim
    with connection() as db:
        db.execute("UPDATE appliance_claims SET status='expired' WHERE id=? AND status='pending'", (claim['id'],))
    claim = dict(claim)
    claim['status'] = 'expired'
    return claim


def register_appliance_claim_routes(app: FastAPI) -> None:
    @app.post('/api/appliance/claim/begin')
    def claim_begin(request: Request, payload: dict) -> dict:
        client = _client_ip(request)
        if not claim_begin_limiter.allow(client):
            raise HTTPException(status_code=429, detail='Claim attempt rate exceeded.')
        device_id = str(payload.get('device_id', '')).strip()
        if not _valid_device_id(device_id):
            raise HTTPException(status_code=400, detail='device_id is missing or has an invalid format.')
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
        if claim['status'] == 'claimed' and claim.get('claim_proof_plaintext'):
            try:
                proof_still_valid = datetime.fromisoformat(claim['proof_expires_at']) > _now()
            except (KeyError, TypeError, ValueError):
                proof_still_valid = False
            if proof_still_valid:
                # Returned on every poll while still valid, never
                # marked "issued" at read time -- consumption happens
                # only at claim/complete (added in a later commit),
                # which is what the "idempotent retry" requirement
                # actually depends on.
                response['claim_proof'] = claim['claim_proof_plaintext']
        return response
