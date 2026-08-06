import ast
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
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


class EventSettingsStub:
    def model_dump(self):
        return {
            "camera": 1,
            "enabled": True,
            "sensitivity": 60,
            "minimum_duration_seconds": 2,
            "cooldown_seconds": 15,
            "zones": [{"name": "Primary zone", "x": 0, "y": 0, "width": 1, "height": 1}],
        }


class CameraSettingsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_SOURCE.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def function(self, name):
        return next(
            item
            for item in self.tree.body
            if isinstance(item, ast.FunctionDef) and item.name == name
        )

    def execute(self, function_name, **values):
        app = RouteStub()
        namespace = {
            "app": app,
            "HTMLResponse": object,
            "Request": object,
            "HTTPException": RuntimeError,
            **values,
        }
        module = ast.fix_missing_locations(
            ast.Module(body=[self.function(function_name)], type_ignores=[])
        )
        exec(compile(module, str(MAIN_SOURCE), "exec"), namespace)
        return namespace

    def http_get_camera_settings(self, namespace):
        route = namespace["app"].routes["/camera/{camera_number}/settings"]

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    payload = route(1, object()).encode("utf-8")
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
                f"http://127.0.0.1:{server.server_port}/camera/1/settings",
                timeout=3,
            ) as response:
                return response.status, response.read().decode("utf-8")
        finally:
            server.server_close()
            thread.join(timeout=3)

    def test_camera_settings_route_returns_http_200(self):
        namespace = self.execute(
            "camera_settings",
            CAMERA_COUNT=4,
            current_user=lambda _request: {"role": "administrator"},
            has_permission=lambda _user, _permission: True,
            permission_denied_page=lambda *_args: "permission denied",
            get_event_settings=lambda _camera: EventSettingsStub(),
            camera_process_state={1: {"live": "running", "recording": "running"}},
            record_audit=lambda *_args: None,
            page_shell=lambda _title, _active, content, _scripts="": content,
            os=os,
            json=json,
            escape=escape,
        )

        status, html = self.http_get_camera_settings(namespace)

        self.assertEqual(status, 200)
        self.assertIn("Camera 1 settings", html)
        self.assertIn("Settings apply only to Camera 1", html)

    def test_live_page_buttons_use_camera_specific_settings_routes(self):
        namespace = self.execute(
            "home",
            CAMERA_COUNT=4,
            page_shell=lambda _title, _active, content, _scripts="": content,
        )
        html = namespace["home"]()
        for camera_number in range(1, 5):
            self.assertIn(f'href="/camera/{camera_number}/settings"', html)
        self.assertNotIn('title="Open camera and recording settings" href="/settings"', html)


if __name__ == "__main__":
    unittest.main()
