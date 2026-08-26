"""Admin Portal <-> Partner Portal identity bridge.

This app has always had two separate, independently-authenticated
identity systems -- see live_view_page.py's own module docstring for the
first documented instance of this split:

  - Admin Portal: current_user()/has_permission() in main.py, an
    itsdangerous-signed cookie backed by main.py's local JSON
    load_users()/load_sessions() store.
  - Partner Portal: partner_identity() in partner_portal.py, an
    HMAC-signed cookie backed by partner_db's SQL partner_users/
    user_sessions tables.

An Admin Portal session alone has never been sufficient to satisfy
partner_db.require_permission()/require_partner_access() -- so an
Admin Portal-only appliance operator had to log into the Partner Portal
a second time, in the same browser, before /operations/rdm's appliance
data or its Restart VMS / Reboot Appliance actions would work.

This module is the *decision logic* for a narrow, explicit, auditable
bridge that removes that second login for an admin who has proven, once,
that they also control a real Partner Portal account -- without ever
touching a password, without granting any admin account partner rights
it wasn't explicitly linked to, and without weakening partner_db's own
authorization checks (require_permission()/allowed() are never bypassed;
this only ever supplies the identity dict those checks are run against,
exactly like partner_identity() already does for a direct login).

Design constraints this module enforces, all fail-closed:
  - A link only ever *points at* an existing partner_users row (by id
    and email) that a real human already legitimately owns -- creating
    one never creates or edits a partner_users row, never touches
    password_hash, and never merges credentials across the two systems.
  - BRIDGEABLE_ADMIN_ROLES/BRIDGEABLE_PARTNER_ROLES exclude every
    customer-facing role on both sides. A customer account (on either
    system) can never become a link's source or target, and this is
    re-checked on every resolution, not only at link-creation time -- a
    partner_users row later demoted to customer_viewer, or an admin
    account later demoted, loses bridged access on its very next use.
  - resolve_bridged_identity() re-reads the target partner_users row
    fresh on every call (see its own docstring) -- a link is a live
    permission check, exactly like partner_identity() itself is for a
    direct login, never a one-time stamp that could outlive a
    revocation or a role change.
  - Who may *create* a link is deliberately NOT decided here -- see
    main.py's link_partner_account() route, which requires the
    requesting browser to hold a currently-valid session on *both*
    systems at once (proof of controlling both accounts) before a link
    is ever written, and audits every creation. This module only ever
    answers "is this link still valid right now" -- linking itself, and
    all its own authorization, lives at the route layer, not here.

Pure, DB/FastAPI-free decision logic (fully unit-testable) plus
DB-touching wrappers that take an explicit db connection, matching
camera_access.py's established dependency-light pattern in this
codebase.
"""
from __future__ import annotations

# Partner Portal roles a bridge may ever resolve to. Deliberately
# excludes customer_owner/customer_viewer -- see the module docstring.
BRIDGEABLE_PARTNER_ROLES = {"administrator", "partner_owner", "salesperson", "technician"}

# Legacy Admin Portal roles allowed to create or use a bridge at all.
# Deliberately excludes any customer-facing legacy role and the
# unauthenticated 'viewer' fallback current_user() returns.
BRIDGEABLE_ADMIN_ROLES = {"administrator", "admin", "support_admin"}


def can_create_link(admin_role: str, partner_role: str) -> bool:
    """The single choke point for creating a link (called by main.py's
    link_partner_account() route, itself gated on holding both live
    sessions at once -- see the module docstring). Both sides must
    already be legitimate non-customer roles at the moment of linking;
    fails closed for any unrecognized or customer role on either side."""
    return admin_role in BRIDGEABLE_ADMIN_ROLES and partner_role in BRIDGEABLE_PARTNER_ROLES


def resolve_bridged_identity(*, admin_role: str, link_row: dict | None, live_partner_user: dict | None) -> dict | None:
    """Pure decision: given a stored link row and the CURRENT
    partner_users row it points to (read fresh by the caller, never
    cached), decide whether the bridge still authorizes this admin
    session as a partner identity right now. Every input is
    independently re-validated on every call:

      - admin_role must still be a bridgeable admin role.
      - link_row must exist and not be revoked.
      - live_partner_user must exist, be approved, its email must match
        the link's own recorded email (defends against a partner_users
        row's id being reused/repointed under the same primary key),
        and its role must still be a BRIDGEABLE_PARTNER_ROLE.

    Returns an identity dict shaped exactly like partner_identity()'s
    own output -- role/email/partner_id/customer_id -- plus
    via_bridge=True so callers can be honest in the UI and in audit
    logs about how this identity was reached, never silently
    indistinguishable from a direct login."""
    if admin_role not in BRIDGEABLE_ADMIN_ROLES:
        return None
    if not link_row or link_row.get("revoked_at"):
        return None
    if not live_partner_user or not live_partner_user.get("approved", 1):
        return None
    if str(live_partner_user.get("email", "")).strip().lower() != str(link_row.get("partner_email", "")).strip().lower():
        return None
    role = live_partner_user.get("role")
    if role not in BRIDGEABLE_PARTNER_ROLES:
        return None
    return {
        "role": role,
        "email": live_partner_user["email"],
        "partner_id": live_partner_user.get("partner_id"),
        "customer_id": live_partner_user.get("customer_id"),
        "via_bridge": True,
        "bridged_admin_user_id": link_row.get("admin_user_id"),
    }


def get_link(db, *, admin_user_id: str) -> dict | None:
    result = db.execute(
        "SELECT admin_user_id,admin_email,partner_user_id,partner_email,linked_at,linked_by,revoked_at "
        "FROM admin_partner_links WHERE admin_user_id=?",
        (admin_user_id,),
    ).fetchone()
    return dict(result) if result else None


def create_link(db, *, admin_user_id: str, admin_email: str, partner_user_id: str, partner_email: str, linked_by: str, now: str) -> None:
    """One admin_user_id maps to at most one bridged partner identity at
    a time -- re-linking (e.g. to a different partner account) replaces
    the previous row outright rather than accumulating stale links to
    reason about, the same replace-not-append convention
    camera_access.set_camera_access() already established for 'selected'
    mode in this codebase."""
    db.execute("DELETE FROM admin_partner_links WHERE admin_user_id=?", (admin_user_id,))
    db.execute(
        "INSERT INTO admin_partner_links(admin_user_id,admin_email,partner_user_id,partner_email,linked_at,linked_by,revoked_at) "
        "VALUES(?,?,?,?,?,?,NULL)",
        (admin_user_id, admin_email, partner_user_id, partner_email, now, linked_by),
    )


def revoke_link(db, *, admin_user_id: str, now: str) -> None:
    """Revokes immediately -- deletes no history (the row and its
    linked_at/linked_by stay for audit purposes), just stops
    resolve_bridged_identity() from ever validating it again."""
    db.execute("UPDATE admin_partner_links SET revoked_at=? WHERE admin_user_id=? AND revoked_at IS NULL", (now, admin_user_id))


def bridge_partner_identity(db, *, admin_user: dict) -> dict | None:
    """DB-touching wrapper: given an already-resolved Admin Portal user
    dict (from current_user()) and an open partner_db connection,
    resolves the bridged partner identity if this admin has a live,
    still-valid link -- or None if they have no link, a revoked one, or
    current_user() itself describes an unauthenticated/disabled caller
    (the anonymous fallback current_user() returns for no session at
    all must never be treated as a linkable admin_user_id)."""
    admin_id = admin_user.get("id")
    if not admin_id or admin_id == "anonymous" or not admin_user.get("enabled"):
        return None
    admin_role = str(admin_user.get("role") or "")
    link = get_link(db, admin_user_id=admin_id)
    live_partner_user = None
    if link:
        result = db.execute(
            "SELECT id,email,role,partner_id,customer_id,approved FROM partner_users WHERE id=?",
            (link["partner_user_id"],),
        ).fetchone()
        live_partner_user = dict(result) if result else None
    return resolve_bridged_identity(admin_role=admin_role, link_row=link, live_partner_user=live_partner_user)
