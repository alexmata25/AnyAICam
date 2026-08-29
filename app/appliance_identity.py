"""Appliance identity contract v1 -- signed manifest, delegated operator
authentication, and revocation reconciliation. See the reviewed design
doc (published this session) for the full rationale; this module is the
implementation of that contract's cloud side.

Governing rule this module exists to enforce: an appliance is authorized
to know about a user only if a live identity_grants row *explicitly*
resolves to that appliance's own scope (global / its partner_id / its
customer_id / its site_id / its own cloud_id) -- never merely because
the user's partner_id matches the appliance's partner_id. See
grant_resolves() below, the single choke point every other function in
this module routes through.

Two roles never appear here, by construction, not by filtering:
  - admin@local (main.py's legacy users.json bootstrap account) is not
    a partner_users row at all, so it cannot have a grant and cannot
    appear in identity_grants, a manifest, or an assertion.
  - Plaintext passwords, password hashes, and password-reset secrets
    are never read by anything in this module that builds a manifest or
    assertion body -- only password_hash verification (via partner_db.
    verify_password) touches a hash, and only to produce a yes/no,
    never to expose it.

Signing: Ed25519 (asymmetric). The cloud holds the private key; an
appliance only ever needs the public key to verify, so it never holds
anything capable of forging a manifest or assertion, even if the
appliance itself were compromised. v1/dev note: this module stores the
keypair in partner_db's identity_signing_keys table for the mock
backend's convenience -- a real cloud deployment keeps the private key
in a KMS/secrets manager, never a queryable table; see
MockCloudIdentityBackend's own docstring below.

Pure, DB/FastAPI-free decision logic (fully unit-testable) plus
DB-touching wrappers that take an explicit db connection, matching
camera_access.py's established dependency-light pattern in this
codebase.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
from datetime import datetime, timedelta

logger = logging.getLogger("anyaicam.identity")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

SCOPE_TYPES = {"global", "partner", "customer", "site", "appliance"}

# authenticate_operator() below picks the first entry of its sorted
# "matching" grants list once more than one grant satisfies the
# requested portal bucket for the same account -- broadest scope first,
# so a user who legitimately holds both a broad and a narrower grant for
# the same role/portal (e.g. a true global administrator who is also
# explicitly the administrator of one specific partner) always gets
# their broadest applicable view, never an arbitrary one that happens to
# depend on identity_grants row insertion order (SQLite makes no
# ordering guarantee without an explicit ORDER BY). Found live: once
# bootstrap_admin() (partner_db.py) started creating its own scope_type=
# 'partner' grant, an account that ALSO held an explicit scope_type=
# 'global' grant for the same role could be routed to the narrower
# partner view purely because its grant happened to be inserted first.
SCOPE_BREADTH_ORDER = {"global": 0, "partner": 1, "customer": 2, "site": 3, "appliance": 4}
GRANTABLE_ROLES = {"administrator", "partner_owner", "salesperson", "technician", "customer_owner", "customer_viewer"}
PORTAL_BUCKETS = {
    "administrator": {"administrator"},
    "partner": {"partner_owner", "salesperson"},
    "technician": {"technician"},
}

# The authorization matrix from the reviewed design doc, enforced at
# grant-creation time (see validate_grant_role_scope()) so an invalid
# combination -- a technician granted 'global', a customer granted
# 'partner' -- can never be created via the grant-management API in the
# first place, not just filtered out later at manifest-build time.
# 'administrator' is deliberately allowed at both 'global' (true
# platform-wide reach) and 'partner' (administrator of one company only)
# -- see grant_resolves()'s own docstring on why that distinction is the
# whole point of this contract.
VALID_ROLE_SCOPES = {
    "administrator": {"global", "partner"},
    "partner_owner": {"partner"},
    "salesperson": {"partner"},
    "technician": {"appliance", "site"},
    "customer_owner": {"customer"},
    "customer_viewer": {"customer"},
}


def validate_grant_role_scope(role: str, scope_type: str) -> None:
    """Raises ValueError for a role/scope_type combination that isn't in
    VALID_ROLE_SCOPES -- the grant-management API (main.py) and
    create_grant() both route through this so an invalid grant can never
    be created by either path."""
    if role not in GRANTABLE_ROLES:
        raise ValueError(f"Unknown role: {role!r}. Must be one of {sorted(GRANTABLE_ROLES)}.")
    if scope_type not in VALID_ROLE_SCOPES[role]:
        raise ValueError(f"{role!r} cannot be granted at scope_type={scope_type!r}. Valid scopes for this role: {sorted(VALID_ROLE_SCOPES[role])}.")


# Defaults for the TTLs proposed in the design doc; overridable via
# env vars (see get_ttl_config()) so a deployment can tune them without
# a code change -- but bounded, so a misconfigured value can never
# create effectively permanent authentication.
ASSERTION_TTL_MINUTES_DEFAULT = 15
SESSION_TTL_HOURS_DEFAULT = 8
OFFLINE_GRACE_HOURS_DEFAULT = 72

# (env var, default, valid inclusive range) -- the range is the actual
# safety control: an operator can raise or lower these, but never past
# a bound that would make a credential effectively never expire.
_TTL_BOUNDS = {
    "session_ttl_hours": ("ANYAICAM_SESSION_TTL_HOURS", SESSION_TTL_HOURS_DEFAULT, (1, 24 * 7)),        # 1 hour .. 7 days
    "offline_grace_hours": ("ANYAICAM_OFFLINE_GRACE_HOURS", OFFLINE_GRACE_HOURS_DEFAULT, (1, 24 * 30)),  # 1 hour .. 30 days
    "assertion_ttl_minutes": ("ANYAICAM_ASSERTION_TTL_MINUTES", ASSERTION_TTL_MINUTES_DEFAULT, (1, 60)), # 1 .. 60 minutes
}


def get_ttl_config() -> dict[str, int]:
    """Reads the three TTLs from their env vars, falling back to the
    safe default -- and re-falling-back to it, with a logged warning,
    for anything unparseable or outside its valid range -- rather than
    trusting an operator-supplied value that could otherwise make a
    session/assertion effectively permanent. Called fresh on every use
    (no caching) so a config change takes effect on the next request,
    not only at process start."""
    config: dict[str, int] = {}
    for key, (env_var, default, (low, high)) in _TTL_BOUNDS.items():
        raw = os.environ.get(env_var, "").strip()
        if not raw:
            config[key] = default
            continue
        try:
            value = int(raw)
        except ValueError:
            logger.warning("%s=%r is not an integer; using default %s.", env_var, raw, default)
            config[key] = default
            continue
        if not (low <= value <= high):
            logger.warning("%s=%s is outside the valid range [%s, %s]; using default %s.", env_var, value, low, high, default)
            config[key] = default
            continue
        config[key] = value
    return config


class ManifestError(ValueError):
    """Raised by verify_* functions -- callers turn this into a hard
    refusal to trust the payload, never a partial/best-effort trust."""


class CloudIdentityUnavailable(RuntimeError):
    """Raised by a CloudIdentityBackend when the cloud genuinely cannot
    be reached (network failure, timeout) -- distinct from a normal
    "denied" authentication result. Design doc §4: an already-
    established local session may keep working through its offline
    grace period regardless (that's the existing session/cookie
    mechanism, unaffected), but a brand-new login attempt must never be
    silently approved, retried against a local cache, or otherwise
    trusted when this is raised -- the caller (portal_login_submit())
    surfaces it as an honest "cloud unavailable" refusal. Never raised
    by MockCloudIdentityBackend in normal operation (there is no real
    network hop to fail in dev); tests simulate it directly to prove
    the refusal path, and a real AwsCloudIdentityBackend raises it on
    an actual connection/timeout failure."""


# =============================================================== pure: scope resolution


def grant_resolves(*, scope_type: str, scope_id: str | None, partner_id: str | None, customer_id: str | None, site_id: str | None, cloud_id: str | None) -> bool:
    """The single choke point: does this grant apply to an appliance
    with the given partner/customer/site/cloud_id? A 'partner' scope
    resolving to every appliance under that partner is the one
    deliberate case of broad reach in this model -- every other scope
    type is an exact, single-id match, never inferred."""
    if scope_type == "global":
        return True
    if scope_type == "partner":
        return bool(partner_id) and scope_id == partner_id
    if scope_type == "customer":
        return bool(customer_id) and scope_id == customer_id
    if scope_type == "site":
        return bool(site_id) and scope_id == site_id
    if scope_type == "appliance":
        return bool(cloud_id) and scope_id == cloud_id
    return False


def build_manifest_identities(user_rows: list[dict], grants_by_user: dict[str, list[dict]], *, partner_id: str | None, customer_id: str | None, site_id: str | None, cloud_id: str | None) -> list[dict]:
    """Pure: given already-loaded partner_users rows and their grants
    (both DB-free inputs), return the identities array for a manifest
    scoped to one appliance. A user with zero resolving grants is
    omitted entirely -- never included with an empty grants list."""
    identities: list[dict] = []
    for user in user_rows:
        grants = grants_by_user.get(user["id"], [])
        resolving = [
            {"role": grant["role"], "scope_type": grant["scope_type"], "scope_id": grant["scope_id"]}
            for grant in grants
            if not grant.get("revoked_at")
            and grant_resolves(scope_type=grant["scope_type"], scope_id=grant.get("scope_id"), partner_id=partner_id, customer_id=customer_id, site_id=site_id, cloud_id=cloud_id)
        ]
        if not resolving:
            continue
        identities.append({
            "user_id": user["id"],
            "email": user["email"],
            "grants": resolving,
            "enabled": bool(user.get("approved", 1)) and str(user.get("account_status") or "active").lower() not in {"suspended", "revoked"},
            "revoked_at": user.get("revoked_at"),
            "authorization_version": int(user.get("authorization_version", 1)),
        })
    return identities


def should_refresh_manifest(cached_version: int | None, current_version: int) -> bool:
    """Deterministic (integer comparison, never a timestamp/clock-based
    guess) staleness check. None (an appliance that has never cached a
    version -- e.g. right after activation) always refreshes."""
    return cached_version is None or cached_version != current_version


def manifest_version_for(identities: list[dict]) -> int:
    """Derived, not stored -- the highest authorization_version among
    the identities actually included is, by definition, the version at
    which this manifest's content could last have changed. No separate
    counter to keep in sync."""
    return max((identity["authorization_version"] for identity in identities), default=0)


def portal_bucket_matches(role: str, portal: str | None) -> bool:
    if portal not in PORTAL_BUCKETS:
        return False
    return role in PORTAL_BUCKETS[portal]


# =============================================================== pure: canonical JSON + signing


def canonical_json(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_body(body: dict, *, key_id: str, private_key_b64: str) -> dict:
    private_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    signature = private_key.sign(canonical_json(body))
    return {"alg": "Ed25519", "key_id": key_id, "value": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")}


def verify_signed_body(body: dict, signature: dict, *, public_keys: dict[str, str]) -> None:
    """Raises ManifestError on any failure -- callers must never catch
    this and fall through to trusting the body anyway."""
    if not signature or signature.get("alg") != "Ed25519":
        raise ManifestError("Unsupported or missing signature algorithm.")
    key_id = signature.get("key_id")
    public_key_b64 = public_keys.get(key_id)
    if not public_key_b64:
        raise ManifestError(f"Unknown signing key: {key_id!r}.")
    try:
        padded = signature["value"] + "=" * (-len(signature["value"]) % 4)
        raw_signature = base64.urlsafe_b64decode(padded)
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        public_key.verify(raw_signature, canonical_json(body))
    except (InvalidSignature, ValueError, KeyError) as error:
        raise ManifestError("Signature verification failed.") from error


def verify_manifest(manifest: dict, *, expected_cloud_id: str, public_keys: dict[str, str], now: datetime | None = None) -> None:
    """The four-step check from the design doc, in order, fail-closed.
    Raises ManifestError on the first failure."""
    now = now or datetime.now()
    body = {key: value for key, value in manifest.items() if key != "signature"}
    verify_signed_body(body, manifest.get("signature", {}), public_keys=public_keys)
    expires_at = manifest.get("expires_at")
    if not expires_at or datetime.fromisoformat(expires_at) <= now:
        raise ManifestError("Manifest has expired.")
    if manifest.get("appliance", {}).get("cloud_id") != expected_cloud_id:
        raise ManifestError("Manifest was issued for a different appliance.")


def verify_assertion(assertion_envelope: dict, *, expected_cloud_id: str, public_keys: dict[str, str], now: datetime | None = None) -> None:
    now = now or datetime.now()
    body = {key: value for key, value in assertion_envelope.items() if key != "signature"}
    verify_signed_body(body, assertion_envelope.get("signature", {}), public_keys=public_keys)
    assertion = assertion_envelope.get("assertion", {})
    expires_at = assertion.get("expires_at")
    if not expires_at or datetime.fromisoformat(expires_at) <= now:
        raise ManifestError("Assertion has expired.")
    if assertion.get("cloud_id") != expected_cloud_id:
        raise ManifestError("Assertion was issued for a different appliance.")


# =============================================================== pure: local session reconciliation


def sessions_to_revoke(local_sessions: list[dict], manifest_identities: list[dict]) -> list[str]:
    """Pure: given the appliance's own cached local session records
    (each {"session_id","user_id","role","authorization_version"}) and
    the identities array from a freshly-verified manifest, return the
    session_ids that must be force-expired immediately.

    A session is revoked when its user has dropped out of the manifest
    entirely (revoked/unassigned/out of scope for this appliance), is no
    longer enabled, or -- the specific case that matters once one user
    can hold more than one simultaneous role-scoped grant, each with its
    own session -- when THAT SESSION's own role no longer has a live,
    resolving grant. authorization_version is per-user, not per-grant
    (see partner_users.authorization_version's own column comment), so
    comparing it directly would revoke every session a user has the
    instant *any one* of their grants changes, including a completely
    unrelated one -- e.g. revoking a Partner grant must never also
    revoke that same person's still-valid Administrator session. Role
    presence is the precise signal; authorization_version is only
    consulted as a fallback for a legacy session that never recorded
    which role it was established under."""
    by_user = {identity["user_id"]: identity for identity in manifest_identities}
    revoke: list[str] = []
    for session in local_sessions:
        identity = by_user.get(session["user_id"])
        if not identity or not identity["enabled"] or identity.get("revoked_at"):
            revoke.append(session["session_id"])
            continue
        session_role = session.get("role")
        if session_role is not None:
            current_roles = {grant["role"] for grant in identity["grants"]}
            if session_role not in current_roles:
                revoke.append(session["session_id"])
            continue
        if int(session.get("authorization_version", -1)) != identity["authorization_version"]:
            revoke.append(session["session_id"])
    return revoke


def session_within_offline_grace(*, last_verified_at: datetime, now: datetime, grace_hours: int | None = None) -> bool:
    if grace_hours is None:
        grace_hours = get_ttl_config()["offline_grace_hours"]
    return now - last_verified_at <= timedelta(hours=grace_hours)


# =============================================================== DB-touching wrappers


def ensure_signing_key(db) -> dict:
    """Get-or-create the active signing key -- idempotent, matching
    partner_db.bootstrap_admin()'s own convention. v1/dev only: see this
    module's docstring on why the private key lives in this table."""
    existing = db.execute("SELECT key_id,public_key_b64,private_key_b64 FROM identity_signing_keys WHERE revoked_at IS NULL ORDER BY created_at DESC LIMIT 1").fetchone()
    if existing:
        return dict(existing)
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_b64 = base64.b64encode(private_key.private_bytes_raw()).decode("ascii")
    public_b64 = base64.b64encode(public_key.public_bytes_raw()).decode("ascii")
    key_id = f"anyaicam-identity-{secrets.token_hex(6)}"
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO identity_signing_keys(key_id,public_key_b64,private_key_b64,created_at) VALUES(?,?,?,?)",
        (key_id, public_b64, private_b64, now),
    )
    return {"key_id": key_id, "public_key_b64": public_b64, "private_key_b64": private_b64}


def active_public_keys(db) -> dict[str, str]:
    rows = db.execute("SELECT key_id,public_key_b64 FROM identity_signing_keys WHERE revoked_at IS NULL").fetchall()
    return {row["key_id"]: row["public_key_b64"] for row in rows}


def has_global_administrator_grant(db, *, email: str) -> bool:
    """Live re-check, never a cached claim: does this email currently
    hold a non-revoked identity_grants row with role='administrator'
    and scope_type='global'? This is the one check that lets a cloud-
    delegated Partner Portal session bridge into legacy Admin Portal
    access (see main.py's cloud_administrator_bridge()) -- deliberately
    excludes scope_type='partner' (a company-level administrator) so a
    partner-scoped admin can never silently gain global AnyAiCam reach.
    Called fresh on every current_user() resolution for a bridged
    session, so revoking the grant removes Admin Portal access on the
    very next request, not only after the appliance's own manifest
    reconciliation cycle."""
    user = db.execute("SELECT id FROM partner_users WHERE lower(email)=?", (email.strip().lower(),)).fetchone()
    if not user:
        return False
    grant = db.execute(
        "SELECT id FROM identity_grants WHERE user_id=? AND role='administrator' AND scope_type='global' AND revoked_at IS NULL",
        (user["id"],),
    ).fetchone()
    return grant is not None


def create_grant(db, *, user_id: str, role: str, scope_type: str, scope_id: str | None, granted_by: str, now: str | None = None) -> str:
    if scope_type not in SCOPE_TYPES:
        raise ValueError(f"Unknown scope_type: {scope_type!r}")
    if scope_type != "global" and not scope_id:
        raise ValueError(f"scope_id is required for scope_type={scope_type!r}")
    validate_grant_role_scope(role, scope_type)
    now = now or datetime.now().isoformat()
    grant_id = secrets.token_hex(8)
    db.execute(
        "INSERT INTO identity_grants(id,user_id,role,scope_type,scope_id,granted_at,granted_by,revoked_at) VALUES(?,?,?,?,?,?,?,NULL)",
        (grant_id, user_id, role, scope_type, scope_id, now, granted_by),
    )
    bump_authorization_version(db, user_id)
    return grant_id


def revoke_grant(db, *, grant_id: str, now: str | None = None) -> None:
    now = now or datetime.now().isoformat()
    row = db.execute("SELECT user_id FROM identity_grants WHERE id=?", (grant_id,)).fetchone()
    db.execute("UPDATE identity_grants SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (now, grant_id))
    if row:
        bump_authorization_version(db, row["user_id"])


def bump_authorization_version(db, user_id: str) -> None:
    db.execute("UPDATE partner_users SET authorization_version=authorization_version+1 WHERE id=?", (user_id,))


def _appliance_scope(db, *, cloud_id: str) -> dict | None:
    row = db.execute("SELECT cloud_id,partner_id,customer_id,site_id FROM appliances WHERE cloud_id=?", (cloud_id,)).fetchone()
    return dict(row) if row else None


def reconcile_sessions_against_manifest(db, *, manifest: dict) -> list[str]:
    """DB-touching wrapper around sessions_to_revoke(): walks every
    still-open user_sessions row (revoked_at IS NULL) whose user_id
    appears anywhere in this manifest's identities (scoped, by
    construction, to one appliance's own partner/customer/site), and
    force-expires (revoked_at=now) any that no longer resolve. Returns
    the list of session ids just revoked, so a caller can log/audit
    them. Call this whenever a fresh, verified manifest is fetched --
    see this module's own docstring on where that's wired today and
    what's still a manual/follow-up trigger."""
    identities = manifest.get("identities", [])
    # Deliberately not filtered to just this manifest's user_ids: a user
    # who dropped OUT of the manifest entirely (role removed, revoked,
    # unassigned) is exactly the case sessions_to_revoke() must catch,
    # and an IN-list built from the manifest's own identities would
    # silently exclude them.
    open_sessions = db.execute("SELECT id,user_id,role,authorization_version_at_login FROM user_sessions WHERE revoked_at IS NULL").fetchall()
    local_sessions = [
        {"session_id": row["id"], "user_id": row["user_id"], "role": row["role"], "authorization_version": row["authorization_version_at_login"]}
        for row in open_sessions
    ]
    to_revoke = sessions_to_revoke(local_sessions, identities)
    if to_revoke:
        now = datetime.now().isoformat()
        placeholders = ",".join("?" for _ in to_revoke)
        db.execute(f"UPDATE user_sessions SET revoked_at=? WHERE id IN ({placeholders})", [now, *to_revoke])
    return to_revoke


def _load_identities_for_appliance(db, *, appliance: dict) -> list[dict]:
    """Shared by build_manifest() (needs the full identities array) and
    current_manifest_version() (needs only their authorization_versions)
    so the two never compute this from two different queries that could
    drift out of sync with each other."""
    grants = db.execute("SELECT id,user_id,role,scope_type,scope_id,revoked_at FROM identity_grants").fetchall()
    grants_by_user: dict[str, list[dict]] = {}
    for grant in grants:
        grants_by_user.setdefault(grant["user_id"], []).append(dict(grant))
    user_ids = list(grants_by_user.keys())
    user_rows: list[dict] = []
    if user_ids:
        placeholders = ",".join("?" for _ in user_ids)
        user_rows = [dict(row) for row in db.execute(
            f"SELECT id,email,approved,account_status,authorization_version FROM partner_users WHERE id IN ({placeholders})", user_ids,
        ).fetchall()]
    return build_manifest_identities(
        user_rows, grants_by_user,
        partner_id=appliance["partner_id"], customer_id=appliance["customer_id"], site_id=appliance["site_id"], cloud_id=appliance["cloud_id"],
    )


def build_manifest(db, *, cloud_id: str, ttl_minutes: int = 60) -> dict:
    """DB-touching wrapper: resolves the appliance's own scope, loads
    every non-revoked grant plus its user row, delegates to the pure
    build_manifest_identities()/manifest_version_for(), and signs the
    result. Raises ValueError if cloud_id doesn't match a real,
    activated appliance -- never returns a manifest for one that
    doesn't exist."""
    appliance = _appliance_scope(db, cloud_id=cloud_id)
    if not appliance:
        raise ValueError(f"No activated appliance with cloud_id={cloud_id!r}.")
    identities = _load_identities_for_appliance(db, appliance=appliance)
    now = datetime.now()
    body = {
        "manifest_version": manifest_version_for(identities),
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=ttl_minutes)).isoformat(),
        "appliance": {"cloud_id": appliance["cloud_id"], "partner_id": appliance["partner_id"], "customer_id": appliance["customer_id"], "site_id": appliance["site_id"]},
        "identities": identities,
    }
    key = ensure_signing_key(db)
    body["signature"] = sign_body(body, key_id=key["key_id"], private_key_b64=key["private_key_b64"])
    return body


def current_manifest_version(db, *, cloud_id: str) -> int:
    """Cheap version check -- no signing, no full identities payload --
    for a heartbeat ACK to carry so the caller can detect staleness
    without fetching (and an appliance without decrypting/verifying)
    a full manifest every cycle. Returns 0 for an unknown cloud_id
    rather than raising -- a heartbeat from an appliance mid-activation
    should never hard-fail on this."""
    appliance = _appliance_scope(db, cloud_id=cloud_id)
    if not appliance:
        return 0
    return manifest_version_for(_load_identities_for_appliance(db, appliance=appliance))


def authenticate_operator(db, *, email: str, password: str, portal: str, cloud_id: str, ttl_minutes: int | None = None) -> dict:
    """DB-touching: verifies the password (via partner_db.verify_password,
    imported at call time to avoid a module-load cycle), then requires a
    live grant that BOTH matches the requested portal bucket AND
    resolves to this specific appliance's scope -- a correct password
    alone is never sufficient. Returns a signed assertion envelope on
    success, or {"status":"denied","reason":...} -- never raises for an
    ordinary authentication failure, only for a structurally invalid
    request (unknown cloud_id). ttl_minutes defaults to the configured
    (env-overridable, range-validated) assertion TTL -- see
    get_ttl_config() -- read fresh on every call, not frozen at import
    time, so a config change takes effect immediately."""
    if ttl_minutes is None:
        ttl_minutes = get_ttl_config()["assertion_ttl_minutes"]
    from partner_db import verify_password

    appliance = _appliance_scope(db, cloud_id=cloud_id)
    if not appliance:
        raise ValueError(f"No activated appliance with cloud_id={cloud_id!r}.")
    user = db.execute(
        "SELECT id,email,role,password_hash,approved,account_status,authorization_version FROM partner_users WHERE lower(email)=?",
        (email.strip().lower(),),
    ).fetchone()
    if not user or not verify_password(password, user["password_hash"]):
        return {"status": "denied", "reason": "invalid"}
    user = dict(user)
    if str(user.get("account_status") or "active").lower() in {"suspended", "revoked"}:
        return {"status": "denied", "reason": user["account_status"].lower()}
    if not user.get("approved"):
        return {"status": "denied", "reason": "pending"}

    grants = [dict(row) for row in db.execute(
        "SELECT role,scope_type,scope_id FROM identity_grants WHERE user_id=? AND revoked_at IS NULL", (user["id"],),
    ).fetchall()]
    matching = sorted(
        (
            grant for grant in grants
            if portal_bucket_matches(grant["role"], portal)
            and grant_resolves(scope_type=grant["scope_type"], scope_id=grant.get("scope_id"), partner_id=appliance["partner_id"], customer_id=appliance["customer_id"], site_id=appliance["site_id"], cloud_id=appliance["cloud_id"])
        ),
        key=lambda grant: SCOPE_BREADTH_ORDER.get(grant["scope_type"], len(SCOPE_BREADTH_ORDER)),
    )
    if not matching:
        # Distinguish "wrong portal for this account" from "no access to
        # this appliance at all" the same way resolve_portal_login()
        # already does for the legacy/partner_db split -- never silently
        # fall back to a different portal than what was requested.
        any_portal_grant = any(grant_resolves(scope_type=g["scope_type"], scope_id=g.get("scope_id"), partner_id=appliance["partner_id"], customer_id=appliance["customer_id"], site_id=appliance["site_id"], cloud_id=appliance["cloud_id"]) for g in grants)
        return {"status": "denied", "reason": "not_authorized_for_selected_portal" if any_portal_grant else "not_authorized_for_this_appliance"}

    grant = matching[0]  # broadest scope first -- see SCOPE_BREADTH_ORDER's own comment
    now = datetime.now()
    assertion = {
        "user_id": user["id"], "email": user["email"], "role": grant["role"],
        "scope_type": grant["scope_type"], "scope_id": grant.get("scope_id"),
        "cloud_id": appliance["cloud_id"],
        "authorization_version": int(user["authorization_version"]),
        "issued_at": now.isoformat(), "expires_at": (now + timedelta(minutes=ttl_minutes)).isoformat(),
    }
    key = ensure_signing_key(db)
    envelope = {"status": "ok", "assertion": assertion}
    # Sign the exact same shape verify_assertion() reconstructs (every
    # field except "signature" itself) -- signing a narrower body than
    # what gets verified would make every real assertion fail its own
    # signature check.
    envelope["signature"] = sign_body(envelope, key_id=key["key_id"], private_key_b64=key["private_key_b64"])
    return envelope


# =============================================================== the cloud-identity backend seam


class CloudIdentityBackend:
    """Mirrors provisioning_service.py's ProvisioningBackend seam: plain
    duck-typing so MockCloudIdentityBackend and a future real backend
    (calling AWS over HTTPS with the appliance's own permanent
    credential) share one interface the rest of the app calls through."""

    def fetch_manifest(self, *, cloud_id: str) -> dict: raise NotImplementedError

    def authenticate_operator(self, *, email: str, password: str, portal: str, cloud_id: str) -> dict: raise NotImplementedError

    def public_keys(self) -> dict[str, str]: raise NotImplementedError


class MockCloudIdentityBackend(CloudIdentityBackend):
    """v1/dev implementation: the 'cloud' and the 'appliance' are the
    same process and the same partner_db today, so this calls the
    DB-touching functions above directly (in-process) rather than over
    HTTP -- exactly like provisioning_service.MockProvisioningBackend
    already does for Cloud ID issuance. A real AwsCloudIdentityBackend
    would implement this same interface over a genuine network call
    with the appliance's permanent credential in the Authorization
    header; nothing above this class needs to change when that lands."""

    def __init__(self, connection_factory):
        self._connection_factory = connection_factory

    def fetch_manifest(self, *, cloud_id: str) -> dict:
        with self._connection_factory() as db:
            return build_manifest(db, cloud_id=cloud_id)

    def authenticate_operator(self, *, email: str, password: str, portal: str, cloud_id: str) -> dict:
        with self._connection_factory() as db:
            return authenticate_operator(db, email=email, password=password, portal=portal, cloud_id=cloud_id)

    def public_keys(self) -> dict[str, str]:
        with self._connection_factory() as db:
            return active_public_keys(db)


_backend_instance: CloudIdentityBackend | None = None


def get_cloud_identity_backend() -> CloudIdentityBackend:
    global _backend_instance
    if _backend_instance is None:
        from partner_db import connection
        _backend_instance = MockCloudIdentityBackend(connection)
    return _backend_instance


def reset_cloud_identity_backend_for_tests() -> None:
    global _backend_instance
    _backend_instance = None
