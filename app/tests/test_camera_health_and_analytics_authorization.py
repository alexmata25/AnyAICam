"""Regression coverage for the Samsung UI walkthrough finding: /camera-health
and /analytics took NO request parameter and performed NO identity/
permission check at all -- reachable by any caller regardless of role,
including an unauthenticated one, as long as it got past authentication_
middleware. Both routes now go through the exact same centralized gate
every other Admin/Analytics page already uses (see /investigate's
investigation_page(), /operations/rdm, /investigation-cases, etc.):
current_user(request) + has_permission(user, "view_analytics"), denying
with permission_denied_page() on failure. No route-specific bypass logic
was added -- this reuses the existing helper exactly as instructed.

IMPORTANT (see this session's own postmortem): permission_denied_page()
renders the DENIED page's own `title` argument as its <h1> even on the
denial path, so a raw `<h1>{title}</h1>` substring is NOT a valid "access
granted" signal -- these tests always assert on the actual denial-specific
body text, "Your current role does not include", never on the heading.

Every scenario the task asked for is covered per route: Administrator
(has view_analytics) -> allowed; a legacy role without view_analytics
(viewer) -> denied; a role ROLE_PERMISSIONS doesn't recognize at all
(the shape of a bare Partner/Technician/Customer identity as seen by
this legacy current_user() boundary) -> denied; the unauthenticated
default -> denied.
"""
import inspect

import pytest

import main


# =============================================================== signature regression guard
# Guards directly against re-introducing the original bug: both routes
# used to take zero parameters and therefore could not possibly call
# current_user(request) at all.


def test_camera_health_page_accepts_a_request_parameter():
    assert "request" in inspect.signature(main.camera_health_page).parameters


def test_analytics_accepts_a_request_parameter():
    assert "request" in inspect.signature(main.analytics).parameters


# =============================================================== shared identity fixtures


def _administrator():
    return {"id": "u-admin", "email": "amata@anyaicam.com", "role": "administrator", "enabled": True, "camera_ids": []}


def _viewer():
    # A real legacy role that ROLE_PERMISSIONS recognizes but deliberately
    # grants no view_analytics -- distinct from the "unrecognized role"
    # case below.
    return {"id": "u-viewer", "email": "viewer@example.com", "role": "viewer", "enabled": True, "camera_ids": []}


def _bare_partner_or_customer_identity():
    # current_user()'s legacy boundary has no concept of partner_owner/
    # customer_owner/technician roles at all -- has_permission() falls
    # back to ROLE_PERMISSIONS.get(role, set()) for anything it doesn't
    # recognize, i.e. always denied. This is the shape current_user()
    # would actually produce for a Partner/Technician/Customer caller
    # that never resolves through cloud_administrator_bridge() (no live
    # global administrator grant) -- matching the task's requirement
    # that those identities are denied unless product policy explicitly
    # grants this page.
    return {"id": "u-partner", "email": "partner@example.com", "role": "partner_owner", "enabled": True, "camera_ids": []}


def _anonymous():
    return {"id": "anonymous", "email": "", "role": "viewer", "enabled": False, "camera_ids": [], "display_name": "Unauthenticated"}


_DENIAL_MARKER = "Your current role does not include"


# =============================================================== /camera-health


def test_camera_health_denies_anonymous(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _anonymous())
    result = main.camera_health_page(object())
    assert _DENIAL_MARKER in result
    assert "view_analytics" in result


def test_camera_health_denies_a_role_without_view_analytics(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _viewer())
    result = main.camera_health_page(object())
    assert _DENIAL_MARKER in result


def test_camera_health_denies_an_unrecognized_partner_or_customer_role(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _bare_partner_or_customer_identity())
    result = main.camera_health_page(object())
    assert _DENIAL_MARKER in result


def test_camera_health_allows_administrator(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _administrator())
    result = main.camera_health_page(object())
    assert _DENIAL_MARKER not in result
    assert "Camera health" in result
    assert "Camera status" in result


# =============================================================== /analytics


def test_analytics_denies_anonymous(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _anonymous())
    result = main.analytics(object())
    assert _DENIAL_MARKER in result
    assert "view_analytics" in result


def test_analytics_denies_a_role_without_view_analytics(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _viewer())
    result = main.analytics(object())
    assert _DENIAL_MARKER in result


def test_analytics_denies_an_unrecognized_partner_or_customer_role(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _bare_partner_or_customer_identity())
    result = main.analytics(object())
    assert _DENIAL_MARKER in result


def test_analytics_allows_administrator(monkeypatch):
    monkeypatch.setattr(main, "current_user", lambda request: _administrator())
    result = main.analytics(object())
    assert _DENIAL_MARKER not in result
    assert "Analytics" in result


# =============================================================== a live global-administrator grant (cloud_administrator_bridge) also passes
# Since current_user() is the single choke point both routes now call,
# a Partner Portal session with a live, global-scoped administrator
# grant (this session's cloud_administrator_bridge()) is expected to
# reach both pages exactly like a legacy administrator@local session --
# proving no route-specific bypass logic was needed, the existing
# helper covers this case for free.


def test_camera_health_allows_a_cloud_delegated_global_administrator(monkeypatch):
    monkeypatch.setattr(main, "authenticated_user", lambda request: None)
    monkeypatch.setattr(main, "cloud_administrator_bridge", lambda request: _administrator())
    result = main.camera_health_page(object())
    assert _DENIAL_MARKER not in result


def test_analytics_allows_a_cloud_delegated_global_administrator(monkeypatch):
    monkeypatch.setattr(main, "authenticated_user", lambda request: None)
    monkeypatch.setattr(main, "cloud_administrator_bridge", lambda request: _administrator())
    result = main.analytics(object())
    assert _DENIAL_MARKER not in result
