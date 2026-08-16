"""Phase 6d (docs/AI_HANDOFF.md Sec 8) tests for app/live_view_page.py:
GET /customer/cameras/{camera_id}/live.

Route-level tests use a real, lightweight in-memory SQLite database (no
foreign-key enforcement) covering cameras/partner_users/
customer_camera_permissions -- following the exact same pattern already
established in test_live_playlist.py and test_live_view_sessions.py.
partner_identity()/connection() are patched; page_shell is a small,
direct test double that records its own call arguments (matching the
project's own established "lambda *args, **kwargs: ..." fake-registrar
convention). No real partner_db, no real network/AWS call anywhere.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

os.environ.setdefault("ANYAICAM_DATABASE_BACKEND", "sqlite")
os.environ.setdefault(
    "ANYAICAM_PARTNER_DB",
    str(Path(tempfile.gettempdir()) / "anyaicam-live-view-page-wiring-test.db"),
)
os.environ.setdefault("ANYAICAM_ENV", "development")
os.environ.setdefault(
    "ANYAICAM_LIVE_MANIFEST_FILE",
    str(Path(tempfile.gettempdir()) / "anyaicam-live-view-page-manifest-import-guard.json"),
)

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402

import live_view_page  # noqa: E402


def _endpoint(app: FastAPI, path: str):
    for candidate_route in app.routes:
        if getattr(candidate_route, "path", None) == path:
            return candidate_route.endpoint
    raise AssertionError(f"route not registered: {path}")


class _PageShellRecorder:
    """Fake page_shell -- records exactly what it was called with,
    matching this project's own established "lambda *args, **kwargs"
    fake-registrar convention (e.g. RDM-2's
    register_appliance_cloud_routes(_ROUTE_APP, lambda *_args, **_kwargs: ''))
    but keeping the call arguments for assertions."""

    def __init__(self):
        self.calls = []

    def __call__(self, title, active, content, scripts=""):
        self.calls.append({"title": title, "active": active, "content": content, "scripts": scripts})
        return f"<rendered>{content}{scripts}</rendered>"


_ROUTE_APP = FastAPI()
_page_shell = _PageShellRecorder()
live_view_page.register_live_view_page_routes(_ROUTE_APP, _page_shell)
live_view_page_endpoint = _endpoint(_ROUTE_APP, "/customer/cameras/{camera_id}/live")


def _make_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE cameras(id TEXT PRIMARY KEY, customer_id TEXT, site_id TEXT, "
        "appliance_id TEXT, name TEXT, camera_number INTEGER)"
    )
    db.execute("CREATE TABLE partner_users(id TEXT PRIMARY KEY, email TEXT)")
    db.execute(
        "CREATE TABLE customer_camera_permissions(user_id TEXT, camera_id TEXT, can_live INTEGER)"
    )
    return db


def _seed(db, *, can_live=1, customer_id="cust-1", camera_id="cam-1", name="Front Door"):
    db.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number) VALUES(?,?,?,?,?,?)",
        (camera_id, customer_id, "site-1", "app-1", name, 5),
    )
    db.execute("INSERT OR IGNORE INTO partner_users(id,email) VALUES(?,?)", ("user-1", "owner@example.com"))
    if can_live is not None:
        db.execute(
            "INSERT INTO customer_camera_permissions(user_id,camera_id,can_live) VALUES(?,?,?)",
            ("user-1", camera_id, can_live),
        )


class _ConnectionContext:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self._db

    def __exit__(self, *_args):
        return False


class FakeRequest:
    def __init__(self):
        self.headers = {}
        self.client = None


IDENTITY = {"role": "customer_owner", "customer_id": "cust-1", "email": "owner@example.com"}


class LiveViewPageTestCase(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        _page_shell.calls.clear()
        self._patches = [
            patch.object(live_view_page, "partner_identity", return_value=IDENTITY),
            patch.object(live_view_page, "connection", return_value=_ConnectionContext(self.db)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self.db.close()


class AuthAndAuthorizationTests(LiveViewPageTestCase):
    def test_missing_identity_redirects_to_login(self):
        with patch.object(live_view_page, "partner_identity", return_value=None):
            result = live_view_page_endpoint(request=FakeRequest(), camera_id="cam-1")
        self.assertIsInstance(result, RedirectResponse)
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.headers["location"], "/partner-login")

    def test_non_customer_owner_role_redirects_to_login(self):
        with patch.object(live_view_page, "partner_identity", return_value={**IDENTITY, "role": "customer_viewer"}):
            result = live_view_page_endpoint(request=FakeRequest(), camera_id="cam-1")
        self.assertIsInstance(result, RedirectResponse)
        self.assertEqual(result.status_code, 303)

    def test_camera_not_found_returns_404(self):
        with self.assertRaises(HTTPException) as ctx:
            live_view_page_endpoint(request=FakeRequest(), camera_id="cam-missing")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_camera_belonging_to_another_customer_returns_404(self):
        _seed(self.db, customer_id="cust-OTHER")
        with self.assertRaises(HTTPException) as ctx:
            live_view_page_endpoint(request=FakeRequest(), camera_id="cam-1")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_missing_can_live_row_returns_403(self):
        _seed(self.db, can_live=None)
        with self.assertRaises(HTTPException) as ctx:
            live_view_page_endpoint(request=FakeRequest(), camera_id="cam-1")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_can_live_zero_returns_403(self):
        _seed(self.db, can_live=0)
        with self.assertRaises(HTTPException) as ctx:
            live_view_page_endpoint(request=FakeRequest(), camera_id="cam-1")
        self.assertEqual(ctx.exception.status_code, 403)


class SuccessfulRenderTests(LiveViewPageTestCase):
    def test_successful_render_calls_page_shell_once(self):
        _seed(self.db)
        live_view_page_endpoint(request=FakeRequest(), camera_id="cam-1")
        self.assertEqual(len(_page_shell.calls), 1)

    def test_page_title_and_nav_key(self):
        _seed(self.db, name="Front Door")
        live_view_page_endpoint(request=FakeRequest(), camera_id="cam-1")
        call = _page_shell.calls[0]
        self.assertIn("Front Door", call["title"])
        self.assertEqual(call["active"], "live")

    def test_content_contains_escaped_camera_name(self):
        _seed(self.db, name="<script>alert(1)</script>")
        live_view_page_endpoint(request=FakeRequest(), camera_id="cam-1")
        content = _page_shell.calls[0]["content"]
        self.assertNotIn("<script>alert(1)</script>", content)
        self.assertIn("&lt;script&gt;", content)

    def test_scripts_embed_the_real_start_and_playlist_urls(self):
        _seed(self.db)
        live_view_page_endpoint(request=FakeRequest(), camera_id="cam-1")
        scripts = _page_shell.calls[0]["scripts"]
        self.assertIn('"/api/customer/cameras/cam-1/live/start"', scripts)
        self.assertIn('"/api/customer/cameras/cam-1/live/playlist.m3u8"', scripts)

    def test_scripts_embed_the_stop_endpoint_prefix(self):
        _seed(self.db)
        live_view_page_endpoint(request=FakeRequest(), camera_id="cam-1")
        scripts = _page_shell.calls[0]["scripts"]
        self.assertIn("/api/customer/live/sessions/", scripts)
        self.assertIn("/stop", scripts)

    def test_scripts_include_hls_js_and_poll_configuration(self):
        _seed(self.db)
        live_view_page_endpoint(request=FakeRequest(), camera_id="cam-1")
        scripts = _page_shell.calls[0]["scripts"]
        self.assertIn("cdn.jsdelivr.net/npm/hls.js", scripts)
        self.assertIn(f"pollIntervalMs={live_view_page.POLL_INTERVAL_MS}", scripts)
        self.assertIn(f"pollTimeoutMs={live_view_page.POLL_TIMEOUT_MS}", scripts)

    def test_camera_id_with_special_characters_is_safely_json_embedded(self):
        # A camera_id containing a double quote must never be able to
        # break out of the embedded JS string literal.
        tricky_id = 'cam"1'
        _seed(self.db, camera_id=tricky_id)
        live_view_page_endpoint(request=FakeRequest(), camera_id=tricky_id)
        scripts = _page_shell.calls[0]["scripts"]
        self.assertIn('cam\\"1', scripts)  # properly escaped, never a raw unescaped quote

    def test_camera_with_no_name_falls_back_to_camera_id_in_title(self):
        _seed(self.db, name=None, camera_id="cam-noname")
        live_view_page_endpoint(request=FakeRequest(), camera_id="cam-noname")
        self.assertIn("cam-noname", _page_shell.calls[0]["title"])


if __name__ == "__main__":
    unittest.main()
