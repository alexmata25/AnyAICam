"""RDM-2 Group 2D (docs/AI_HANDOFF.md): tests for the new update-result
reporting route in app/appliance_cloud.py:
POST /api/appliance/updates/{update_id}/result

Route function is pulled directly off a freshly-registered FastAPI app and
called as a plain Python function (no TestClient/ASGI needed), matching the
approach already established in test_live_relay_session_endpoints.py.

Unlike that file, this endpoint's entire job is durable persistence, so
these tests exercise REAL connection()/rows() round-trips against a real
temporary SQLite database (ANYAICAM_PARTNER_DB) rather than mocking `row`.
authenticate_appliance() is mocked to return a fixed appliance dict --
that mechanism is already covered elsewhere (test_appliance_protocol.py /
the real authenticate_appliance() implementation itself).

No network, no AWS.
"""

import os
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
    str(Path(tempfile.gettempdir()) / "anyaicam-rdm2-2d-update-history-test.db"),
)
os.environ.setdefault("ANYAICAM_ENV", "development")
# See test_live_relay_session_endpoints.py for why this is set explicitly --
# importing appliance_cloud constructs a module-level LiveManifestStore.
os.environ.setdefault(
    "ANYAICAM_LIVE_MANIFEST_FILE",
    str(Path(tempfile.gettempdir()) / "anyaicam-rdm2-2d-live-manifest-import-guard.json"),
)

from fastapi import FastAPI, HTTPException  # noqa: E402

import appliance_cloud  # noqa: E402
from partner_db import connection, rows  # noqa: E402


def _endpoint(app: FastAPI, path: str):
    for candidate_route in app.routes:
        if getattr(candidate_route, "path", None) == path:
            return candidate_route.endpoint
    raise AssertionError(f"route not registered: {path}")


_ROUTE_APP = FastAPI()
appliance_cloud.register_appliance_cloud_routes(_ROUTE_APP, lambda *_args, **_kwargs: "")
update_result = _endpoint(_ROUTE_APP, "/api/appliance/updates/{update_id}/result")


class FakeRequest:
    """authenticate_appliance() is mocked in every test below (except the
    dedicated auth test), so header values here are never actually read."""

    def __init__(self):
        self.headers = {}
        self.client = None


FAKE_APPLIANCE = {"id": "appliance-1", "cloud_id": "TESTCLOUD1", "customer_id": "customer-1", "site_id": "site-1"}
OTHER_APPLIANCE = {"id": "appliance-2", "cloud_id": "TESTCLOUD2", "customer_id": "customer-2", "site_id": "site-2"}


def setUpModule():
    # partner_db.initialize_database() (which creates every base table AND
    # runs apply_migrations()) only runs ONCE, as a module-level side
    # effect of partner_db's own first import -- whichever test module
    # imports partner_db first "wins" that one-time initialization for
    # whatever ANYAICAM_PARTNER_DB path is current at that moment. This
    # module cannot assume it is that first importer (another test module
    # imported earlier in the same process may have already claimed it
    # against a DIFFERENT db path), so it explicitly re-runs
    # initialize_database() here -- safe/idempotent (every statement is
    # CREATE TABLE IF NOT EXISTS / ALTER TABLE ADD COLUMN-if-missing /
    # apply_migrations()'s own schema_migrations-gated re-entrancy) --
    # rather than relying on import-order luck.
    import partner_db
    partner_db.initialize_database()

    # appliance_update_history has a real FOREIGN KEY(appliance_id)
    # REFERENCES appliances(id), enforced by database_backend.connect()'s
    # PRAGMA foreign_keys=ON -- these tests exercise the real constraint
    # rather than working around it, so FAKE_APPLIANCE/OTHER_APPLIANCE
    # need real (idempotent, INSERT OR IGNORE) rows across the full
    # partners -> customers -> sites -> appliances chain.
    now = "2026-01-01T00:00:00"
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('partner-1','Test Partner','approved','real',?)", (now,))
        db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('customer-1','partner-1','Customer One','customer-one@example.invalid','active','real',?)", (now,))
        db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('customer-2','partner-1','Customer Two','customer-two@example.invalid','active','real',?)", (now,))
        db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','customer-1','Site One',?)", (now,))
        db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-2','customer-2','Site Two',?)", (now,))
        db.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appliance-1','customer-1','site-1','TESTCLOUD1',?)", (now,))
        db.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appliance-2','customer-2','site-2','TESTCLOUD2',?)", (now,))


def _payload(**overrides):
    body = {
        "update_id": "upd-1",
        "from_version": "1.0.0",
        "to_version": "1.1.0",
        "state": "healthy",
        "error": "",
        "rollback_from": None,
        "duration_seconds": 4.2,
    }
    body.update(overrides)
    return body


class UpdateResultEndpointTestCase(unittest.TestCase):
    def setUp(self):
        with connection() as db:
            db.execute("DELETE FROM appliance_update_history")
        self._patches = [
            patch.object(appliance_cloud, "authenticate_appliance", return_value=FAKE_APPLIANCE),
            patch.object(appliance_cloud, "audit"),
            patch.object(appliance_cloud, "REMOTE_UPDATE_ENABLED", True),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def _rows_for(self, appliance_id="appliance-1", update_id="upd-1"):
        return rows(
            "SELECT * FROM appliance_update_history WHERE appliance_id=? AND update_id=?",
            (appliance_id, update_id),
        )


# -- authentication -----------------------------------------------------

class AuthTests(unittest.TestCase):
    def test_auth_failure_propagates_before_anything_else(self):
        def _raise(request):
            raise HTTPException(status_code=401, detail="Appliance authentication headers are required.")
        with patch.object(appliance_cloud, "authenticate_appliance", side_effect=_raise), \
                patch.object(appliance_cloud, "REMOTE_UPDATE_ENABLED", True):
            with self.assertRaises(HTTPException) as ctx:
                update_result(FakeRequest(), "upd-1", _payload())
        self.assertEqual(ctx.exception.status_code, 401)


# -- valid results --------------------------------------------------------

class ValidResultTests(UpdateResultEndpointTestCase):
    def test_valid_healthy_result_is_recorded(self):
        result = update_result(FakeRequest(), "upd-1", _payload(state="healthy"))
        self.assertEqual(result, {"status": "accepted"})
        rows_ = self._rows_for()
        self.assertEqual(len(rows_), 1)
        self.assertEqual(rows_[0]["state"], "healthy")
        self.assertEqual(rows_[0]["from_version"], "1.0.0")
        self.assertEqual(rows_[0]["to_version"], "1.1.0")

    def test_valid_rolled_back_result_is_recorded(self):
        result = update_result(FakeRequest(), "upd-1", _payload(state="rolled_back", rollback_from="1.1.0"))
        self.assertEqual(result, {"status": "accepted"})
        rows_ = self._rows_for()
        self.assertEqual(rows_[0]["state"], "rolled_back")
        self.assertEqual(rows_[0]["rollback_from"], "1.1.0")

    def test_valid_rollback_failed_result_is_recorded(self):
        result = update_result(
            FakeRequest(), "upd-1",
            _payload(state="rollback_failed", error="no prior version to roll back to"),
        )
        self.assertEqual(result, {"status": "accepted"})
        rows_ = self._rows_for()
        self.assertEqual(rows_[0]["state"], "rollback_failed")
        self.assertEqual(rows_[0]["error"], "no prior version to roll back to")

    def test_valid_activation_failed_and_restarting_are_also_accepted(self):
        # Broad accepted-state set (approved decision): resume_if_pending()
        # can conclude with these too, not only the three terminal states.
        update_result(FakeRequest(), "upd-1", _payload(state="restarting"))
        update_result(FakeRequest(), "upd-2", _payload(update_id="upd-2", state="activation_failed"))
        states = {item["state"] for item in self._rows_for()} | {item["state"] for item in self._rows_for(update_id="upd-2")}
        self.assertEqual(states, {"restarting", "activation_failed"})


# -- idempotent replay / conflicts -----------------------------------------

class ReplayAndConflictTests(UpdateResultEndpointTestCase):
    def test_duplicate_identical_replay_is_a_200_noop(self):
        first = update_result(FakeRequest(), "upd-1", _payload(state="healthy"))
        second = update_result(FakeRequest(), "upd-1", _payload(state="healthy"))
        self.assertEqual(first, {"status": "accepted"})
        self.assertEqual(second, {"status": "accepted", "duplicate": True})
        self.assertEqual(len(self._rows_for()), 1)  # not inserted twice

    def test_same_state_different_content_is_409(self):
        update_result(FakeRequest(), "upd-1", _payload(state="healthy", duration_seconds=4.2))
        with self.assertRaises(HTTPException) as ctx:
            update_result(FakeRequest(), "upd-1", _payload(state="healthy", duration_seconds=9.9))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(len(self._rows_for()), 1)  # the original row is untouched

    def test_a_different_terminal_state_for_an_already_terminal_update_id_is_409(self):
        update_result(FakeRequest(), "upd-1", _payload(state="healthy"))
        with self.assertRaises(HTTPException) as ctx:
            update_result(FakeRequest(), "upd-1", _payload(state="rollback_failed"))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(len(self._rows_for()), 1)

    def test_an_interim_restarting_report_followed_by_a_final_state_is_not_a_conflict(self):
        # The legitimate progression this append-only design exists for:
        # a rollback triggered mid-resume reports 'restarting' first, then
        # its OWN later restart reports the real conclusion.
        update_result(FakeRequest(), "upd-1", _payload(state="restarting"))
        result = update_result(FakeRequest(), "upd-1", _payload(state="rolled_back", rollback_from="1.0.0"))
        self.assertEqual(result, {"status": "accepted"})
        states = {item["state"] for item in self._rows_for()}
        self.assertEqual(states, {"restarting", "rolled_back"})


# -- malformed / invalid payloads -------------------------------------------

class MalformedPayloadTests(UpdateResultEndpointTestCase):
    def test_path_and_body_update_id_mismatch_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            update_result(FakeRequest(), "upd-1", _payload(update_id="upd-DIFFERENT"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self._rows_for(), [])

    def test_invalid_state_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            update_result(FakeRequest(), "upd-1", _payload(state="downloading"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_missing_required_field_is_400(self):
        payload = _payload()
        del payload["from_version"]
        with self.assertRaises(HTTPException) as ctx:
            update_result(FakeRequest(), "upd-1", payload)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_negative_duration_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            update_result(FakeRequest(), "upd-1", _payload(duration_seconds=-1))
        self.assertEqual(ctx.exception.status_code, 400)


# -- feature flag -----------------------------------------------------------

class FeatureFlagTests(UpdateResultEndpointTestCase):
    def test_disabled_flag_returns_404_before_touching_the_database(self):
        with patch.object(appliance_cloud, "REMOTE_UPDATE_ENABLED", False):
            with self.assertRaises(HTTPException) as ctx:
                update_result(FakeRequest(), "upd-1", _payload())
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self._rows_for(), [])


# -- ownership scoping --------------------------------------------------------

class OwnershipScopingTests(UpdateResultEndpointTestCase):
    def test_another_appliance_reporting_the_same_update_id_does_not_conflict(self):
        # Structural scoping: every row is keyed by the AUTHENTICATED
        # appliance_id, so two different appliances can use the identical
        # update_id string without ever colliding.
        update_result(FakeRequest(), "upd-1", _payload(state="healthy"))
        with patch.object(appliance_cloud, "authenticate_appliance", return_value=OTHER_APPLIANCE):
            result = update_result(FakeRequest(), "upd-1", _payload(state="rollback_failed"))
        self.assertEqual(result, {"status": "accepted"})
        self.assertEqual(len(self._rows_for(appliance_id="appliance-1")), 1)
        self.assertEqual(len(self._rows_for(appliance_id="appliance-2")), 1)


if __name__ == "__main__":
    unittest.main()
