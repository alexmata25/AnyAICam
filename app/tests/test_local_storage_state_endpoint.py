"""Local recording storage management (2026-09-17): regression coverage
for GET /api/appliance/local-storage-state (main.py) -- the HTTP-based
replacement for a file-based cross-process handoff that turned out to be
incompatible with a real, deliberate security boundary confirmed live on
Ryzen (STATE_DIR is mounted read-only inside the VMS container). The
appliance-agent's own metrics.py polls this route over localhost.
"""

import pytest
from fastapi.testclient import TestClient

import main
import local_storage_manager as lsm


@pytest.fixture()
def client():
    from cloud_config import settings
    trusted = settings.effective_trusted_hosts or []
    allowed_host = "testserver" if ("*" in trusted or "testserver" in trusted or not trusted) else trusted[0]
    with TestClient(main.app, base_url=f"http://{allowed_host}") as test_client:
        yield test_client


def test_endpoint_is_unauthenticated_matching_health_version(client):
    response = client.get("/api/appliance/local-storage-state")
    assert response.status_code == 200


def test_endpoint_reflects_current_in_process_worker_state(client, monkeypatch):
    monkeypatch.setattr(lsm, "local_storage_manager_state", {
        "worker_status": "running", "storage_state": "warning", "free_percent": 17.3,
        "last_scan_at": "2026-09-17T02:00:00", "last_cleanup_at": "2026-09-16T10:00:00", "last_error": None,
    })
    response = client.get("/api/appliance/local-storage-state")
    body = response.json()
    assert body == {
        "worker_status": "running",
        "storage_state": "warning",
        "free_percent": 17.3,
        "last_cleanup_at": "2026-09-16T10:00:00",
        "last_scan_at": "2026-09-17T02:00:00",
    }


def test_endpoint_reflects_disabled_worker_with_no_data_yet(client, monkeypatch):
    monkeypatch.setattr(lsm, "local_storage_manager_state", {
        "worker_status": "disabled", "storage_state": None, "free_percent": None,
        "last_scan_at": None, "last_cleanup_at": None, "last_error": None,
    })
    response = client.get("/api/appliance/local-storage-state")
    body = response.json()
    assert body["worker_status"] == "disabled"
    assert body["storage_state"] is None
