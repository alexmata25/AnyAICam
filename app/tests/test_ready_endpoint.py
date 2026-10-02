"""Regression coverage for GET /ready -- confirmed live during the first
disposable-EC2 installer validation run: every call 500'd with
"camera_status() missing 1 required positional argument: 'request'".

readiness_snapshot() (called by /ready and several admin/diagnostic
pages) called camera_status() with no arguments, but camera_status()
(the real GET /api/cameras/status handler) required request: Request
with no default -- fine when FastAPI itself injects it for an HTTP call
to that route directly, but readiness_snapshot() has no HTTP request in
scope at all, so every one of its ~10 call sites across the app was
calling a function guaranteed to raise. /ready is the one an appliance's
own health/orchestration tooling depends on, so it always failed.

Fixed by giving camera_status()'s request parameter a None default --
None flows into _customer_playback_cameras(None), whose existing
try/except around partner_identity() already treats that as "no
customer identity" and returns None, which is exactly correct for an
internal readiness check: no portal session to scope to, so it falls
back to this edge appliance's own local, non-customer-scoped camera
status, unchanged from what a real anonymous caller would get.
"""
import pytest
from fastapi.testclient import TestClient

import main
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_ready_endpoint.db"


@pytest.fixture()
def http_client(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app) as test_client:
            yield test_client


def test_ready_endpoint_does_not_500(http_client):
    response = http_client.get("/ready")
    assert response.status_code != 500


def test_ready_endpoint_returns_a_readiness_snapshot(http_client):
    # A bare, freshly-initialized test DB has nothing configured, so a
    # real 503 ("not ready yet") is the correct, honest answer here --
    # this only proves the endpoint completes and returns real JSON
    # instead of crashing with a 500.
    response = http_client.get("/ready")
    assert response.status_code in (200, 503)
    body = response.json()
    assert isinstance(body, dict)


def test_camera_status_with_no_request_falls_back_to_legacy_status(http_client):
    """Direct-call contract readiness_snapshot() relies on: calling
    camera_status() with no arguments at all (no HTTP request in scope)
    must not raise."""
    from main import camera_status
    result = camera_status()
    assert isinstance(result, dict)
    assert "cameras" in result
