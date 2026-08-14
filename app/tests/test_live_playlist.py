"""Phase 6b (docs/AI_HANDOFF.md Sec 8) tests for app/live_playlist.py:
render_playlist() (pure rendering) and the
GET /api/customer/cameras/{camera_id}/live/playlist.m3u8 route.

Named to sort alphabetically AFTER test_cloud_readiness.py on purpose -- see
test_live_view_page_characterization.py's docstring for the full
explanation (this file imports appliance_cloud -> partner_db, which
freezes its DB path on first import in the process).

Route-level tests use a real, lightweight in-memory SQLite database (no
foreign-key enforcement) covering cameras/partner_users/
customer_camera_permissions, following the same pattern already
established in test_customer_camera_number_wiring.py. live_manifest_store
and get_configured_signer() are patched with simple fakes -- no real
filesystem manifest file, no real signing key, no real network/AWS call
anywhere.
"""

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

os.environ.setdefault("ANYAICAM_DATABASE_BACKEND", "sqlite")
os.environ.setdefault(
    "ANYAICAM_PARTNER_DB",
    str(Path(tempfile.gettempdir()) / "anyaicam-live-playlist-wiring-test.db"),
)
os.environ.setdefault("ANYAICAM_ENV", "development")
os.environ.setdefault(
    "ANYAICAM_LIVE_MANIFEST_FILE",
    str(Path(tempfile.gettempdir()) / "anyaicam-live-playlist-manifest-import-guard.json"),
)

from fastapi import FastAPI, HTTPException  # noqa: E402

import live_playlist  # noqa: E402
from live_playlist import (
    STALE_MANIFEST_SECONDS,
    _segment_filename,
    _valid_sequence,
    render_playlist,
)


def _endpoint(app: FastAPI, path: str):
    for candidate_route in app.routes:
        if getattr(candidate_route, "path", None) == path:
            return candidate_route.endpoint
    raise AssertionError(f"route not registered: {path}")


_ROUTE_APP = FastAPI()
live_playlist.register_live_playlist_routes(_ROUTE_APP)
live_playlist_endpoint = _endpoint(_ROUTE_APP, "/api/customer/cameras/{camera_id}/live/playlist.m3u8")

EXPECTED_PREFIX = "live/cust-1/site-1/app-1/cam-1/"
VALID_RENDER_KWARGS = dict(
    expected_prefix=EXPECTED_PREFIX,
    cloudfront_base_url="https://d31cxfv0l904ar.cloudfront.net",
    customer_id="cust-1",
    site_id="site-1",
    appliance_id="app-1",
    camera_id="cam-1",
    key_id="KEYPAIRID123",
    rsa_signer=lambda message: b"fake-signature",
)


class ValidatorTests(unittest.TestCase):
    def test_segment_filename_strips_matching_prefix(self):
        self.assertEqual(_segment_filename(EXPECTED_PREFIX + "camera1_1.ts", EXPECTED_PREFIX), "camera1_1.ts")

    def test_segment_filename_returns_none_for_wrong_prefix(self):
        self.assertIsNone(_segment_filename("live/cust-OTHER/site-1/app-1/cam-1/camera1_1.ts", EXPECTED_PREFIX))

    def test_segment_filename_returns_none_for_non_string(self):
        self.assertIsNone(_segment_filename(None, EXPECTED_PREFIX))
        self.assertIsNone(_segment_filename(12345, EXPECTED_PREFIX))

    def test_valid_sequence_accepts_int(self):
        self.assertEqual(_valid_sequence(7), 7)
        self.assertEqual(_valid_sequence(0), 0)

    def test_valid_sequence_rejects_bool_none_and_non_int(self):
        self.assertIsNone(_valid_sequence(True))
        self.assertIsNone(_valid_sequence(False))
        self.assertIsNone(_valid_sequence(None))
        self.assertIsNone(_valid_sequence("3"))
        self.assertIsNone(_valid_sequence(1.5))


class RenderPlaylistTests(unittest.TestCase):
    def _manifest(self, segments, updated_at=1000.0):
        return {"segments": segments, "updated_at": updated_at}

    def test_basic_structure_and_target_duration(self):
        manifest = self._manifest([{"key": EXPECTED_PREFIX + "camera1_0.ts", "sequence": 0}])
        text = render_playlist(manifest, **VALID_RENDER_KWARGS)
        self.assertTrue(text.startswith("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n"))

    def test_media_sequence_is_first_stored_entrys_sequence(self):
        manifest = self._manifest([
            {"key": EXPECTED_PREFIX + "camera1_5.ts", "sequence": 5},
            {"key": EXPECTED_PREFIX + "camera1_6.ts", "sequence": 6},
        ])
        text = render_playlist(manifest, **VALID_RENDER_KWARGS)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:5\n", text)

    def test_stored_arrival_order_is_preserved_not_resorted(self):
        # Simulates a post-restart sequence reset: arrival order (47, 48, 0,
        # 1) must be rendered in that order, not re-sorted to (0, 1, 47, 48).
        # Filenames use valid numeric suffixes -- letters aren't allowed by
        # the canonical camera\d+[0-9_.\-]*\.ts pattern.
        manifest = self._manifest([
            {"key": EXPECTED_PREFIX + "camera1_147.ts", "sequence": 47},
            {"key": EXPECTED_PREFIX + "camera1_148.ts", "sequence": 48},
            {"key": EXPECTED_PREFIX + "camera1_100.ts", "sequence": 0},
            {"key": EXPECTED_PREFIX + "camera1_101.ts", "sequence": 1},
        ])
        text = render_playlist(manifest, **VALID_RENDER_KWARGS)
        order = [line for line in text.splitlines() if line.startswith("https://")]
        self.assertEqual(len(order), 4)
        self.assertIn("camera1_147.ts", order[0])
        self.assertIn("camera1_148.ts", order[1])
        self.assertIn("camera1_100.ts", order[2])
        self.assertIn("camera1_101.ts", order[3])
        self.assertEqual(text.count("#EXT-X-MEDIA-SEQUENCE:"), 1)
        self.assertTrue(text.split("#EXT-X-MEDIA-SEQUENCE:")[1].startswith("47"))

    def test_empty_manifest_renders_valid_empty_playlist(self):
        text = render_playlist(self._manifest([]), **VALID_RENDER_KWARGS)
        self.assertIn("#EXTM3U", text)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:0", text)
        self.assertNotIn("#EXTINF", text)

    def test_endlist_is_never_emitted(self):
        manifest = self._manifest([{"key": EXPECTED_PREFIX + "camera1_0.ts", "sequence": 0}])
        text = render_playlist(manifest, **VALID_RENDER_KWARGS)
        self.assertNotIn("#EXT-X-ENDLIST", text)

    def test_entry_with_wrong_prefix_is_dropped_not_fatal(self):
        manifest = self._manifest([
            {"key": "live/cust-OTHER/site-1/app-1/cam-1/camera1_0.ts", "sequence": 0},
            {"key": EXPECTED_PREFIX + "camera1_1.ts", "sequence": 1},
        ])
        text = render_playlist(manifest, **VALID_RENDER_KWARGS)
        self.assertEqual(text.count("#EXTINF"), 1)
        self.assertIn("camera1_1.ts", text)

    def test_entry_with_missing_sequence_is_dropped_not_defaulted_to_zero(self):
        manifest = self._manifest([
            {"key": EXPECTED_PREFIX + "camera1_0.ts", "sequence": None},
            {"key": EXPECTED_PREFIX + "camera1_1.ts", "sequence": 1},
        ])
        text = render_playlist(manifest, **VALID_RENDER_KWARGS)
        self.assertEqual(text.count("#EXTINF"), 1)
        self.assertIn("camera1_1.ts", text)
        self.assertNotIn("camera1_0.ts", text)

    def test_entry_with_unsignable_filename_is_dropped(self):
        manifest = self._manifest([
            {"key": EXPECTED_PREFIX + "not-a-segment.mp4", "sequence": 0},
            {"key": EXPECTED_PREFIX + "camera1_1.ts", "sequence": 1},
        ])
        text = render_playlist(manifest, **VALID_RENDER_KWARGS)
        self.assertEqual(text.count("#EXTINF"), 1)
        self.assertIn("camera1_1.ts", text)

    def test_non_dict_entry_is_dropped(self):
        manifest = self._manifest(["not-a-dict", {"key": EXPECTED_PREFIX + "camera1_1.ts", "sequence": 1}])
        text = render_playlist(manifest, **VALID_RENDER_KWARGS)
        self.assertEqual(text.count("#EXTINF"), 1)


def _make_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE cameras(id TEXT PRIMARY KEY, customer_id TEXT, site_id TEXT, "
        "appliance_id TEXT, camera_number INTEGER)"
    )
    db.execute("CREATE TABLE partner_users(id TEXT PRIMARY KEY, email TEXT)")
    db.execute(
        "CREATE TABLE customer_camera_permissions(user_id TEXT, camera_id TEXT, can_live INTEGER)"
    )
    return db


def _seed(db, *, can_live=1, camera_number=5, customer_id="cust-1"):
    db.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number) VALUES(?,?,?,?,?)",
        ("cam-1", customer_id, "site-1", "app-1", camera_number),
    )
    db.execute("INSERT INTO partner_users(id,email) VALUES(?,?)", ("user-1", "owner@example.com"))
    if can_live is not None:
        db.execute(
            "INSERT INTO customer_camera_permissions(user_id,camera_id,can_live) VALUES(?,?,?)",
            ("user-1", "cam-1", can_live),
        )


class _ConnectionContext:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self._db

    def __exit__(self, *_args):
        return False


class FakeManifestStore:
    def __init__(self, manifest=None):
        self._manifest = manifest or {"segments": [], "updated_at": None}

    def manifest_for(self, camera_id):
        return self._manifest


IDENTITY = {"role": "customer_owner", "customer_id": "cust-1", "email": "owner@example.com"}


class LivePlaylistRouteTests(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        self._patches = [
            patch.object(live_playlist, "partner_identity", return_value=IDENTITY),
            patch.object(live_playlist, "connection", return_value=_ConnectionContext(self.db)),
            patch.object(live_playlist, "live_manifest_store", FakeManifestStore()),
            patch.object(live_playlist, "get_configured_signer", return_value=lambda message: b"sig"),
        ]
        for p in self._patches:
            p.start()
        self._env_patch = patch.dict(
            os.environ,
            {
                live_playlist.CLOUDFRONT_KEY_PAIR_ID_ENV: "KEYPAIRID123",
                live_playlist.CLOUDFRONT_URL_ENV: "https://d31cxfv0l904ar.cloudfront.net",
            },
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        for p in reversed(self._patches):
            p.stop()
        self.db.close()

    def test_camera_not_found_returns_404(self):
        with self.assertRaises(HTTPException) as ctx:
            live_playlist_endpoint(request=object(), camera_id="cam-missing")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_camera_belonging_to_another_customer_returns_404(self):
        _seed(self.db, customer_id="cust-OTHER")
        with self.assertRaises(HTTPException) as ctx:
            live_playlist_endpoint(request=object(), camera_id="cam-1")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_missing_can_live_row_returns_403(self):
        _seed(self.db, can_live=None)
        with self.assertRaises(HTTPException) as ctx:
            live_playlist_endpoint(request=object(), camera_id="cam-1")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_can_live_zero_returns_403(self):
        _seed(self.db, can_live=0)
        with self.assertRaises(HTTPException) as ctx:
            live_playlist_endpoint(request=object(), camera_id="cam-1")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_unassigned_camera_number_returns_409(self):
        _seed(self.db, can_live=1, camera_number=None)
        with self.assertRaises(HTTPException) as ctx:
            live_playlist_endpoint(request=object(), camera_id="cam-1")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_missing_signing_configuration_returns_503(self):
        _seed(self.db)
        with patch.object(live_playlist, "get_configured_signer", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                live_playlist_endpoint(request=object(), camera_id="cam-1")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_missing_key_id_env_returns_503(self):
        _seed(self.db)
        with patch.dict(os.environ, {live_playlist.CLOUDFRONT_KEY_PAIR_ID_ENV: ""}):
            with self.assertRaises(HTTPException) as ctx:
                live_playlist_endpoint(request=object(), camera_id="cam-1")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_missing_cloudfront_url_env_returns_503(self):
        _seed(self.db)
        with patch.dict(os.environ, {live_playlist.CLOUDFRONT_URL_ENV: ""}):
            with self.assertRaises(HTTPException) as ctx:
                live_playlist_endpoint(request=object(), camera_id="cam-1")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_successful_render_returns_200_with_expected_headers(self):
        _seed(self.db)
        response = live_playlist_endpoint(request=object(), camera_id="cam-1")
        self.assertEqual(response.media_type, "application/vnd.apple.mpegurl")
        self.assertEqual(response.headers["cache-control"], "no-cache, no-store, must-revalidate")
        self.assertIn(b"#EXTM3U", response.body)

    def test_non_customer_owner_role_returns_403(self):
        _seed(self.db)
        with patch.object(live_playlist, "partner_identity", return_value={**IDENTITY, "role": "customer_viewer"}):
            with self.assertRaises(HTTPException) as ctx:
                live_playlist_endpoint(request=object(), camera_id="cam-1")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_stale_manifest_renders_empty_playlist(self):
        _seed(self.db)
        stale_manifest = {
            "segments": [{"key": EXPECTED_PREFIX + "camera1_0.ts", "sequence": 0}],
            "updated_at": time.time() - STALE_MANIFEST_SECONDS - 5,
        }
        with patch.object(live_playlist, "live_manifest_store", FakeManifestStore(stale_manifest)):
            response = live_playlist_endpoint(request=object(), camera_id="cam-1")
        self.assertNotIn(b"#EXTINF", response.body)


if __name__ == "__main__":
    unittest.main()
