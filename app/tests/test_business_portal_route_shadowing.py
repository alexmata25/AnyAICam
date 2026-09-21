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

Fixed by removing ONLY the two shadowing GET handlers from business_
portal.py -- its POST endpoints (/api/sites, /api/users, etc.) and its
other GET pages (/appliances, /branding, /setup-legacy, /pricing-
legacy), which have no confirmed real replacement, are untouched.
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
    """The POST endpoints business_portal.py's own pages' own client-side
    JS calls (invite_user, add_site, complete_setup, update_branding)
    were never part of the shadowing bug and must be unaffected."""
    for path in ("/api/sites", "/api/users", "/api/setup/complete", "/api/branding", "/api/account-management"):
        matches = [r for r in main.app.routes if getattr(r, "path", None) == path]
        assert matches, f"expected {path} to still be registered"
