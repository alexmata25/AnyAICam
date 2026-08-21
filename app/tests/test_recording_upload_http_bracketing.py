"""Diagnostic-only http_call_begin/http_call_returned bracketing around
the two urlopen() calls in _control_plane_post()/_control_plane_get().
Built to distinguish "never even attempted this network call" from
"attempted it and it never returned" from "returned but something
after it failed" -- the pilot appliance showed scan_number=1 logged
and then nothing at all for 2+ minutes, and every existing exception
handler on these two calls already logs on failure, so the silence
itself pointed at a call that neither returns nor raises.

These tests only ever verify the new log lines and that every existing
behavior (return value, exception handling, warning messages) is
completely unchanged -- no request/timeout/retry/DNS/credential/S3/
cutoff/scan-timing behavior is touched by this change, and these tests
prove exactly that boundary.
"""

import json
import logging
import urllib.error
from unittest.mock import MagicMock

import pytest

import recording_uploader as ru


@pytest.fixture(autouse=True)
def _fake_identity(monkeypatch):
    monkeypatch.setattr(ru, "_load_appliance_identity", lambda: ("appliance-1", "cred-1"))
    monkeypatch.setattr(ru, "CLOUD_URL", "https://app.example.test")


def _fake_response(body: dict):
    ctx = MagicMock()
    ctx.__enter__.return_value.read.return_value = json.dumps(body).encode()
    return ctx


# --------------------------------------------------------------- POST


def test_post_logs_begin_then_returned_on_success(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="anyaicam.recording_uploader")
    monkeypatch.setattr(ru.urllib.request, "urlopen", lambda *a, **k: _fake_response({"status": "accepted"}))

    result = ru._control_plane_post("/api/appliance/recordings/cam-1/credentials", {})

    assert result == {"status": "accepted"}  # existing return-value behavior unchanged
    messages = [r.message for r in caplog.records]
    begin_idx = messages.index("recording_upload.http_call_begin path=/api/appliance/recordings/cam-1/credentials")
    returned_idx = messages.index("recording_upload.http_call_returned path=/api/appliance/recordings/cam-1/credentials")
    assert begin_idx < returned_idx  # begin strictly before returned


def test_post_logs_begin_but_not_returned_on_http_error(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="anyaicam.recording_uploader")

    def _raise(*a, **k):
        raise urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)

    monkeypatch.setattr(ru.urllib.request, "urlopen", _raise)

    result = ru._control_plane_post("/api/appliance/recordings/cam-1/credentials", {})

    assert result is None  # existing fail-closed behavior unchanged
    messages = [r.message for r in caplog.records]
    assert "recording_upload.http_call_begin path=/api/appliance/recordings/cam-1/credentials" in messages
    assert "recording_upload.http_call_returned path=/api/appliance/recordings/cam-1/credentials" not in messages
    assert any("control_plane_http_error" in m for m in messages)  # existing warning still fires unchanged


def test_post_logs_begin_but_not_returned_on_unreachable(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="anyaicam.recording_uploader")

    def _raise(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(ru.urllib.request, "urlopen", _raise)

    result = ru._control_plane_post("/api/appliance/recordings/cam-1/credentials", {})

    assert result is None
    messages = [r.message for r in caplog.records]
    assert "recording_upload.http_call_begin path=/api/appliance/recordings/cam-1/credentials" in messages
    assert "recording_upload.http_call_returned path=/api/appliance/recordings/cam-1/credentials" not in messages
    assert any("control_plane_unreachable" in m for m in messages)


def test_post_returns_none_without_calling_urlopen_when_cloud_url_unset(monkeypatch, caplog):
    """Existing fail-closed short-circuit (before the network call is
    ever attempted) must stay completely unchanged -- no begin/returned
    log at all in this path."""
    caplog.set_level(logging.INFO, logger="anyaicam.recording_uploader")
    monkeypatch.setattr(ru, "CLOUD_URL", "")
    called = []
    monkeypatch.setattr(ru.urllib.request, "urlopen", lambda *a, **k: called.append(1))

    result = ru._control_plane_post("/api/appliance/recordings/cam-1/credentials", {})

    assert result is None
    assert called == []
    messages = [r.message for r in caplog.records]
    assert not any("http_call_begin" in m for m in messages)


# ---------------------------------------------------------------- GET


def test_get_logs_begin_then_returned_on_success(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="anyaicam.recording_uploader")
    monkeypatch.setattr(ru.urllib.request, "urlopen", lambda *a, **k: _fake_response({"cameras": []}))

    result = ru._control_plane_get("/api/appliance/configuration")

    assert result == {"cameras": []}
    messages = [r.message for r in caplog.records]
    begin_idx = messages.index("recording_upload.http_call_begin path=/api/appliance/configuration")
    returned_idx = messages.index("recording_upload.http_call_returned path=/api/appliance/configuration")
    assert begin_idx < returned_idx


def test_get_logs_begin_but_not_returned_on_http_error(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="anyaicam.recording_uploader")

    def _raise(*a, **k):
        raise urllib.error.HTTPError("http://x", 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(ru.urllib.request, "urlopen", _raise)

    result = ru._control_plane_get("/api/appliance/configuration")

    assert result is None
    messages = [r.message for r in caplog.records]
    assert "recording_upload.http_call_begin path=/api/appliance/configuration" in messages
    assert "recording_upload.http_call_returned path=/api/appliance/configuration" not in messages


def test_get_still_uses_timeout_of_ten_seconds(monkeypatch):
    """Regression guard: the bracketing must not have disturbed the
    existing timeout value."""
    captured = {}

    def _capture(request, timeout=None):
        captured["timeout"] = timeout
        return _fake_response({"cameras": []})

    monkeypatch.setattr(ru.urllib.request, "urlopen", _capture)
    ru._control_plane_get("/api/appliance/configuration")
    assert captured["timeout"] == 10


def test_post_still_uses_timeout_of_ten_seconds(monkeypatch):
    captured = {}

    def _capture(request, timeout=None):
        captured["timeout"] = timeout
        return _fake_response({"status": "accepted"})

    monkeypatch.setattr(ru.urllib.request, "urlopen", _capture)
    ru._control_plane_post("/api/appliance/recordings/cam-1/credentials", {})
    assert captured["timeout"] == 10
