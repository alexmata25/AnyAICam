"""Regression coverage: an Administrator Portal identity (current_user()'s
legacy JSON-store account -- administrator/admin/support_admin/installer)
must never see customer video/footage navigation items just by virtue of
being an administrator.

Root cause: navigation_keys_for_role()'s ADMIN_PORTAL_ROLES branch
returned "every NAV_ITEMS key except PARTNER_IDENTITY_ONLY_NAV_KEYS" --
an allow-almost-everything list that happened to include Events, Smart
alerts, Playback, Media, and Dashboard, none of which that legacy
identity has any customer_camera_permissions/customer_id scoping for
(see camera_access.py's own "administrator: always authorized" rule,
which is scoped to the *Partner Portal* administrator role, a different
identity than this one, and never extended to it). The result matched
exactly what was reported: admin@local's own dashboard/events/etc.
pages correctly render "No customer video access", but the sidebar
still advertised them as if they worked -- the same "confusing dead
end" class of bug 5978a5a already fixed for PARTNER_IDENTITY_ONLY_
NAV_KEYS, just for a different, still-uncaught set of items.

Fixed with CUSTOMER_VIDEO_NAV_KEYS, a second exclusion set subtracted
the same way. There is currently no "explicit customer-video
permission" grant mechanism for an Administrator Portal account (see
that constant's own comment, and the existing, matching privacy-
boundary note on /admin-customers: "It does not expose customer camera
video. Any future support-video access must be explicit, time-limited
and audited.") -- until one exists, these stay hidden unconditionally.
"""

import main


CUSTOMER_VIDEO_KEYS = {"events", "alerts", "playback", "media", "dashboard"}
ADMIN_BUSINESS_KEYS = {"admin-customers", "operations", "settings", "audit"}


def test_administrator_never_sees_customer_video_nav():
    keys = main.navigation_keys_for_role("administrator")
    assert not (keys & CUSTOMER_VIDEO_KEYS), keys & CUSTOMER_VIDEO_KEYS


def test_every_admin_portal_role_never_sees_customer_video_nav():
    """admin@local's own role -- and every other legacy Admin Portal
    role -- all resolve through the exact same ADMIN_PORTAL_ROLES
    branch; none of them get customer video access just by being an
    administrator."""
    for role in main.ADMIN_PORTAL_ROLES:
        keys = main.navigation_keys_for_role(role)
        assert not (keys & CUSTOMER_VIDEO_KEYS), (role, keys & CUSTOMER_VIDEO_KEYS)


def test_administrator_still_sees_admin_business_navigation():
    """The fix hides customer video, not admin/business tooling --
    Customer accounts, Operations, Settings, and Audit logs must all
    still be visible."""
    keys = main.navigation_keys_for_role("administrator")
    assert ADMIN_BUSINESS_KEYS <= keys, ADMIN_BUSINESS_KEYS - keys


def test_customer_portal_roles_are_unaffected_by_the_admin_fix():
    """Regression guard the other direction: a real customer_owner/
    customer_viewer must keep seeing their own Events/Smart alerts/
    Playback/Dashboard nav exactly as before -- this fix only narrows
    the Admin Portal branch, never the customer-portal one."""
    for role in ("customer_owner", "customer_viewer"):
        keys = main.navigation_keys_for_role(role)
        assert {"events", "alerts", "playback", "dashboard"} <= keys, (role, keys)


def test_partner_identity_only_nav_keys_are_still_excluded_too():
    # The original exclusion (5978a5a) must keep working alongside the
    # new one -- this fix subtracts a second set, it does not replace
    # the first.
    keys = main.navigation_keys_for_role("administrator")
    assert not (keys & main.PARTNER_IDENTITY_ONLY_NAV_KEYS)
