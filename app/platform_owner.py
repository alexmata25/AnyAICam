"""platform_owner: RBAC tier and recovery infrastructure for the account(s)
that need the complete AnyAiCam administrative surface, above ordinary
Partner Portal accounts -- built entirely on top of what already existed
in this codebase before this module, not a new parallel identity system.

Role hierarchy and how it actually maps onto this codebase (2026-09-23)
-------------------------------------------------------------------------
The requested hierarchy is platform_owner / platform_admin / partner_admin /
partner_user / customer_admin / customer_user. Concretely:

  - platform_owner and platform_admin ARE the existing identity_grants(
    role='administrator', scope_type='global') mechanism -- already fully
    implemented, already fully tested (see test_cloud_administrator_bridge.py,
    test_bootstrap_admin_identity_grant.py, test_portal_login_delegated_auth.py),
    already wired into login routing (resolve_portal_login() in main.py
    sends a scope_type='global' administrator straight to /admin-portal),
    nav visibility (navigation_keys_for_role() already treats partner_db
    role=='administrator' as an ADMIN_PORTAL_ROLES identity), and route-
    level re-verification on every single request (current_user() ->
    cloud_administrator_bridge() -> appliance_identity.
    has_global_administrator_grant(), which re-checks the LIVE grant row,
    not a cached session claim, on every call -- a revoked grant loses
    access on the very next request). There is no new grant shape, no new
    role string, no new nav-visibility logic to write for this tier's
    access itself -- see provision_platform_owner() in appliance_identity.py
    for the one real gap that existed (bootstrapping the FIRST such
    account) and this module for the recovery infrastructure layered on
    top of it. platform_owner and platform_admin are currently
    synonymous in data shape (both are simply "holds a live global
    grant") -- no concrete behavioral distinction between them was
    specified beyond naming, so none is invented here. A future real
    split (e.g. platform_admin unable to grant new platform_owner
    accounts) can be added later by checking granted_by/an additional
    grant attribute, without a schema change, once there's an actual
    behavioral requirement to enforce.

  - partner_admin / partner_user are this codebase's existing partner_
    owner / administrator(scope_type='partner', i.e. NOT holding a
    global grant) / technician / salesperson roles and their existing,
    already-audited ROLE_PERMISSIONS (partner_db.py) -- unrenamed,
    unchanged. Building this pass's hierarchy did not require touching
    that system at all, and per the user's own explicit instruction
    ("Do not destroy or replace working Partner/customer authentication"),
    it wasn't.

  - customer_admin / customer_user are this codebase's existing
    customer_owner / customer_viewer roles -- likewise unrenamed,
    unchanged.

The dedicated "AnyAiCam Admin Console" the user asked to prefer already
exists too: /admin-portal (the real destination a global grant already
routes to) and /operations/identity-grants (the real, already-tested,
already-permission-gated UI+API -- GET/POST /api/operations/identity-
grants, POST .../revoke -- for granting/revoking further platform_owner/
platform_admin access to other accounts, self-service, once at least one
real account holds the bootstrap grant). Building a second, parallel
console would have duplicated already-working, already-tested surface
for no benefit; this module's job was closing the one real bootstrap
gap and adding the recovery layer neither this tier nor any other
account in this codebase had before.

What this module actually adds
-------------------------------
1. MFA (TOTP, RFC 6238, stdlib-only -- hmac/hashlib/struct/time/base64,
   no new dependency) for accounts holding a live global grant. Table:
   platform_owner_mfa (one row per user, secret_base32, confirmed_at
   NULL until the enrollment code is verified once).
2. Recovery codes: 10 single-use, pbkdf2-hashed-at-rest codes (same
   password_hash()/verify_password() primitive partner_db.py already
   uses for passwords), shown once at generation, consumable as an
   alternate to a TOTP code if the device is lost. Table:
   platform_owner_recovery_codes.
3. Login-time enforcement: POST /api/portal-login (main.py) is given
   ONE new, narrow branch -- see the PLATFORM_OWNER_MFA_HOOK marker in
   that function -- inserted immediately before its existing call to
   establish_partner_session(). If (and only if) the login just resolved
   to system=="partner", destination=="/admin-portal" (i.e. a genuine
   global-grant login, not any other portal/role), AND this user has a
   CONFIRMED MFA enrollment, the session is NOT established yet.
   Instead a short-lived (5 minute), single-use pending_mfa_logins row
   is created holding only a user_id + the already-decided destination/
   email/role/authorization_version (never a password, never a grant --
   the grant itself is independently re-verified again, live, the
   moment the pending login is actually completed) and the endpoint
   returns {"status":"mfa_required","mfa_pending_token":...} instead of
   a redirect. Every account without confirmed MFA (which includes
   every non-global-grant account in the whole system, always) is
   completely unaffected -- this branch is unreachable for them.
4. POST /api/platform-owner/mfa/verify: consumes a pending_mfa_logins
   token plus a 6-digit TOTP code OR one recovery code, re-verifies the
   grant is STILL live at this exact moment (not trusting the earlier
   password-check result), and only then calls the exact same
   establish_partner_session() the ordinary login path already uses --
   so a completed MFA login is byte-for-byte the same kind of session
   as any other, subject to the exact same route-level re-verification
   on every subsequent request.
5. Break-glass recovery, deliberately NOT reachable from any login form:
   create_break_glass_token() is a plain Python function, meant to be
   invoked directly (a one-line `docker exec ... python3 -c "..."` on
   the host, or an equivalent direct-database-access path an operator
   already has) -- never a route, never callable over HTTP by an
   attacker. It mints a short-lived (30 minute), single-use, hashed-at-
   rest token tied to one specific existing global-grant account and
   writes a real audit_logs row (partner_db.audit()) recording who
   created it and why. The ONLY web-reachable piece is redemption --
   POST /api/platform-owner/break-glass-recover, which accepts the raw
   token, verifies hash+expiry+unused, marks it used, and establishes an
   ORDINARY session via establish_partner_session() exactly like every
   other path here -- recovery re-authenticates the ACCOUNT, it never
   grants a permission the account's own live identity_grants row
   wouldn't otherwise produce, and every subsequent request still goes
   through the exact same current_user()/cloud_administrator_bridge()/
   has_global_administrator_grant() re-verification as any other
   session. Redemption is itself audited (a second audit_logs row, on
   successful use).

v1/dev storage note (matching this codebase's own existing, already-
documented limitation for identity_signing_keys -- see partner_db.py's
initialize_database()): TOTP secrets and break-glass token hashes live
in this same SQLite/Postgres database. A real production deployment
would keep TOTP secrets in a proper secrets manager, same as that
existing table's own docstring already says about its private key --
this module does not pretend otherwise.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import datetime, timedelta
from typing import Callable

from fastapi import FastAPI, HTTPException, Request


# --------------------------------------------------------------- TOTP (RFC 6238)


def generate_totp_secret() -> str:
    """A fresh, random 160-bit secret, base32-encoded (the standard
    encoding every authenticator app -- Google Authenticator, Authy,
    1Password, etc. -- expects for manual/QR entry)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def totp_provisioning_uri(secret_base32: str, *, email: str, issuer: str = "AnyAiCam") -> str:
    """A standard otpauth:// URI -- what a QR code for this secret would
    encode. Returned as plain text (this module renders no QR image);
    any authenticator app also accepts typing the raw secret in
    manually, which the enroll response also returns."""
    from urllib.parse import quote

    label = quote(f"{issuer}:{email}")
    return f"otpauth://totp/{label}?secret={secret_base32}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"


def _totp_code_at(secret_base32: str, counter: int) -> str:
    key = base64.b32decode(secret_base32.upper() + "=" * (-len(secret_base32) % 8))
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{truncated % 1_000_000:06d}"


def verify_totp_code(secret_base32: str, code: str, *, now: float | None = None, window: int = 1) -> bool:
    """True if `code` (a 6-digit string) is valid for the current 30s
    step, or up to `window` steps before/after it (clock-skew
    tolerance -- +/-1 step, i.e. +/-30s, is the conventional default
    every mainstream TOTP implementation uses). Constant-time compare
    per candidate so this never leaks which step (if any) matched via
    timing."""
    code = (code or "").strip()
    if not code or not code.isdigit() or len(code) != 6:
        return False
    now = time.time() if now is None else now
    counter = int(now // 30)
    return any(hmac.compare_digest(_totp_code_at(secret_base32, counter + delta), code) for delta in range(-window, window + 1))


# ------------------------------------------------------------- recovery codes


def _hash_code(code: str) -> str:
    from partner_db import password_hash

    return password_hash(code)


def _verify_code(code: str, encoded: str) -> bool:
    from partner_db import verify_password

    return verify_password(code, encoded)


def generate_recovery_codes(db, *, user_id: str, count: int = 10) -> list[str]:
    """Regenerating invalidates every prior code for this user (a stale
    code from a prior enrollment must never keep working silently) and
    returns the `count` new raw codes -- shown to the caller exactly
    once; only their pbkdf2 hash is ever stored. Each is an 8-character
    uppercase alphanumeric string (Crockford-ish, no ambiguous
    0/O/1/I/L) grouped for readability."""
    alphabet = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
    now = datetime.now().isoformat()
    db.execute("DELETE FROM platform_owner_recovery_codes WHERE user_id=?", (user_id,))
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(alphabet) for _ in range(8))
        codes.append(raw)
        db.execute(
            "INSERT INTO platform_owner_recovery_codes(id,user_id,code_hash,used_at,created_at) VALUES(?,?,?,NULL,?)",
            (secrets.token_hex(8), user_id, _hash_code(raw), now),
        )
    return codes


def consume_recovery_code(db, *, user_id: str, code: str) -> bool:
    """True and marks the matching row used (single redemption, never
    reusable) if `code` matches any still-unused code for this user --
    false otherwise, including for an already-used code. Every unused
    row is checked (hashes are salted per-row, there's no shortcut
    lookup by value) -- the recovery-code population is always small
    (10 by default), so this is not a performance concern.

    2026-09-23 review fix: finding the match (by hash) and claiming it
    are necessarily two steps -- there's no shortcut lookup by value --
    but claiming it must still be one atomic statement, the same
    UPDATE-with-rowcount-check idiom cloud_security.py's own
    consume_password_reset() already established for exactly this
    problem. The prior version's plain `UPDATE ... WHERE id=?` (no
    `AND used_at IS NULL`, no rowcount check) let two concurrent
    redemptions of the same code both read "unused" before either
    wrote, so both could succeed -- a real, if narrow, single-use
    violation this codebase already had the right pattern for
    elsewhere and this function didn't use."""
    rows = db.execute(
        "SELECT id,code_hash FROM platform_owner_recovery_codes WHERE user_id=? AND used_at IS NULL", (user_id,)
    ).fetchall()
    for row in rows:
        if _verify_code(code, row["code_hash"]):
            claimed = db.execute(
                "UPDATE platform_owner_recovery_codes SET used_at=? WHERE id=? AND used_at IS NULL",
                (datetime.now().isoformat(), row["id"]),
            )
            return bool(claimed.rowcount)
    return False


# --------------------------------------------------------------------- MFA


def mfa_is_confirmed(db, *, user_id: str) -> bool:
    row = db.execute("SELECT confirmed_at FROM platform_owner_mfa WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and row["confirmed_at"])


def _require_global_grant_session(request: Request) -> dict:
    """Every route below that manages MFA/recovery-code enrollment
    itself (not the login-time verify/break-glass endpoints, which are
    reached mid-login or without a session by design) requires an
    ALREADY-ESTABLISHED session that is, right now, a live global
    grant -- re-checked the same way current_user()/
    cloud_administrator_bridge() already does for every other Admin
    Portal route, not trusted from the session cookie's own role claim
    alone."""
    from partner_portal import partner_identity

    identity = partner_identity(request)
    if not identity or identity.get("role") != "administrator":
        raise HTTPException(status_code=403, detail="Platform owner access is required.")
    email = str(identity.get("email") or "").strip()
    from appliance_identity import has_global_administrator_grant
    from partner_db import connection

    with connection() as db:
        if not email or not has_global_administrator_grant(db, email=email):
            raise HTTPException(status_code=403, detail="Platform owner access is required.")
        user = db.execute("SELECT id FROM partner_users WHERE lower(email)=?", (email.lower(),)).fetchone()
    if not user:
        raise HTTPException(status_code=403, detail="Platform owner access is required.")
    return {"email": email, "user_id": user["id"]}


# ---------------------------------------------------------------- break-glass


def create_break_glass_token(db, *, email: str, reason: str, created_by: str, ttl_minutes: int = 30) -> str | None:
    """NOT a route -- call this directly (a one-line `python3 -c` on the
    host, or equivalent direct database/infrastructure access) when the
    normal password + MFA + recovery-codes path is genuinely
    unavailable. Returns the raw token (shown/logged exactly once by
    the caller's own shell -- never persisted anywhere but this call's
    return value) or None if `email` doesn't currently hold a live
    global grant (break-glass recovers access to an EXISTING platform
    owner account, it never creates a new one or grants one that
    doesn't already exist). Every creation is audited via partner_db.
    audit() with `reason` and `created_by` recorded -- this is
    deliberately not silent."""
    from appliance_identity import has_global_administrator_grant

    email = (email or "").strip().lower()
    if not email or not has_global_administrator_grant(db, email=email):
        return None
    user = db.execute("SELECT id FROM partner_users WHERE lower(email)=?", (email,)).fetchone()
    if not user:
        return None
    raw_token = secrets.token_urlsafe(32)
    now = datetime.now()
    expires_at = now + timedelta(minutes=ttl_minutes)
    token_id = secrets.token_hex(8)
    db.execute(
        "INSERT INTO break_glass_tokens(id,user_id,token_hash,reason,created_by,created_at,expires_at,used_at) VALUES(?,?,?,?,?,?,?,NULL)",
        (token_id, user["id"], _hash_code(raw_token), reason, created_by, now.isoformat(), expires_at.isoformat()),
    )
    # Written directly on the already-open `db` handle, NOT via
    # partner_db.audit() -- this function is meant to be called from
    # inside the caller's own `with connection() as db:` block (a
    # direct shell/script invocation, per this function's own "NOT a
    # route" contract above), and audit() opening a second connection
    # while the first is still open/uncommitted is a real SQLite lock,
    # not just a style preference. Same audit_logs shape audit() itself
    # writes, just inline -- see provision_platform_owner() (appliance_
    # identity.py) for the identical fix and its own fuller rationale.
    import json as _json
    db.execute(
        "INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
        (created_by, "system", "break_glass_create", "break_glass_token", token_id, _json.dumps({"target_email": email, "reason": reason, "expires_at": expires_at.isoformat()}), now.isoformat()),
    )
    return raw_token


def _redeem_break_glass_token(db, *, email: str, token: str) -> dict | None:
    """2026-09-23 review fix: same atomic-claim pattern as
    consume_recovery_code() above, for the same reason -- the token
    must be hash-matched among candidates before its specific row id is
    known, but claiming that identified row must be one atomic
    UPDATE-with-rowcount-check statement (cloud_security.py's
    consume_password_reset() idiom), not a separate UPDATE with no
    check. The prior version's bare `UPDATE ... WHERE id=?` let two
    concurrent redemptions of the same break-glass token both read
    "unused" before either wrote -- for the single most sensitive
    credential in this whole module, this must be airtight."""
    email = (email or "").strip().lower()
    user = db.execute("SELECT id FROM partner_users WHERE lower(email)=?", (email,)).fetchone()
    if not user:
        return None
    now_iso = datetime.now().isoformat()
    rows = db.execute(
        "SELECT id,token_hash,expires_at FROM break_glass_tokens WHERE user_id=? AND used_at IS NULL AND expires_at>?",
        (user["id"], now_iso),
    ).fetchall()
    for row in rows:
        if _verify_code(token, row["token_hash"]):
            claimed = db.execute(
                "UPDATE break_glass_tokens SET used_at=? WHERE id=? AND used_at IS NULL",
                (now_iso, row["id"]),
            )
            if claimed.rowcount:
                return {"user_id": user["id"]}
            return None
    return None


# ---------------------------------------------------------- pending MFA logins
# Used by main.py's POST /api/portal-login (see the PLATFORM_OWNER_MFA_HOOK
# marker there) and this module's own /api/platform-owner/mfa/verify route.


def create_pending_mfa_login(db, *, user_id: str, destination: str, email: str, role: str, authorization_version_at_login: int | None, ttl_minutes: int = 5) -> str:
    token = secrets.token_urlsafe(24)
    now = datetime.now()
    db.execute(
        "INSERT INTO pending_mfa_logins(id,user_id,destination,email,role,authorization_version_at_login,created_at,expires_at,used_at) VALUES(?,?,?,?,?,?,?,?,NULL)",
        (token, user_id, destination, email, role, authorization_version_at_login, now.isoformat(), (now + timedelta(minutes=ttl_minutes)).isoformat()),
    )
    return token


def _consume_pending_mfa_login(db, *, token: str) -> dict | None:
    """2026-09-23 review fix: `token` is itself the row's primary key
    here (unlike the recovery-code/break-glass cases, which must first
    find a hash match among several candidates) -- so the claim can be
    the FIRST and only statement, no separate lookup needed, closing
    the same class of race the prior SELECT-then-UPDATE (no rowcount
    check) version had: two concurrent completions of the same pending
    login could otherwise both read "unused" before either wrote."""
    now_iso = datetime.now().isoformat()
    claimed = db.execute(
        "UPDATE pending_mfa_logins SET used_at=? WHERE id=? AND used_at IS NULL AND expires_at>?",
        (now_iso, token, now_iso),
    )
    if not claimed.rowcount:
        return None
    row = db.execute(
        "SELECT id,user_id,destination,email,role,authorization_version_at_login FROM pending_mfa_logins WHERE id=?",
        (token,),
    ).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------- routes


def register_platform_owner_routes(app: FastAPI, shell: Callable) -> None:
    @app.post("/api/platform-owner/mfa/enroll")
    def platform_owner_mfa_enroll(request: Request) -> dict:
        actor = _require_global_grant_session(request)
        secret = generate_totp_secret()
        from partner_db import connection

        with connection() as db:
            db.execute(
                "INSERT INTO platform_owner_mfa(user_id,secret_base32,confirmed_at,created_at) VALUES(?,?,NULL,?) "
                "ON CONFLICT(user_id) DO UPDATE SET secret_base32=excluded.secret_base32, confirmed_at=NULL, created_at=excluded.created_at",
                (actor["user_id"], secret, datetime.now().isoformat()),
            )
        return {
            "status": "enrolled_pending_confirmation",
            "secret_base32": secret,
            "provisioning_uri": totp_provisioning_uri(secret, email=actor["email"]),
            "message": "Enter the 6-digit code from your authenticator app to confirm enrollment.",
        }

    @app.post("/api/platform-owner/mfa/confirm")
    def platform_owner_mfa_confirm(request: Request, payload: dict) -> dict:
        actor = _require_global_grant_session(request)
        code = str(payload.get("code", ""))
        from partner_db import audit as partner_audit
        from partner_db import connection

        with connection() as db:
            row = db.execute("SELECT secret_base32 FROM platform_owner_mfa WHERE user_id=?", (actor["user_id"],)).fetchone()
            if not row:
                raise HTTPException(status_code=400, detail="No pending MFA enrollment. Call enroll first.")
            if not verify_totp_code(row["secret_base32"], code):
                raise HTTPException(status_code=400, detail="Incorrect code.")
            db.execute("UPDATE platform_owner_mfa SET confirmed_at=? WHERE user_id=?", (datetime.now().isoformat(), actor["user_id"]))
            codes = generate_recovery_codes(db, user_id=actor["user_id"])
        partner_audit({"email": actor["email"], "role": "administrator"}, "mfa_confirm", "platform_owner_mfa", actor["user_id"])
        return {"status": "confirmed", "recovery_codes": codes, "message": "MFA is now required for this account's Admin Portal logins. Save these recovery codes -- they are shown only once."}

    @app.post("/api/platform-owner/recovery-codes/regenerate")
    def platform_owner_recovery_codes_regenerate(request: Request) -> dict:
        actor = _require_global_grant_session(request)
        from partner_db import audit as partner_audit
        from partner_db import connection

        with connection() as db:
            if not mfa_is_confirmed(db, user_id=actor["user_id"]):
                raise HTTPException(status_code=400, detail="Confirm MFA enrollment before generating recovery codes.")
            codes = generate_recovery_codes(db, user_id=actor["user_id"])
        partner_audit({"email": actor["email"], "role": "administrator"}, "recovery_codes_regenerate", "platform_owner_recovery_codes", actor["user_id"])
        return {"status": "regenerated", "recovery_codes": codes}

    @app.post("/api/platform-owner/mfa/verify")
    def platform_owner_mfa_verify(request: Request, payload: dict):
        mfa_pending_token = str(payload.get("mfa_pending_token", ""))
        code = str(payload.get("code", ""))
        from appliance_identity import has_global_administrator_grant
        from partner_db import audit as partner_audit
        from partner_db import connection

        with connection() as db:
            pending = _consume_pending_mfa_login(db, token=mfa_pending_token)
            if not pending:
                raise HTTPException(status_code=400, detail="This sign-in attempt has expired. Sign in again.")
            # Re-verify the grant is STILL live right now -- the pending
            # row is only a "password already checked, MFA still owed"
            # marker, never itself a source of authorization.
            if not has_global_administrator_grant(db, email=pending["email"]):
                raise HTTPException(status_code=403, detail="This account no longer has platform owner access.")
            mfa_row = db.execute("SELECT secret_base32, confirmed_at FROM platform_owner_mfa WHERE user_id=?", (pending["user_id"],)).fetchone()
            ok = bool(mfa_row and mfa_row["confirmed_at"] and verify_totp_code(mfa_row["secret_base32"], code))
            if not ok:
                ok = consume_recovery_code(db, user_id=pending["user_id"], code=code)
            if not ok:
                raise HTTPException(status_code=400, detail="Incorrect code.")
            user_row = db.execute("SELECT id,partner_id,customer_id FROM partner_users WHERE id=?", (pending["user_id"],)).fetchone()
        partner_audit({"email": pending["email"], "role": "administrator"}, "mfa_login_verify", "session", pending["user_id"])

        from partner_portal import establish_partner_session

        return establish_partner_session(
            pending["destination"], request=request, email=pending["email"], role=pending["role"],
            user=dict(user_row) if user_row else None,
            authorization_version_at_login=pending["authorization_version_at_login"],
        )

    @app.post("/api/platform-owner/break-glass-recover")
    def platform_owner_break_glass_recover(request: Request, payload: dict):
        email = str(payload.get("email", ""))
        token = str(payload.get("token", ""))
        from appliance_identity import has_global_administrator_grant
        from partner_db import audit as partner_audit
        from partner_db import connection

        with connection() as db:
            redeemed = _redeem_break_glass_token(db, email=email, token=token)
            if not redeemed:
                raise HTTPException(status_code=400, detail="Invalid or expired recovery token.")
            # Same live re-verification as every other path here -- a
            # token redeemed after the grant itself was independently
            # revoked must not still succeed.
            if not has_global_administrator_grant(db, email=email.strip().lower()):
                raise HTTPException(status_code=403, detail="This account no longer has platform owner access.")
            user_row = db.execute("SELECT id,partner_id,customer_id FROM partner_users WHERE id=?", (redeemed["user_id"],)).fetchone()
        partner_audit({"email": email.strip().lower(), "role": "administrator"}, "break_glass_redeem", "session", redeemed["user_id"])

        from partner_portal import establish_partner_session

        return establish_partner_session(
            "/admin-portal", request=request, email=email.strip().lower(), role="administrator",
            user=dict(user_row) if user_row else None, authorization_version_at_login=None,
        )
