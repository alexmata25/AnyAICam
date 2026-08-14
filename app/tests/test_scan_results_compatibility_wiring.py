"""Wiring tests for the camera-compatibility extension to the existing camera
discovery pipeline: POST /api/appliance/{cloud_id}/scan-jobs/{job_id}
(app.appliance_cloud.secure_scan_results()).

No existing test file covered this route before. These tests call the route
function directly (pulled off a freshly-registered FastAPI app), matching the
"call the function directly, mock the boundary" approach already used in
test_live_relay_session_endpoints.py. authenticate_appliance() and row() are
mocked (already covered elsewhere); connection() is replaced with a small
recording fake so each test can assert exactly which SQL ran and with what
parameter values, without depending on a real database.

Named to sort alphabetically AFTER test_cloud_readiness.py on purpose -- see
test_live_view_page_characterization.py's docstring for the full explanation.
Short version: importing appliance_cloud pulls in partner_db, whose
initialize_database() only runs on the first import of partner_db in the
process; a file that does this earlier in discovery order than
test_cloud_readiness.py (which deletes/re-initializes its own temp sqlite
file at its own import time) would freeze that first import to a different
temp path and break its migrated-tables assertion. This is the same
pre-existing, documented test-suite fragility (docs/AI_HANDOFF.md §8 Phase 0
finding #2), not something introduced here -- the original name for this
file, test_camera_discovery_scan_wiring.py, sorted before test_cloud_readiness.py
and reproduced exactly this failure; renamed to fix it, no other changes.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# See test_live_stream_ffmpeg_characterization.py for why ANYAICAM_PARTNER_DB
# is set explicitly here rather than left unset: importing appliance_cloud
# pulls in partner_db, which freezes its DB path on first import for the rest
# of the process -- point it at a safe temp path, not a real project path.
os.environ.setdefault("ANYAICAM_DATABASE_BACKEND", "sqlite")
os.environ.setdefault(
    "ANYAICAM_PARTNER_DB",
    str(Path(tempfile.gettempdir()) / "anyaicam-camera-compatibility-wiring-test.db"),
)
os.environ.setdefault("ANYAICAM_ENV", "development")

from fastapi import FastAPI  # noqa: E402

import appliance_cloud  # noqa: E402


def _endpoint(app: FastAPI, path: str):
    for candidate_route in app.routes:
        if getattr(candidate_route, "path", None) == path:
            return candidate_route.endpoint
    raise AssertionError(f"route not registered: {path}")


_ROUTE_APP = FastAPI()
appliance_cloud.register_appliance_cloud_routes(_ROUTE_APP, lambda *_args, **_kwargs: "")
secure_scan_results = _endpoint(_ROUTE_APP, "/api/appliance/{cloud_id}/scan-jobs/{job_id}")


class FakeRequest:
    """authenticate_appliance() is mocked in every test below, so the header
    values here are never actually read -- this only exists because the route
    function takes a `request` parameter."""

    def __init__(self):
        self.headers = {}
        self.client = None


class _RecordingDB:
    """Records every db.execute(sql, params) call instead of touching a real
    database, so tests can assert exactly what SQL ran and with what
    parameter values."""

    def __init__(self):
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return MagicMock()

    def insert_camera_calls(self):
        return [(sql, params) for sql, params in self.calls if sql.startswith("INSERT OR IGNORE INTO cameras")]

    def update_job_calls(self):
        return [(sql, params) for sql, params in self.calls if sql.startswith("UPDATE camera_scan_jobs")]


class _ConnectionContext:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self._db

    def __exit__(self, *_args):
        return False


FAKE_APPLIANCE = {"id": "appliance-1", "cloud_id": "TESTCLOUD1", "customer_id": "customer-1", "site_id": "site-1"}
FAKE_JOB = {"id": "job-1", "customer_id": "customer-1", "appliance_id": "appliance-1", "status": "running"}


class SecureScanResultsCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.db = _RecordingDB()
        self._patches = [
            patch.object(appliance_cloud, "authenticate_appliance", return_value=FAKE_APPLIANCE),
            patch.object(appliance_cloud, "row", return_value=FAKE_JOB),
            patch.object(appliance_cloud, "connection", return_value=_ConnectionContext(self.db)),
            patch.object(appliance_cloud, "audit"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def _submit(self, results, status="complete"):
        payload = {"status": status, "progress": 100, "results": results, "message": "done"}
        return secure_scan_results(FakeRequest(), "TESTCLOUD1", "job-1", payload)

    def _stored_results_json(self):
        # The compatibility-enriched results are what gets JSON-serialized
        # into the UPDATE camera_scan_jobs call -- this is also exactly what
        # the customer sees via GET /api/customer/camera-scans/{job_id}.
        update_calls = self.db.update_job_calls()
        self.assertEqual(len(update_calls), 1)
        _sql, params = update_calls[0]
        results_json = params[2]  # status,progress,results_json,message,updated_at,job_id
        return json.loads(results_json)

    def test_approved_camera_is_inserted_with_status_discovered_unchanged(self):
        # Existing wired-camera behavior: RTSP+ONVIF confirmed -> APPROVED ->
        # inserted exactly as before this feature existed.
        result = self._submit([{"id": "camera-1", "name": "Front Door", "onvif_support": True, "rtsp_support": True}])
        self.assertEqual(result["message"], "Discovery result accepted.")
        insert_calls = self.db.insert_camera_calls()
        self.assertEqual(len(insert_calls), 1)
        _sql, params = insert_calls[0]
        self.assertEqual(params[0], "camera-1")  # id
        self.assertEqual(params[6], "discovered")  # status column

    def test_partially_supported_camera_is_inserted_with_status_needs_review(self):
        result = self._submit([{"id": "camera-2", "name": "Backyard", "onvif_support": True, "rtsp_support": False}])
        self.assertEqual(result["message"], "Discovery result accepted.")
        insert_calls = self.db.insert_camera_calls()
        self.assertEqual(len(insert_calls), 1)
        _sql, params = insert_calls[0]
        self.assertEqual(params[0], "camera-2")
        self.assertEqual(params[6], "needs_review")

    def test_not_supported_camera_is_never_inserted(self):
        result = self._submit([{"id": "camera-3", "name": "Unknown Device", "onvif_support": False, "rtsp_support": False}])
        self.assertEqual(result["message"], "Discovery result accepted.")
        self.assertEqual(self.db.insert_camera_calls(), [])

    def test_not_supported_camera_still_appears_in_stored_results_with_reasons(self):
        # Excluded from `cameras`, but never silently hidden from the customer --
        # the scan-results screen (GET /api/customer/camera-scans/{job_id})
        # reads this same results_json.
        self._submit([{"id": "camera-3", "onvif_support": False, "rtsp_support": False}])
        stored = self._stored_results_json()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["compatibility_status"], "NOT_SUPPORTED")
        self.assertTrue(stored[0]["compatibility_reasons"])

    def test_mixed_batch_one_not_supported_does_not_affect_the_others(self):
        result = self._submit([
            {"id": "camera-approved", "onvif_support": True, "rtsp_support": True},
            {"id": "camera-not-supported", "onvif_support": False, "rtsp_support": False},
            {"id": "camera-needs-review", "onvif_support": None, "rtsp_support": True},
        ])
        self.assertEqual(result["message"], "Discovery result accepted.")
        insert_calls = self.db.insert_camera_calls()
        inserted_ids = {params[0] for _sql, params in insert_calls}
        self.assertEqual(inserted_ids, {"camera-approved", "camera-needs-review"})
        statuses = {params[0]: params[6] for _sql, params in insert_calls}
        self.assertEqual(statuses["camera-approved"], "discovered")
        self.assertEqual(statuses["camera-needs-review"], "needs_review")

    def test_wifi_and_wired_camera_with_identical_capabilities_get_identical_outcome(self):
        result = self._submit([
            {"id": "camera-wired", "onvif_support": True, "rtsp_support": True, "transport": "wired"},
            {"id": "camera-wifi", "onvif_support": True, "rtsp_support": True, "transport": "wifi"},
        ])
        self.assertEqual(result["message"], "Discovery result accepted.")
        insert_calls = self.db.insert_camera_calls()
        statuses = {params[0]: params[6] for _sql, params in insert_calls}
        self.assertEqual(statuses["camera-wired"], "discovered")
        self.assertEqual(statuses["camera-wifi"], "discovered")
        stored = self._stored_results_json()
        by_id = {item["id"]: item for item in stored}
        self.assertEqual(by_id["camera-wired"]["compatibility_status"], by_id["camera-wifi"]["compatibility_status"])
        self.assertEqual(by_id["camera-wired"]["compatibility_reasons"], by_id["camera-wifi"]["compatibility_reasons"])
        self.assertEqual(by_id["camera-wired"]["transport"], "wired")
        self.assertEqual(by_id["camera-wifi"]["transport"], "wifi")

    def test_credentials_are_stripped_before_compatibility_evaluation_and_never_stored(self):
        # sanitize_appliance_payload() already strips these keys before the
        # compatibility engine (or the DB) ever sees the payload -- this locks
        # down that the ordering is preserved by this feature, not weakened.
        self._submit([{
            "id": "camera-4", "onvif_support": True, "rtsp_support": True,
            "username": "admin", "password": "hunter2", "rtsp_url": "rtsp://admin:hunter2@10.0.0.5/",
            "credentials": "secret-blob", "secret": "also-secret",
        }])
        stored = self._stored_results_json()
        self.assertEqual(len(stored), 1)
        for forbidden in ("username", "password", "rtsp_url", "credentials", "secret"):
            self.assertNotIn(forbidden, stored[0])
        insert_calls = self.db.insert_camera_calls()
        for _sql, params in insert_calls:
            for value in params:
                if isinstance(value, str):
                    self.assertNotIn("hunter2", value)
                    self.assertNotIn("rtsp://", value)

    def test_running_status_does_not_touch_the_cameras_table(self):
        # Only a 'complete' submission ever evaluates compatibility or inserts
        # cameras -- existing 'running'/'error' behavior is unchanged.
        result = self._submit([{"id": "camera-5", "onvif_support": True, "rtsp_support": True}], status="running")
        self.assertEqual(result["message"], "Discovery result accepted.")
        self.assertEqual(self.db.insert_camera_calls(), [])

    def test_camera_without_an_id_still_gets_a_generated_one_and_is_evaluated(self):
        result = self._submit([{"name": "No ID Camera", "onvif_support": True, "rtsp_support": True}])
        self.assertEqual(result["message"], "Discovery result accepted.")
        insert_calls = self.db.insert_camera_calls()
        self.assertEqual(len(insert_calls), 1)
        _sql, params = insert_calls[0]
        self.assertTrue(params[0])  # a generated id, non-empty
        self.assertEqual(params[6], "discovered")


if __name__ == "__main__":
    unittest.main()
