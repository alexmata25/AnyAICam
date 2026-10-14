"""Local customer access on a claimed edge appliance (1.2.6, D-G6).

Problem: a customer could not sign in on their own appliance at all. The
edge's customer sign-in (POST /api/partner-login) only checked the edge's
local partner_users table, which never holds customer accounts, and the
cloud-delegated operator login (POST /api/portal-login) only knew the
administrator/partner/technician portals. A Local-plan customer therefore had
no way to watch the recordings that exist only on the appliance.

This module reuses the existing appliance identity contract
(appliance_identity.py) instead of adding a second identity system:

* Online sign-in: the appliance forwards the customer's email and password,
  authenticated with its OWN appliance credential, to the cloud's existing
  POST /api/appliance/{cloud_id}/authenticate-operator route with the
  'customer' portal. The cloud returns an Ed25519-signed assertion that is
  verified against the installer-provisioned trust anchor
  (/etc/anyaicam-update/entitlement_signing_keys.json -- never keys from the
  network), must be for this appliance's Cloud ID, must carry a
  customer_owner grant for this appliance's own customer, and is then
  turned into an ordinary partner session. The password is never stored.

* Offline sign-in: the owner sets a separate *appliance password* while
  online (after re-confirming their cloud password, which it must differ
  from). Only its salted hash is stored, here, bound to the cloud user id,
  the appliance's customer and the user's authorization_version. It works
  without internet while it is (a) not revoked, (b) bound to the customer
  this appliance currently belongs to, and (c) re-confirmed by a verified
  cloud manifest or online sign-in within the offline grace window
  (ANYAICAM_OFFLINE_GRACE_HOURS, the identity contract's existing setting).

* Revocation: every verified manifest (periodic worker, online sign-in)
  revokes the appliance password and open sessions of a user whose customer
  grant was revoked, whose account was disabled, or whose authorization
  version changed (a grant change or a cloud password reset). A re-claim to a
  different customer invalidates it by the customer binding. The owner can
  turn it off; setting it again replaces it.

Scope (conservative, see the acceptance report): only the account owner
(customer_owner) gets local access. Household viewers' per-camera
permissions live in the cloud and are not synced to the appliance, so they
are refused locally rather than shown cameras they may not be allowed to see.
Technician/administrator sign-in on the appliance is unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from fastapi import Request  # module level: route annotations are strings (postponed evaluation)

logger = logging.getLogger("anyaicam.local_customer_access")

LOCAL_ROLES = {"customer_owner"}
CUSTOMER_PORTAL = "customer"
MIN_PASSWORD_LENGTH = 12
REQUEST_TIMEOUT_SECONDS = 8
RECONCILE_INTERVAL_SECONDS = int(os.environ.get("ANYAICAM_LOCAL_ACCESS_RECONCILE_SECONDS", "900") or 900)


OFFLINE_REFUSED = ("AnyAiCam can't be reached right now, and the email or appliance password is incorrect. Offline, sign in with "
                   "the appliance password you set up under Appliance sign-in while this appliance was online.")


class CloudUnavailable(Exception):
    """The cloud could not be reached (or answered with a server error)."""


class AccessDenied(Exception):
    def __init__(self, status: int, detail: str, reason: str = "denied"):
        super().__init__(detail)
        self.status, self.detail, self.reason = status, detail, reason


# ------------------------------------------------------------------ appliance side helpers
def appliance_identity() -> dict | None:
    from appliance_activation import load_persisted_identity
    return load_persisted_identity()


def _cloud_url() -> str:
    return os.environ.get("ANYAICAM_CLOUD_URL", "").strip().rstrip("/")


def _trusted_keys() -> dict[str, str]:
    import appliance_entitlements
    return appliance_entitlements._trusted_keys()


def _request(method: str, path: str, identity: dict, payload: dict | None = None) -> dict:
    base = _cloud_url()
    if not base:
        raise CloudUnavailable("no cloud URL configured")
    headers = {
        "User-Agent": "AnyAiCam-LocalCustomerAccess/1",
        "Authorization": f"Bearer {identity['credential']}",
        "X-Appliance-ID": identity["appliance_id"],
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_urlsafe(18),
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        if error.code >= 500:
            raise CloudUnavailable(f"HTTP {error.code}") from error
        raise AccessDenied(403, "This appliance is not authorized by AnyAiCam right now.", "appliance_rejected") from error
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        raise CloudUnavailable(type(error).__name__) from error


def _assertion_is_for_this_customer(assertion: dict, identity: dict) -> bool:
    return (assertion.get("role") in LOCAL_ROLES and assertion.get("scope_type") == "customer"
            and assertion.get("scope_id") == identity.get("customer_id") and bool(identity.get("customer_id")))


def delegated_authenticate(email: str, password: str, identity: dict) -> dict:
    """Online customer sign-in through the cloud. Returns the verified
    assertion dict; raises AccessDenied or CloudUnavailable."""
    from appliance_identity import ManifestError, verify_assertion
    result = _request("POST", f"/api/appliance/{identity['cloud_id']}/authenticate-operator", identity,
                      {"email": email, "password": password, "portal": CUSTOMER_PORTAL})
    if result.get("status") != "ok":
        raise AccessDenied(403, _denied_message(result.get("reason")), str(result.get("reason") or "denied"))
    keys = _trusted_keys()
    if not keys:
        raise AccessDenied(503, "This appliance has no AnyAiCam trust anchor installed; sign-in is unavailable.", "no_trust_anchor")
    try:
        verify_assertion(result, expected_cloud_id=identity["cloud_id"], public_keys=keys)
    except (ManifestError, ValueError) as error:
        logger.warning("local_customer_access.assertion_rejected error=%s", error)
        raise AccessDenied(403, "AnyAiCam's answer could not be verified by this appliance.", "invalid_assertion") from error
    assertion = result["assertion"]
    if assertion.get("email", "").lower() != email.strip().lower() or not _assertion_is_for_this_customer(assertion, identity):
        raise AccessDenied(403, "This account is not the owner of this appliance.", "not_owner")
    return assertion


def _denied_message(reason) -> str:
    return {
        "invalid": "The email or password is incorrect.",
        "suspended": "This account is suspended. Contact AnyAiCam support.",
        "revoked": "This account has been revoked. Contact AnyAiCam support.",
        "pending": "This account is awaiting approval.",
        "not_authorized_for_this_appliance": "This account does not have access to this appliance.",
        "not_authorized_for_selected_portal": "Only the account owner can sign in on the appliance itself. "
                                              "Household members use the AnyAiCam app or portal.",
    }.get(str(reason or ""), "Sign-in was refused.")


def fetch_verified_manifest(identity: dict) -> dict:
    from appliance_identity import ManifestError, verify_manifest
    manifest = _request("GET", f"/api/appliance/{identity['cloud_id']}/identity-manifest", identity)
    keys = _trusted_keys()
    if not keys:
        raise CloudUnavailable("no trust anchor")
    try:
        verify_manifest(manifest, expected_cloud_id=identity["cloud_id"], public_keys=keys)
    except (ManifestError, ValueError) as error:
        logger.warning("local_customer_access.manifest_rejected error=%s", error)
        raise CloudUnavailable("manifest rejected") from error
    if (manifest.get("appliance") or {}).get("customer_id") != identity.get("customer_id"):
        raise CloudUnavailable("manifest is for a different customer")
    return manifest


def authorized_in_manifest(manifest: dict, *, user_id: str, authorization_version: int, customer_id: str) -> bool:
    for item in manifest.get("identities", []):
        if item.get("user_id") != user_id:
            continue
        return (bool(item.get("enabled")) and not item.get("revoked_at")
                and int(item.get("authorization_version", -1)) == int(authorization_version)
                and any(g.get("role") in LOCAL_ROLES and g.get("scope_type") == "customer" and g.get("scope_id") == customer_id
                        for g in item.get("grants", [])))
    return False


# ------------------------------------------------------------------ local credential store
def _row(email: str | None = None, user_id: str | None = None) -> dict | None:
    from partner_db import connection
    with connection() as db:
        if user_id:
            row = db.execute("SELECT * FROM local_customer_credentials WHERE user_id=?", (user_id,)).fetchone()
        else:
            row = db.execute("SELECT * FROM local_customer_credentials WHERE lower(email)=?", ((email or "").strip().lower(),)).fetchone()
        return dict(row) if row else None


def _revoke(user_ids: list[str], reason: str) -> int:
    if not user_ids:
        return 0
    from partner_db import connection
    now = datetime.now().isoformat()
    marks = ",".join("?" for _ in user_ids)
    with connection() as db:
        changed = db.execute(f"UPDATE local_customer_credentials SET revoked_at=?,revoked_reason=? WHERE revoked_at IS NULL AND user_id IN ({marks})",
                             (now, reason, *user_ids)).rowcount
        db.execute(f"UPDATE user_sessions SET revoked_at=? WHERE revoked_at IS NULL AND user_id IN ({marks})", (now, *user_ids))
    _disable_local_accounts(user_ids)
    if changed:
        logger.warning("local_customer_access.revoked count=%s reason=%s", changed, reason)
    return changed


def _mark_verified(user_id: str, authorization_version: int) -> None:
    from partner_db import connection
    with connection() as db:
        db.execute("UPDATE local_customer_credentials SET last_verified_at=? WHERE user_id=? AND revoked_at IS NULL AND authorization_version=?",
                   (datetime.now().isoformat(), user_id, int(authorization_version)))


def _within_grace(last_verified_at: str | None) -> bool:
    from appliance_identity import session_within_offline_grace
    if not last_verified_at:
        return False
    try:
        return session_within_offline_grace(last_verified_at=datetime.fromisoformat(last_verified_at), now=datetime.now())
    except ValueError:
        return False


def _usable(credential: dict | None, identity: dict) -> bool:
    return bool(credential and not credential.get("revoked_at") and credential.get("customer_id") == identity.get("customer_id"))


def reconcile(manifest: dict, identity: dict) -> dict:
    """Apply a verified manifest: revoke what is no longer authorized and
    re-confirm (refresh last_verified_at) what still is."""
    from partner_db import connection
    with connection() as db:
        rows = [dict(r) for r in db.execute("SELECT user_id,customer_id,authorization_version FROM local_customer_credentials WHERE revoked_at IS NULL")]
    stale, changed = [], []
    for r in rows:
        if r["customer_id"] == identity.get("customer_id") and authorized_in_manifest(
                manifest, user_id=r["user_id"], authorization_version=r["authorization_version"], customer_id=r["customer_id"]):
            continue
        current = next((i for i in manifest.get("identities", []) if i.get("user_id") == r["user_id"]), None)
        still_owner = (r["customer_id"] == identity.get("customer_id") and current is not None
                       and authorized_in_manifest(manifest, user_id=r["user_id"],
                                                  authorization_version=int(current.get("authorization_version", -1)),
                                                  customer_id=r["customer_id"]))
        (changed if still_owner else stale).append(r["user_id"])
    # Still the owner, only the authorization version moved (e.g. a password
    # reset): say so, so the owner knows to set a new appliance password.
    revoked = _revoke(changed, "authorization_changed") + _revoke(stale, "no_longer_authorized")
    stale = stale + changed
    for r in rows:
        if r["user_id"] not in stale:
            _mark_verified(r["user_id"], r["authorization_version"])
    return {"revoked": revoked, "confirmed": len(rows) - len(stale)}


# ------------------------------------------------------------------ local account cache
def ensure_local_account(user_id: str, email: str, customer_id: str, role: str) -> None:
    """The customer pages on the appliance read the signed-in customer from
    partner_users (by email). Keep a cached row for the owner, with a random,
    never-disclosed password hash so it can never be used to sign in by itself
    -- the appliance never holds a usable copy of any customer password here
    (docs/blockers-before-universal-release.md: local data only as a controlled
    cache). Refreshed on every verified sign-in; disabled on revocation."""
    from partner_db import connection, password_hash
    unusable = password_hash(secrets.token_urlsafe(32))
    now = datetime.now().isoformat()
    with connection() as db:
        existing = db.execute("SELECT id,role FROM partner_users WHERE lower(email)=?", (email.lower(),)).fetchone()
        if existing and existing["role"] not in ("customer_owner", "customer_viewer"):
            # Never turn a local operator account (e.g. the appliance's own
            # administrator) into a customer, or sign a customer in as one.
            raise AccessDenied(409, "This email is also an administrator account on this appliance. "
                                    "Use a different email for the customer account.", "operator_email_conflict")
        if existing:
            db.execute("UPDATE partner_users SET role=?,customer_id=?,partner_id=NULL,approved=1,account_status='active',"
                       "password_hash=?,must_change_password=0 WHERE id=?", (role, customer_id, unusable, existing["id"]))
        else:
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
                       "VALUES(?,NULL,?,?,?,?,1,?,?)", (user_id, email.lower(), email.split("@")[0], role, unusable, customer_id, now))


def _disable_local_accounts(user_ids: list[str]) -> None:
    from partner_db import connection
    if not user_ids:
        return
    marks = ",".join("?" for _ in user_ids)
    with connection() as db:
        db.execute(f"UPDATE partner_users SET approved=0,account_status='revoked' WHERE id IN ({marks}) "
                   f"OR lower(email) IN (SELECT lower(email) FROM local_customer_credentials WHERE user_id IN ({marks}))",
                   (*user_ids, *user_ids))


# ------------------------------------------------------------------ sign-in
def sign_in(email: str, password: str) -> dict:
    """Customer sign-in on this appliance. Returns
    {'user': {...}, 'role', 'authorization_version', 'mode'}; raises AccessDenied."""
    from partner_db import verify_password
    identity = appliance_identity()
    if not identity:
        raise AccessDenied(403, "This appliance is not linked to an AnyAiCam account yet. "
                                "Link it from the AnyAiCam portal, or ask your installer.", "not_claimed")
    email = email.strip().lower()
    try:
        assertion = delegated_authenticate(email, password, identity)
    except CloudUnavailable:
        credential = _row(email=email)
        if (_usable(credential, identity) and verify_password(password, credential["password_hash"])
                and _within_grace(credential.get("last_verified_at"))):
            return _result(credential["user_id"], email, identity, credential["authorization_version"], "offline")
        if (_usable(credential, identity) and verify_password(password, credential["password_hash"])
                and not _within_grace(credential.get("last_verified_at"))):
            raise AccessDenied(503, "This appliance has not been able to reach AnyAiCam for too long to allow offline sign-in. "
                                    "Reconnect it to the internet and try again.", "offline_grace_expired")
        # One answer for every other offline failure (unknown email, no appliance
        # password, wrong password): never reveal which accounts have one.
        raise AccessDenied(503, OFFLINE_REFUSED, "invalid_offline")
    except AccessDenied as denied:
        if denied.reason != "invalid":
            raise
        # The cloud refused this password. It may be the appliance password:
        # accept it only if the cloud's signed manifest still authorizes the user.
        credential = _row(email=email)
        if not (_usable(credential, identity) and verify_password(password, credential["password_hash"])):
            raise
        try:
            manifest = fetch_verified_manifest(identity)
        except CloudUnavailable:
            if _within_grace(credential.get("last_verified_at")):
                return _result(credential["user_id"], email, identity, credential["authorization_version"], "appliance_password")
            raise
        reconcile(manifest, identity)
        if not _usable(_row(user_id=credential["user_id"]), identity):
            raise AccessDenied(403, "This account no longer has access to this appliance.", "no_longer_authorized")
        return _result(credential["user_id"], email, identity, credential["authorization_version"], "appliance_password")
    credential = _row(user_id=assertion["user_id"])
    if credential and not credential.get("revoked_at") and int(credential["authorization_version"]) != int(assertion["authorization_version"]):
        _revoke([assertion["user_id"]], "authorization_changed")
    else:
        _mark_verified(assertion["user_id"], assertion["authorization_version"])
    return _result(assertion["user_id"], email, identity, assertion["authorization_version"], "online")


def _result(user_id: str, email: str, identity: dict, authorization_version: int, mode: str) -> dict:
    ensure_local_account(user_id, email, identity["customer_id"], "customer_owner")
    logger.info("local_customer_access.sign_in mode=%s user_id=%s", mode, user_id)
    return {"user": {"id": user_id, "email": email, "customer_id": identity["customer_id"], "partner_id": None},
            "role": "customer_owner", "authorization_version": int(authorization_version), "mode": mode}


# ------------------------------------------------------------------ appliance password management
def enroll(session: dict, cloud_password: str, new_password: str, *, user_id: str) -> dict:
    """Set (or replace) the owner's appliance password. Needs the cloud:
    the owner re-confirms their AnyAiCam password, which is never stored."""
    from partner_db import connection, password_hash
    identity = appliance_identity()
    if not identity or session.get("role") not in LOCAL_ROLES or session.get("customer_id") != identity.get("customer_id"):
        raise AccessDenied(403, "Only the owner of this appliance can set an appliance password.", "not_owner")
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise AccessDenied(400, f"Use at least {MIN_PASSWORD_LENGTH} characters.", "too_short")
    if new_password == cloud_password:
        raise AccessDenied(400, "Choose a password different from your AnyAiCam account password.", "same_as_cloud")
    try:
        assertion = delegated_authenticate(session["email"], cloud_password, identity)
    except CloudUnavailable as error:
        raise AccessDenied(503, "Setting an appliance password needs the internet: reconnect the appliance and try again.",
                           "cloud_unavailable") from error
    if assertion["user_id"] != user_id:
        raise AccessDenied(403, "Sign in again and retry.", "session_mismatch")
    now = datetime.now().isoformat()
    with connection() as db:
        db.execute("DELETE FROM local_customer_credentials WHERE user_id=?", (assertion["user_id"],))
        db.execute("INSERT INTO local_customer_credentials(user_id,email,customer_id,role,password_hash,authorization_version,"
                   "enrolled_at,last_verified_at,revoked_at,revoked_reason) VALUES(?,?,?,?,?,?,?,?,NULL,NULL)",
                   (assertion["user_id"], assertion["email"].lower(), identity["customer_id"], assertion["role"],
                    password_hash(new_password), int(assertion["authorization_version"]), now, now))
    logger.info("local_customer_access.enrolled user_id=%s", assertion["user_id"])
    return {"status": "enrolled", "enrolled_at": now}


def disable(session: dict, *, user_id: str) -> dict:
    identity = appliance_identity()
    if not identity or session.get("role") not in LOCAL_ROLES or session.get("customer_id") != identity.get("customer_id"):
        raise AccessDenied(403, "Only the owner of this appliance can change appliance sign-in.", "not_owner")
    from partner_db import connection
    with connection() as db:
        changed = db.execute("UPDATE local_customer_credentials SET revoked_at=?,revoked_reason='turned_off_by_owner' "
                             "WHERE user_id=? AND revoked_at IS NULL", (datetime.now().isoformat(), user_id)).rowcount
    return {"status": "disabled" if changed else "not_enrolled"}


def status(user_id: str) -> dict:
    identity = appliance_identity() or {}
    credential = _row(user_id=user_id)
    usable = _usable(credential, identity)
    return {"enrolled": usable, "offline_ready": usable and _within_grace(credential.get("last_verified_at")),
            "last_verified_at": credential.get("last_verified_at") if credential else None,
            "revoked_reason": credential.get("revoked_reason") if credential and credential.get("revoked_at") else None}


async def reconcile_worker() -> None:
    """Periodically re-confirm appliance passwords against the cloud's
    signed manifest. Offline: nothing changes (the grace window applies)."""
    import asyncio
    while True:
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
        try:
            identity = appliance_identity()
            if identity and _row_count():
                manifest = await asyncio.to_thread(fetch_verified_manifest, identity)
                result = await asyncio.to_thread(reconcile, manifest, identity)
                logger.info("local_customer_access.reconciled revoked=%s confirmed=%s", result["revoked"], result["confirmed"])
        except CloudUnavailable as error:
            logger.info("local_customer_access.reconcile_skipped reason=%s", error)
        except AccessDenied as error:
            logger.warning("local_customer_access.reconcile_refused reason=%s", error.reason)
        except Exception:
            logger.exception("local_customer_access.reconcile_failed")


def _row_count() -> int:
    from partner_db import connection
    with connection() as db:
        return db.execute("SELECT count(*) AS n FROM local_customer_credentials WHERE revoked_at IS NULL").fetchone()["n"]


# ------------------------------------------------------------------ routes (edge only)
def register_routes(app, shell) -> None:
    from html import escape
    from fastapi import HTTPException
    from fastapi.responses import HTMLResponse, RedirectResponse
    import cloud_config
    from partner_portal import partner_identity

    def _owner(request: Request) -> tuple[dict, str]:
        if cloud_config.settings.runtime_role != "edge":
            raise HTTPException(status_code=404)
        session = partner_identity(request)
        if not session or session.get("role") not in LOCAL_ROLES:
            raise HTTPException(status_code=403, detail="Only the owner of this appliance can manage appliance sign-in.")
        from partner_db import connection
        with connection() as db:
            row = db.execute("SELECT user_id FROM user_sessions WHERE id=?", (session.get("session_id"),)).fetchone()
        if not row or not row["user_id"]:
            raise HTTPException(status_code=403, detail="Sign in again to manage appliance sign-in.")
        return session, row["user_id"]

    def _call(fn):
        try:
            return fn()
        except AccessDenied as denied:
            raise HTTPException(status_code=denied.status, detail=denied.detail) from denied

    @app.get("/api/customer/appliance-sign-in")
    def appliance_sign_in_status(request: Request) -> dict:
        _, user_id = _owner(request)
        return status(user_id)

    @app.post("/api/customer/appliance-sign-in")
    def appliance_sign_in_set(request: Request, payload: dict) -> dict:
        session, user_id = _owner(request)
        return _call(lambda: enroll(session, str(payload.get("cloud_password") or ""), str(payload.get("new_password") or ""), user_id=user_id))

    @app.post("/api/customer/appliance-sign-in/disable")
    def appliance_sign_in_disable(request: Request) -> dict:
        session, user_id = _owner(request)
        return _call(lambda: disable(session, user_id=user_id))

    @app.get("/customer/appliance-sign-in", response_class=HTMLResponse)
    def appliance_sign_in_page(request: Request):
        if cloud_config.settings.runtime_role != "edge":
            raise HTTPException(status_code=404)
        session = partner_identity(request)
        if not session:
            return RedirectResponse("/customer-login.html?next=/customer/appliance-sign-in", status_code=303)
        _, user_id = _owner(request)
        state = status(user_id)
        if state["enrolled"]:
            when = escape((state["last_verified_at"] or "")[:16].replace("T", " "))
            summary = (f'<p id="appliance-sign-in-state"><strong>On.</strong> You can sign in on this appliance with your appliance password, '
                       f'even without internet. Last confirmed with AnyAiCam: {when}.</p>')
        else:
            reason = {"no_longer_authorized": "It was turned off because your access to this appliance changed.",
                      "authorization_changed": "It was turned off because your AnyAiCam account security changed (for example a password reset).",
                      "turned_off_by_owner": "You turned it off."}.get(state.get("revoked_reason") or "", "")
            summary = f'<p id="appliance-sign-in-state"><strong>Off.</strong> {escape(reason)}</p>'
        content = f'''<header class="topbar"><div><p class="eyebrow">This appliance</p><h1>Appliance sign-in</h1></div>
<a class="ghost-button" href="/customer-portal">Back to dashboard</a></header>
<section class="panel"><h2>Sign in here without internet</h2>
<p class="health-detail">While the appliance is online you sign in here with your AnyAiCam account. Set an appliance password so you can
still sign in, watch live video and play back recordings on your local network when the internet is down. It is separate from your
AnyAiCam password and is only stored on this appliance (as a secure hash). It stays valid while this appliance can confirm your access
with AnyAiCam at least every few days, and it stops working if your access changes or your AnyAiCam password is reset.</p>
{summary}
<form id="appliance-sign-in-form" class="rule-form">
<label>Your AnyAiCam password<input id="asi-cloud" type="password" autocomplete="current-password" required></label>
<label>New appliance password<input id="asi-new" type="password" minlength="{MIN_PASSWORD_LENGTH}" autocomplete="new-password" required></label>
<label>Confirm appliance password<input id="asi-confirm" type="password" minlength="{MIN_PASSWORD_LENGTH}" autocomplete="new-password" required></label>
<button class="action-button" type="submit" id="asi-save">Save appliance password</button>
</form>
<button class="ghost-button" type="button" id="asi-off"{"" if state["enrolled"] else " hidden"}>Turn off appliance sign-in</button>
<p role="status" id="asi-message" class="health-detail"></p></section>'''
        scripts = """<script>
(()=>{const msg=document.getElementById('asi-message');
document.getElementById('appliance-sign-in-form').addEventListener('submit',async e=>{e.preventDefault();
  const n=document.getElementById('asi-new').value;if(n!==document.getElementById('asi-confirm').value){msg.textContent="The appliance passwords don't match.";return;}
  msg.textContent='Saving...';
  const r=await fetch('/api/customer/appliance-sign-in',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cloud_password:document.getElementById('asi-cloud').value,new_password:n})});
  const b=await r.json().catch(()=>({}));msg.textContent=r.ok?'Appliance password saved.':(b.detail||'Could not save.');
  if(r.ok)setTimeout(()=>location.reload(),800);});
document.getElementById('asi-off').addEventListener('click',async()=>{if(!confirm('Turn off appliance sign-in? You will need the internet to sign in here.'))return;
  const r=await fetch('/api/customer/appliance-sign-in/disable',{method:'POST'});msg.textContent=r.ok?'Turned off.':'Could not turn it off.';
  if(r.ok)setTimeout(()=>location.reload(),600);});})();
</script>"""
        return shell("Appliance sign-in", "dashboard", content, scripts)
