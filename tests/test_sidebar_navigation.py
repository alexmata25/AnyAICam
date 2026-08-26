"""Regression coverage for the sidebar/navigation role-routing audit.

app/main.py is not imported directly here -- see camera_mapping.py's
docstring and the other test_*_characterization.py files in this directory
for the documented test-discovery-order fragility that comes from importing
`main` (it is a FastAPI app module with heavy import-time side effects).
Instead these tests parse the relevant source text the same way the rest of
this suite already does for main.py (see
CameraProvisioningIntegrationTests in test_camera_provisioning_health.py),
which is enough to pin down the exact bugs found in this audit:

1. Administrators (ADMIN_PORTAL_ROLES) were shown every sidebar item,
   including ones gated behind partner_identity()/require_partner_access()
   that an administrator account has no record for -- landing them on
   /partner-login or /admin-portal instead of the page the label promised.
2. "Evidence integrity" highlighted the "Cases" sidebar item instead of
   itself, on both the normal and the permission-denied render path.
3. The /settings list linked to nine categories whose detail page is a
   placeholder ("Controls for ... will be connected here"), indistinguishable
   from a working page until clicked.
4. /pricing, /quotes, /setup, and /subscription (app/pricing_portal.py) had
   no authentication check at all.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
PRICING_SOURCE = (ROOT / "app" / "pricing_portal.py").read_text(encoding="utf-8")

# Route decorators live all over app/*.py (each feature area registers its
# own routes into the shared FastAPI app -- see the register_*_routes()
# calls in main.py). Search every current source module rather than a
# curated subset, so a route that moves to a new file doesn't silently
# start passing for the wrong reason -- and so a genuinely missing route
# still fails loudly. Historical main_before_*.py/*.backup.py snapshots are
# excluded: they are not what actually serves traffic.
_ALL_APP_SOURCE = "\n".join(
    path.read_text(encoding="utf-8")
    for path in sorted((ROOT / "app").glob("*.py"))
    if "before" not in path.name and "backup" not in path.name
)


def _nav_items_block():
    start = MAIN_SOURCE.index("NAV_ITEMS = [")
    end = MAIN_SOURCE.index("\n]", start)
    return MAIN_SOURCE[start:end]


def _nav_items():
    return re.findall(
        r'\("([\w-]+)",\s*"(/[^"]*)",\s*"[^"]*",\s*"([^"]*)"\)',
        _nav_items_block(),
    )


def _route_is_registered(route: str) -> bool:
    return f'@app.get("{route}"' in _ALL_APP_SOURCE or f"@app.get('{route}'" in _ALL_APP_SOURCE


class NavItemsExistTests(unittest.TestCase):
    def test_every_sidebar_route_is_registered_somewhere(self):
        nav_items = _nav_items()
        self.assertGreater(len(nav_items), 40, "sanity check: NAV_ITEMS parsing found too few items")
        missing = [(key, route, label) for key, route, label in nav_items if not _route_is_registered(route)]
        self.assertEqual(missing, [], f"sidebar items with no matching @app.get route: {missing}")


class AdminNavigationVisibilityTests(unittest.TestCase):
    """Root cause #1: administrators used to see every nav item, including
    ones only reachable through a partner_identity() the account doesn't
    have."""

    def test_admin_no_longer_sees_every_item_unconditionally(self):
        self.assertNotIn(
            "return None  # Administrators see every navigation item.",
            MAIN_SOURCE,
            "the unconditional 'show everything' branch for ADMIN_PORTAL_ROLES should be gone",
        )

    def test_partner_identity_only_keys_are_excluded_for_admins(self):
        self.assertIn("PARTNER_IDENTITY_ONLY_NAV_KEYS = {", MAIN_SOURCE)
        self.assertIn(
            "{key for key, _url, _icon, _label in NAV_ITEMS} - PARTNER_IDENTITY_ONLY_NAV_KEYS",
            MAIN_SOURCE,
        )
        for key in ("live", "appliances", "partner", "partner-sales",
                    "partner-quotes", "partner-install", "partner-performance",
                    "setup", "subscription", "pricing"):
            self.assertIn(f'"{key}"', MAIN_SOURCE[MAIN_SOURCE.index("PARTNER_IDENTITY_ONLY_NAV_KEYS = {"):
                                                    MAIN_SOURCE.index("}", MAIN_SOURCE.index("PARTNER_IDENTITY_ONLY_NAV_KEYS = {"))])

    def test_admin_still_keeps_its_own_management_items(self):
        # These are not in PARTNER_IDENTITY_ONLY_NAV_KEYS, so the set
        # subtraction must leave them visible to administrators.
        block_start = MAIN_SOURCE.index("PARTNER_IDENTITY_ONLY_NAV_KEYS = {")
        block_end = MAIN_SOURCE.index("}", block_start)
        excluded_block = MAIN_SOURCE[block_start:block_end]
        for key in ("dashboard", "admin-portal", "admin-customers", "settings",
                    "evidence", "audit", "camera-health", "analytics"):
            self.assertNotIn(f'"{key}"', excluded_block, f"{key!r} must stay visible to administrators")


class EvidenceIntegrityActiveKeyTests(unittest.TestCase):
    """Root cause #2: both render paths for /evidence-integrity passed
    "cases" as the active sidebar key instead of "evidence"."""

    def test_success_path_highlights_evidence_not_cases(self):
        self.assertIn(
            'return page_shell("Evidence integrity", "evidence", content, scripts)',
            MAIN_SOURCE,
        )
        self.assertNotIn(
            'return page_shell("Evidence integrity", "cases", content, scripts)',
            MAIN_SOURCE,
        )

    def test_permission_denied_path_highlights_evidence_not_cases(self):
        self.assertIn(
            'return permission_denied_page("Evidence integrity", "evidence", "view_analytics")',
            MAIN_SOURCE,
        )
        self.assertNotIn(
            'return permission_denied_page("Evidence integrity", "cases", "view_analytics")',
            MAIN_SOURCE,
        )


class SettingsListTests(unittest.TestCase):
    """Root cause #3: every /settings category linked to a page, but only
    "Events & alerts" has a real settings_detail() implementation -- the
    other nine rendered "Controls for ... will be connected here", which
    reads as a blank/broken page once opened."""

    def test_only_implemented_categories_are_clickable_links(self):
        self.assertIn('IMPLEMENTED_SETTINGS_CATEGORIES = {"Events & alerts"}', MAIN_SOURCE)
        self.assertIn("if name in IMPLEMENTED_SETTINGS_CATEGORIES else", MAIN_SOURCE)
        self.assertIn('class="pill wait">Coming soon</span></div>', MAIN_SOURCE)

    def test_settings_categories_not_yet_implemented_are_not_silently_dropped(self):
        # They should still exist (so settings_detail() keeps its honest
        # placeholder for anyone who navigates there directly), just not be
        # presented as an equal, working link from the /settings list.
        for name in ("Cameras", "Recording", "Analytics", "Notifications",
                      "Users", "Network", "Storage", "System", "Integrations"):
            self.assertIn(f'("{name}",', MAIN_SOURCE)


class PricingPortalAuthenticationTests(unittest.TestCase):
    """Root cause #4: /pricing, /quotes, /setup, and /subscription had no
    authentication check at all -- /setup in particular creates a customer
    account and returns its temporary password."""

    def test_all_four_pages_check_partner_authorization(self):
        self.assertIn("from fastapi import FastAPI, HTTPException, Request", PRICING_SOURCE)
        for fn_name in ("pricing_page", "quotes_page", "setup_page", "subscription_page"):
            def_index = PRICING_SOURCE.index(f"def {fn_name}(request: Request)")
            # the guard is the very next non-blank statement after the def line
            snippet = PRICING_SOURCE[def_index:def_index + 1000]
            self.assertIn("partner_page_authorization_response(request)", snippet, f"{fn_name} is missing the auth guard")
            self.assertIn("if authorization_response is not None:", snippet, f"{fn_name} is missing the auth guard")


if __name__ == "__main__":
    unittest.main()
