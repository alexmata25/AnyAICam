import ast
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import inspect
from pathlib import Path
import re
import threading
import unittest
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = ROOT / "app" / "main.py"


class RouteStub:
    def __init__(self):
        self.routes = {}

    def get(self, path, **_kwargs):
        def register(function):
            self.routes[path] = function
            return function
        return register


class SettingsAnalyticsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_SOURCE.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def node(self, kind, name):
        return next(
            item
            for item in self.tree.body
            if isinstance(item, kind) and getattr(item, "name", None) == name
        )

    def assignment(self, name):
        return next(
            item
            for item in self.tree.body
            if isinstance(item, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == name for target in item.targets)
        )

    def execute(self, *nodes, **values):
        app = RouteStub()
        namespace = {
            "app": app,
            "HTMLResponse": object,
            "Request": object,
            **values,
        }
        module = ast.fix_missing_locations(ast.Module(body=list(nodes), type_ignores=[]))
        exec(compile(module, str(MAIN_SOURCE), "exec"), namespace)
        return namespace

    def http_get(self, namespace, path):
        routes = namespace["app"].routes

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    route = routes[self.path]
                    result = route(object()) if inspect.signature(route).parameters else route()
                    payload = str(result).encode("utf-8")
                    self.send_response(200)
                except Exception as error:
                    payload = str(error).encode("utf-8")
                    self.send_response(500)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        try:
            with urlopen(
                f"http://127.0.0.1:{server.server_port}{path}", timeout=3
            ) as response:
                return response.status, response.read().decode("utf-8")
        finally:
            server.server_close()
            thread.join(timeout=3)

    def test_settings_page_renders_category_links(self):
        namespace = self.execute(
            self.node(ast.FunctionDef, "slugify"),
            self.assignment("SETTINGS_CATEGORIES"),
            self.assignment("IMPLEMENTED_SETTINGS_CATEGORIES"),
            self.node(ast.FunctionDef, "settings"),
            re=re,
            escape=escape,
            current_user=lambda _request: {"role": "administrator"},
            has_permission=lambda _user, _permission: True,
            record_audit=lambda *_args: None,
            page_shell=lambda _title, _active, content, _scripts="": content,
        )

        status, html = self.http_get(namespace, "/settings")
        self.assertEqual(status, 200)
        # "Events & alerts" is the only category with a real settings_detail()
        # implementation; it stays a real link. The rest, including
        # "Cameras", render as a non-navigable "Coming soon" row instead of a
        # link to a placeholder page -- see test_sidebar_navigation.py for
        # the full sidebar/navigation audit this encodes.
        self.assertIn('href="/settings/events-alerts"', html)
        self.assertNotIn('href="/settings/cameras"', html)
        self.assertIn("Coming soon", html)

    def test_analytics_page_renders_feature_links(self):
        namespace = self.execute(
            self.assignment("ANALYTICS_FEATURES"),
            self.node(ast.FunctionDef, "slugify"),
            self.node(ast.FunctionDef, "analytics"),
            re=re,
            escape=escape,
            get_camera_numbers=lambda: [1, 2, 3, 4],
            page_shell=lambda _title, _active, content, _scripts="": content,
        )

        status, html = self.http_get(namespace, "/analytics")
        self.assertEqual(status, 200)
        self.assertIn('href="/analytics/license-plate-recognition"', html)
        self.assertIn('href="/analytics/vehicle-search"', html)


if __name__ == "__main__":
    unittest.main()
