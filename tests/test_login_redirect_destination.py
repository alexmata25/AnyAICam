"""Focused regression test for the safe_login_destination() self-redirect
guard (fix/login-redirect-loop-guard).

Investigation finding: an authenticated visitor requesting
GET /login?next=/login (or POST /login with hidden field next_url=/login)
would loop forever, because safe_login_destination()'s fallback returned
the literal string "/login" when nothing else matched, and GET /login
unconditionally redirects an authenticated visitor to whatever
safe_login_destination() returns -- so the browser would be redirected to
/login again, and again, without the login form or any real destination
ever rendering. No internal code path was found that actually produces
next=/login, but the gap was real and unguarded regardless of the trigger.

This is a pure, self-contained function with no FastAPI/DB dependencies,
so its exact source (plus the role-set constants and the sibling
portal_destination_for_user() it calls) is extracted verbatim from
app/main.py and exec'd in isolation -- avoiding the app-wide import side
effects documented in tests/test_camera_diagnostics.py -- and exercised
directly, not through a hand-copied duplicate.
"""

import unittest
from pathlib import Path

MAIN_PY = Path(__file__).resolve().parents[1] / "app" / "main.py"

START_MARKER = 'PARTNER_PORTAL_ROLES = {"partner_admin", "partner_sales", "installer"}'
END_MARKER = "def partner_page_authorization_response(request: Request) -> Response | None:"


class SafeLoginDestinationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = MAIN_PY.read_text(encoding="utf-8")
        start = source.index(START_MARKER)
        end = source.index(END_MARKER, start)
        snippet = source[start:end]
        namespace = {}
        exec(
            compile(snippet, "app/main.py (extracted login-destination logic)", "exec"),
            namespace,
        )
        cls.safe_login_destination = staticmethod(namespace["safe_login_destination"])
        cls.portal_destination_for_user = staticmethod(namespace["portal_destination_for_user"])

    def test_authenticated_next_equal_to_login_does_not_loop(self):
        user = {"id": "u1", "role": "customer_owner"}
        destination = self.safe_login_destination(user, "/login")
        self.assertNotEqual(destination, "/login")
        self.assertEqual(destination, self.portal_destination_for_user(user))

    def test_default_next_still_goes_to_role_portal(self):
        cases = (
            ("customer_owner", "/customer-portal"),
            ("customer_viewer", "/customer-portal"),
            ("partner_sales", "/partner-sales"),
            ("partner_admin", "/partner-sales"),
            ("installer", "/partner-installations"),
            ("administrator", "/admin-portal"),
            ("support_admin", "/admin-portal"),
            ("", "/"),
        )
        for role, expected in cases:
            with self.subTest(role=role or "(none)"):
                user = {"id": "u1", "role": role}
                self.assertEqual(self.safe_login_destination(user, "/"), expected)

    def test_explicit_next_within_owned_portal_is_preserved(self):
        user = {"id": "u1", "role": "customer_owner"}
        self.assertEqual(
            self.safe_login_destination(user, "/customer-portal?page=alerts"),
            "/customer-portal?page=alerts",
        )

    def test_next_into_a_portal_the_role_cannot_use_is_redirected(self):
        user = {"id": "u1", "role": "customer_owner"}
        self.assertEqual(
            self.safe_login_destination(user, "/admin-portal"),
            "/customer-portal",
        )

    def test_unsafe_next_falls_back_to_role_portal(self):
        user = {"id": "u1", "role": "customer_owner"}
        for unsafe in ("//evil.example", "not-a-path", ""):
            with self.subTest(unsafe=unsafe):
                self.assertEqual(self.safe_login_destination(user, unsafe), "/customer-portal")


if __name__ == "__main__":
    unittest.main()
