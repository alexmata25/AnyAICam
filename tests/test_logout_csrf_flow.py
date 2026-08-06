import ast
import hmac
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = ROOT / "app" / "main.py"
SECURITY_SOURCE = ROOT / "app" / "cloud_security.py"


class RouteStub:
    def __init__(self):
        self.routes = {}

    def post(self, path, **_kwargs):
        def register(function):
            self.routes[path] = function
            return function
        return register


class RedirectStub:
    def __init__(self, url, status_code):
        self.url = url
        self.status_code = status_code
        self.deleted = []

    def delete_cookie(self, name, **options):
        self.deleted.append((name, options))


class RequestStub:
    headers = {"user-agent": "Regression test"}

    def __init__(self, session="signed-session"):
        self.cookies = {"anyaicam_session": session}


class LogoutCsrfFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = MAIN_SOURCE.read_text(encoding="utf-8")
        cls.security = SECURITY_SOURCE.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.main)
        cls.security_tree = ast.parse(cls.security)

    def csrf_validation(self, cookie, header, unsigned="csrf"):
        function = next(
            node for node in self.security_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "csrf_validation"
        )
        namespace = {"hmac": hmac, "unsign": lambda _value: unsigned}
        module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        exec(compile(module, str(SECURITY_SOURCE), "exec"), namespace)
        return namespace["csrf_validation"](cookie, header)

    def test_logout_frontend_uses_protected_fetch_and_follows_redirect(self):
        self.assertIn("document.getElementById('sidebar-logout')?.addEventListener('click'", self.main)
        self.assertIn("window.fetch('/logout',{method:'POST'})", self.main)
        self.assertEqual(self.main.count("requestUrl=input instanceof URL?input:"), 2)
        self.assertNotIn("new URL(input.url),sameOrigin", self.main)
        self.assertIn("if(response.redirected){window.location.assign(response.url);return}", self.main)
        self.assertIn("'X-CSRF-Token':token", self.main)
        self.assertIn('id="sidebar-logout"', self.main)
        self.assertIn('class="sidebar-logout" type="button"', self.main)
        self.assertNotIn('method="post" action="/logout"', self.main)

    def test_logout_handler_destroys_session_and_redirects_to_login(self):
        function = next(
            node
            for node in self.tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "logout"
        )
        app = RouteStub()
        destroyed = []
        namespace = {
            "app": app,
            "Request": object,
            "authenticated_user": lambda _request: None,
            "destroy_session": destroyed.append,
            "SESSION_COOKIE_NAME": "anyaicam_session",
            "RedirectResponse": RedirectStub,
        }
        module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        exec(compile(module, str(MAIN_SOURCE), "exec"), namespace)

        response = app.routes["/logout"](RequestStub())

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.url, "/login")
        self.assertEqual(destroyed, ["signed-session"])
        self.assertEqual(response.deleted, [("anyaicam_session", {"path": "/"})])

    def test_successful_logout_csrf_is_accepted(self):
        self.assertEqual(self.csrf_validation("signed-token", "signed-token"), (True, "valid"))

    def test_logout_without_token_is_rejected(self):
        self.assertEqual(self.csrf_validation("signed-token", None), (False, "missing_header"))
        self.assertEqual(self.csrf_validation(None, "signed-token"), (False, "missing_cookie"))

    def test_invalid_logout_token_is_rejected(self):
        self.assertEqual(self.csrf_validation("signed-token", "different"), (False, "token_mismatch"))
        self.assertEqual(self.csrf_validation("signed-token", "signed-token", unsigned=None), (False, "invalid_signature"))
        self.assertNotIn("request.url.path!='/logout'", self.security)

    def test_failed_logout_diagnostics_are_redacted(self):
        self.assertIn("logout_csrf_failed reason=%s", self.security)
        self.assertIn("header_present=%s", self.security)
        self.assertNotIn("cookie_value=", self.security)
        self.assertNotIn("header_value=", self.security)

    def test_login_fetch_behavior_is_unchanged(self):
        self.assertIn('form.matches(\'.auth-form\')', self.main)
        self.assertIn("new FormData(form)", self.main)
        self.assertIn("new URLSearchParams()", self.main)
        self.assertEqual(self.main.count("{auth_form_script()}</body></html>"), 3)

    def test_login_logout_login_again_keeps_csrf_and_rotates_session(self):
        function = next(
            node for node in self.tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "logout"
        )
        app = RouteStub()
        destroyed = []
        namespace = {
            "app": app,
            "Request": object,
            "authenticated_user": lambda _request: None,
            "destroy_session": destroyed.append,
            "SESSION_COOKIE_NAME": "anyaicam_session",
            "RedirectResponse": RedirectStub,
        }
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(MAIN_SOURCE), "exec"), namespace)
        first = app.routes["/logout"](RequestStub("first-login-session"))
        second = app.routes["/logout"](RequestStub("second-login-session"))
        self.assertEqual(destroyed, ["first-login-session", "second-login-session"])
        self.assertEqual((first.url, second.url), ("/login", "/login"))
        self.assertTrue(all(name == "anyaicam_session" for response in (first, second) for name, _options in response.deleted))
        self.assertNotIn("delete_cookie('anyaicam_csrf'", self.main)

    def test_cookie_security_attributes_remain_compatible(self):
        self.assertIn("httponly=False,samesite='strict'", self.security)
        self.assertIn("httponly=True", self.main)
        self.assertIn('samesite="lax"', self.main)
        self.assertIn('path="/"', self.main)


if __name__ == "__main__":
    unittest.main()
