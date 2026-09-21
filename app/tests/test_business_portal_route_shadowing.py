"""Regression coverage for a real, live bug found during an overnight
software audit (2026-09-22): register_business_routes() (business_
portal.py) registered its own mock, unauthenticated, single-JSON-file
(account_management.json) GET handlers for /sites-management and
/users, and main.py calls register_business_routes(app, page_shell)
BEFORE declaring its own real handlers for those same two exact paths
further down the file. Starlette dispatches to the FIRST-registered
route for an identical exact path, so every real request to either
page was actually served by the mock data below, never the real,
multi-tenant-DB-backed (and, for /users, manage_users-permission-
checked) implementation in main.py.

Confirmed live via direct route-table inspection on the real Ryzen
appliance (main.app.routes) to be the root cause of an earlier real
customer report this session: /sites-management showed "No sites
configured" regardless of the customer's actual configured sites,
because the mock account_management.json store's own 'sites' list is
always empty for a real deployment that never used that legacy setup
wizard.

A follow-up full route-table audit (comparing every (path, method) pair
across main.app.routes) found the identical pattern once more: POST
/api/users also shadowed main.py's real create_user(). Fixed the same
way.

Fixed by removing ONLY these three shadowing handlers from business_
portal.py -- its remaining POST endpoints (/api/sites, /api/setup/
complete, /api/branding) and its other GET pages (/appliances,
/branding, /setup-legacy, /pricing-legacy), which have no confirmed
real replacement, are untouched.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402


def _routes_for(path: str) -> list:
    return [r for r in main.app.routes if getattr(r, "path", None) == path]


def test_sites_management_is_not_shadowed_by_the_legacy_mock_page():
    routes = _routes_for("/sites-management")
    assert len(routes) == 1, (
        f"expected exactly one /sites-management route, found {len(routes)} -- "
        f"a reintroduced duplicate would silently shadow whichever one is registered first"
    )
    assert routes[0].endpoint.__module__ == "main", (
        "the real, multi-tenant-DB-backed /sites-management handler in main.py "
        "must be the one Starlette actually dispatches to"
    )


def test_users_page_is_not_shadowed_by_the_legacy_mock_page():
    routes = _routes_for("/users")
    assert len(routes) == 1, (
        f"expected exactly one /users route, found {len(routes)} -- "
        f"a reintroduced duplicate would silently shadow whichever one is registered first"
    )
    assert routes[0].endpoint.__module__ == "main", (
        "the real, manage_users-permission-checked /users handler in main.py "
        "must be the one Starlette actually dispatches to, not business_portal.py's "
        "unauthenticated mock"
    )


def test_business_portal_other_legacy_pages_are_still_registered():
    """Confirms the fix was surgical: only the two confirmed-shadowed
    GET handlers were removed. The remaining business_portal.py pages
    (no confirmed real replacement found) are untouched -- this test
    would fail if a future edit accidentally deleted more than intended."""
    for path in ("/appliances", "/branding", "/setup-legacy", "/pricing-legacy"):
        routes = _routes_for(path)
        assert len(routes) == 1, f"expected {path} to still be registered exactly once, found {len(routes)}"
        assert routes[0].endpoint.__module__ == "business_portal", (
            f"{path} was expected to still be business_portal.py's own page"
        )


def test_business_portal_post_endpoints_still_registered():
    """The remaining POST endpoints business_portal.py's own pages'
    client-side JS calls (add_site, complete_setup, update_branding)
    were never part of the shadowing bug and must be unaffected."""
    for path in ("/api/sites", "/api/setup/complete", "/api/branding", "/api/account-management"):
        matches = [r for r in main.app.routes if getattr(r, "path", None) == path]
        assert matches, f"expected {path} to still be registered"


def test_post_api_users_is_not_shadowed_by_the_legacy_mock_endpoint():
    """POST /api/users was ALSO shadowed the same way: business_portal.py's
    unauthenticated invite_user() mock (writing to account_management.json)
    registered before main.py's own real, permission-checked create_user().
    No confirmed live page still calls this shadowed path directly (the
    real /users page uses a separate real invitations flow), but any
    direct caller was silently getting the mock instead of real user
    creation -- removed for the same reason as the two GET pages."""
    matches = [r for r in main.app.routes if getattr(r, "path", None) == "/api/users" and "POST" in (getattr(r, "methods", None) or ())]
    assert len(matches) == 1, f"expected exactly one POST /api/users route, found {len(matches)}"
    assert matches[0].endpoint.__module__ == "main", (
        "the real, permission-checked create_user() in main.py must be the one "
        "Starlette actually dispatches to, not business_portal.py's unauthenticated mock"
    )
