"""Per-camera user permissions -- separate from per-camera analytics
entitlements (customer_analytics_panel.py). A customer_viewer's set of
*visible* cameras; analytics entitlements then further gate what shows for
cameras they can already see.

Pure, DB/FastAPI-free decision logic (fully unit-testable); DB-touching
wrappers take an explicit db connection, matching camera_mapping.py's
established dependency-light pattern in this codebase.
"""
from __future__ import annotations

ACCESS_MODES = ("all", "selected")
DEFAULT_ACCESS_MODE = "selected"


def is_camera_authorized(
    camera_id: str,
    *,
    role: str,
    access_mode: str,
    permitted_camera_ids: set[str],
) -> bool:
    """The single decision every enforcement point in this app should
    reduce to. Fails closed: an unrecognized role, an unrecognized
    access_mode, or a customer_viewer with no explicit row for this exact
    camera all return False -- never True by default.

    - administrator / customer_owner: always authorized for any camera
      already scoped to their own customer_id by the caller's own SQL
      (this function does not itself check customer/site ownership --
      see authorized_camera_ids()'s docstring for where that happens).
    - customer_viewer, access_mode='all': authorized for any camera
      already scoped to their customer_id by the caller.
    - customer_viewer, access_mode='selected' (the default for every new
      user -- see DEFAULT_ACCESS_MODE): authorized only if camera_id is
      explicitly in permitted_camera_ids. A brand new camera is never in
      that set until a customer_owner explicitly adds it -- see the
      "new camera does not auto-expose" test.
    """
    if role in {"administrator", "customer_owner"}:
        return True
    if role != "customer_viewer":
        return False
    if access_mode == "all":
        return True
    if access_mode != "selected":
        return False  # unrecognized mode -- fail closed, not open
    return camera_id in permitted_camera_ids


def filter_authorized_cameras(
    camera_ids: list[str],
    *,
    role: str,
    access_mode: str,
    permitted_camera_ids: set[str],
) -> list[str]:
    """Same decision as is_camera_authorized(), applied to a list -- for
    building a visible-cameras list (Live grid, Playback camera picker,
    Investigate camera filter, etc.) rather than checking one camera a
    direct URL/API call named."""
    return [
        camera_id
        for camera_id in camera_ids
        if is_camera_authorized(camera_id, role=role, access_mode=access_mode, permitted_camera_ids=permitted_camera_ids)
    ]


def authorized_camera_ids(db, *, user_id: str, customer_id: str, role: str, access_mode: str) -> set[str]:
    """DB-touching wrapper: every camera_id belonging to customer_id (never
    another customer's or another site's -- the WHERE customer_id=? clause
    is the cross-customer-leakage boundary) that this specific user_id/role
    is authorized for, per is_camera_authorized()'s rules.

    administrator/customer_owner/access_mode='all' still only returns this
    customer's own cameras -- "all cameras" means all of *this customer's*
    cameras, never every camera in the system.
    """
    all_camera_ids = [row["id"] for row in db.execute("SELECT id FROM cameras WHERE customer_id=?", (customer_id,)).fetchall()]
    if role in {"administrator", "customer_owner"} or access_mode == "all":
        return set(all_camera_ids)
    permitted = {
        row["camera_id"]
        for row in db.execute(
            "SELECT camera_id FROM customer_camera_permissions WHERE user_id=?", (user_id,)
        ).fetchall()
    }
    return {camera_id for camera_id in all_camera_ids if camera_id in permitted}


def set_camera_access(db, *, user_id: str, access_mode: str, camera_ids: list[str], now: str) -> None:
    """Customer-owner-facing assignment: sets a user's access_mode on
    partner_users and, for 'selected' mode, replaces their entire
    customer_camera_permissions row set with exactly camera_ids (removing
    access to anything not in the new list takes effect immediately --
    there is no stale leftover row granting access the owner just revoked).
    For 'all' mode, existing selected-mode rows are left alone (harmless,
    unused while access_mode='all') rather than deleted, so switching back
    to 'selected' later restores the previous per-camera grants instead of
    starting from zero.
    """
    if access_mode not in ACCESS_MODES:
        raise ValueError(f"Unknown access_mode: {access_mode!r}")
    db.execute("UPDATE partner_users SET camera_access_mode=? WHERE id=?", (access_mode, user_id))
    if access_mode == "selected":
        db.execute("DELETE FROM customer_camera_permissions WHERE user_id=?", (user_id,))
        for camera_id in camera_ids:
            db.execute(
                "INSERT INTO customer_camera_permissions(user_id,camera_id,can_live,can_playback,can_download,can_share,can_alerts) "
                "VALUES(?,?,1,1,0,0,1)",
                (user_id, camera_id),
            )


def remove_camera_access(db, *, user_id: str, camera_id: str) -> None:
    """Revokes one camera for one user immediately -- deletes the row
    outright (unlike analytics entitlements' soft-cancel convention;
    per-camera visibility has no "history" concept to preserve). Only
    meaningful in 'selected' mode; harmless no-op otherwise."""
    db.execute("DELETE FROM customer_camera_permissions WHERE user_id=? AND camera_id=?", (user_id, camera_id))
