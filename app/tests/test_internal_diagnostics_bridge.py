"""GET /internal/diagnostics: RDM 4's VMS/container-level health
snapshot for the host-level anyaicam-agent's run_diagnostics command.
Every field reuses an existing source (system_metrics(),
internal_camera_status(), recording_uploader.recording_upload_state,
a plain SELECT 1) -- these tests verify the merge and the same
loopback-or-gateway guard /internal/camera-status already established,
not a second health model.

Same environment note as test_internal_camera_status_bridge.py: this
file imports main.py and will show as a pre-existing, unrelated
pytesseract collection error in a sandbox missing that package.
"""

from types import SimpleNamespace

import pytest

import main


def _fake_request(host):
    return SimpleNamespace(client=SimpleNamespace(host=host) if host else None)


def test_rejects_non_loopback_client():
    with pytest.raises(Exception) as excinfo:
        main.internal_diagnostics(_fake_request("203.0.113.5"))
    assert getattr(excinfo.value, "status_code", None) == 403


def test_allows_loopback_and_returns_expected_shape():
    result = main.internal_diagnostics(_fake_request("127.0.0.1"))
    for key in ("service", "version", "runtime_role", "uptime_seconds", "system", "cameras", "recording_upload", "database_reachable", "checked_at"):
        assert key in result


def test_allows_the_docker_gateway_too(monkeypatch):
    monkeypatch.setattr(main, "_docker_gateway_ip", lambda: "172.18.0.1")
    result = main.internal_diagnostics(_fake_request("172.18.0.1"))
    assert result["service"] == "AnyAiCam VMS"


def test_reuses_system_metrics_directly(monkeypatch):
    monkeypatch.setattr(main, "system_metrics", lambda: {"cpu_percent": 12.3, "memory_percent": 45.6, "storage_percent": 7.8, "storage_free_gb": 99.0, "checked_at": "x"})
    result = main.internal_diagnostics(_fake_request("127.0.0.1"))
    assert result["system"]["cpu_percent"] == 12.3


def test_reuses_internal_camera_status_directly(monkeypatch):
    monkeypatch.setattr(main, "internal_camera_status", lambda request: {"cameras": [{"camera": 1, "online": True}], "checked_at": "x"})
    result = main.internal_diagnostics(_fake_request("127.0.0.1"))
    assert result["cameras"] == [{"camera": 1, "online": True}]


def test_reflects_real_recording_upload_worker_state(monkeypatch):
    monkeypatch.setitem(main.recording_uploader.recording_upload_state, "worker_status", "running")
    monkeypatch.setitem(main.recording_uploader.recording_upload_state, "last_error", None)
    result = main.internal_diagnostics(_fake_request("127.0.0.1"))
    assert result["recording_upload"]["worker_status"] == "running"


def test_database_unreachable_is_reported_not_raised(monkeypatch):
    import partner_db

    def _boom():
        raise RuntimeError("db is down")

    monkeypatch.setattr(partner_db, "connection", _boom)
    result = main.internal_diagnostics(_fake_request("127.0.0.1"))  # must not raise
    assert result["database_reachable"] is False
    assert "db is down" in result["database_error"]
