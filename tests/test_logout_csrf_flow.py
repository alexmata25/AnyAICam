import ast
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
    cookies = {"anyaicam_session": "signed-session"}
    headers = {"user-agent": "Regression test"}


class LogoutCsrfFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = MAIN_SOURCE.read_text(encoding="utf-8")
        cls.security = SECURITY_SOURCE.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.main)

    def test_logout_frontend_uses_protected_fetch_and_follows_redirect(self):
        self.assertIn("form.matches('.sidebar-auth[action=\"/logout\"]')", self.main)
        self.assertIn("window.fetch(form.action,{method:'POST'})", self.main)
        self.assertIn("if(response.redirected){window.location.assign(response.url);return}", self.main)
        self.assertIn("'X-CSRF-Token':token", self.main)

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

    def test_missing_and_invalid_csrf_tokens_remain_rejected(self):
        self.assertIn("not cookie or not header", self.security)
        self.assertIn("not hmac.compare_digest(cookie,header)", self.security)
        self.assertIn("unsign(cookie)!='csrf'", self.security)
        self.assertNotIn("request.url.path!='/logout'", self.security)

    def test_login_fetch_behavior_is_unchanged(self):
        self.assertIn('form.matches(\'.auth-form\')', self.main)
        self.assertIn("new FormData(form)", self.main)
        self.assertIn("new URLSearchParams()", self.main)
        self.assertEqual(self.main.count("{auth_form_script()}</body></html>"), 3)

    def test_cookie_security_attributes_remain_compatible(self):
        self.assertIn("httponly=False,samesite='strict'", self.security)
        self.assertIn("httponly=True", self.main)
        self.assertIn('samesite="lax"', self.main)
        self.assertIn('path="/"', self.main)


if __name__ == "__main__":
    unittest.main()
