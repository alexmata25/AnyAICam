"""Focused tests for the Camera App / Alerts navigation-audience fix
(fix/customer-app-nav-audience).

Root cause (established investigation): NAV_ITEMS includes
("customer-app-settings", "/customer-app-settings", ..., "Camera apps & alerts")
in the single shared master list rendered into the general VMS sidebar.
navigation_keys_for_role() returns None (meaning "unrestricted, show every
item") for ADMIN_PORTAL_ROLES, so administrators saw this link even though
/customer-app-settings (app/customer_platform.py) correctly requires
customer_owner/customer_viewer and returns a 403 for everyone else.
Installer, partner, and the generic permission-mapped VMS roles were
already unaffected -- none of their navigation_keys_for_role() branches
include "customer-app-settings".

The fix adds one condition to page_shell()'s existing visible_nav_items
filter: "customer-app-settings" is now only ever visible when
shell_role is in CUSTOMER_PORTAL_ROLES, regardless of what
navigation_keys_for_role() returned (including its None/"unrestricted"
case for admins). The route's own authorization
(app/customer_platform.py's _is_customer() check) is untouched.

As with the other main.py-embedded logic in this repo, app/main.py can't
be imported directly here. NavAudienceFilterBehaviorTests extracts the
real NAV_ITEMS, ROLE_PERMISSIONS, the three portal role-set constants,
navigation_keys_for_role(), and the exact new filter condition verbatim
and executes them together for genuine behavioral proof, rather than a
hand-copied duplicate. RouteAuthorizationUnchangedSourceTests confirms
the route handler itself was not touched.
"""

import unittest
from pathlib import Path

MAIN_PY = Path(__file__).resolve().parents[1] / "app" / "main.py"
CUSTOMER_PLATFORM_PY = Path(__file__).resolve().parents[1] / "app" / "customer_platform.py"


def _extract(source: str, start_marker: str, end_marker: str, include_end: bool = True) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    if include_end:
        end += len(end_marker)
    return source[start:end]


class NavAudienceFilterBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = MAIN_PY.read_text(encoding="utf-8")

        role_permissions_src = _extract(source, "ROLE_PERMISSIONS = {\n", "\n}\n")
        role_sets_src = _extract(
            source,
            'PARTNER_PORTAL_ROLES = {"partner_admin", "partner_sales", "installer"}',
            "def is_master_admin(user: dict | None) -> bool:",
            include_end=False,
        )
        nav_items_src = _extract(source, "NAV_ITEMS = [\n", "\n]\n")
        nav_keys_fn_src = _extract(
            source,
            "def navigation_keys_for_role(role: str) -> set[str] | None:",
            "def page_shell(title: str, active: str, content: str, scripts: str = \"\") -> str:",
            include_end=False,
        )
        filter_condition_src = _extract(
            source,
            "if (allowed_keys is None or item[0] in allowed_keys)",
            'and (item[0] != "customer-app-settings" or shell_role in CUSTOMER_PORTAL_ROLES)',
        )

        wrapper_src = (
            "def compute_visible_nav_items(nav_items, allowed_keys, shell_role):\n"
            "    return [\n"
            "        item for item in nav_items\n"
            "        " + filter_condition_src + "\n"
            "    ]\n"
        )

        namespace = {}
        for snippet, label in (
            (role_sets_src, "role sets"),
            (role_permissions_src, "ROLE_PERMISSIONS"),
            (nav_items_src, "NAV_ITEMS"),
            (nav_keys_fn_src, "navigation_keys_for_role"),
            (wrapper_src, "compute_visible_nav_items wrapper"),
        ):
            exec(compile(snippet, f"app/main.py (extracted {label})", "exec"), namespace)

        cls.NAV_ITEMS = namespace["NAV_ITEMS"]
        cls.navigation_keys_for_role = staticmethod(namespace["navigation_keys_for_role"])
        cls.compute_visible_nav_items = staticmethod(namespace["compute_visible_nav_items"])

    def _visible_keys_for_role(self, role: str) -> set:
        allowed_keys = self.navigation_keys_for_role(role)
        visible = self.compute_visible_nav_items(self.NAV_ITEMS, allowed_keys, role)
        return {item[0] for item in visible}

    def test_nav_items_contains_the_customer_app_settings_entry(self):
        keys = {item[0] for item in self.NAV_ITEMS}
        self.assertIn("customer-app-settings", keys)

    def test_admin_role_no_longer_sees_camera_apps_and_alerts(self):
        self.assertNotIn("customer-app-settings", self._visible_keys_for_role("administrator"))
        self.assertNotIn("customer-app-settings", self._visible_keys_for_role("support_admin"))

    def test_installer_and_partner_roles_still_do_not_see_it(self):
        self.assertNotIn("customer-app-settings", self._visible_keys_for_role("installer"))
        self.assertNotIn("customer-app-settings", self._visible_keys_for_role("partner_sales"))

    def test_generic_vms_role_does_not_see_it(self):
        self.assertNotIn("customer-app-settings", self._visible_keys_for_role("viewer"))

    def test_customer_roles_still_see_camera_apps_and_alerts(self):
        self.assertIn("customer-app-settings", self._visible_keys_for_role("customer_owner"))
        self.assertIn("customer-app-settings", self._visible_keys_for_role("customer_viewer"))


class RouteAuthorizationUnchangedSourceTests(unittest.TestCase):
    """Confirms /customer-app-settings' own authorization check is untouched
    -- this fix only changes navigation visibility, never the route."""

    @classmethod
    def setUpClass(cls):
        cls.source = CUSTOMER_PLATFORM_PY.read_text(encoding="utf-8")

    def test_route_still_requires_customer_role(self):
        self.assertIn('@app.get("/customer-app-settings", response_class=HTMLResponse)', self.source)
        self.assertIn("def customer_app_settings(request: Request):", self.source)
        self.assertIn("if not _is_customer(user):", self.source)
        self.assertIn(
            'raise HTTPException(status_code=403, detail="Customer account required.")',
            self.source,
        )

    def test_is_customer_role_set_unchanged(self):
        self.assertIn(
            'return str(user.get("role") or "").lower() in {"customer_owner", "customer_viewer"}',
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
