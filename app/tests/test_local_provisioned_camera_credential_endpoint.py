"""Cloud->edge camera-configuration sync (2026-09-12), local endpoint half.

POST /api/local/provisioned-camera-credential exists so this exact box's
own appliance-agent (never any other caller -- see main.py's
provisioned_camera_credential() module docstring) can hand off the ONE
plaintext camera credential it legitimately holds in memory during
provisioning, so it can be encrypted and persisted locally. These tests
call the route function directly (it is a plain, top-level function --
no FastAPI request parsing is under test here, just its own two
independent access checks and its DB effect) with a minimal Request
stand-in, since TestClient's own ASGI transport reports a fixed
'testclient' host that can't exercise the loopback-allow path directly.
"""
import sqlite3

import pytest

import main
from appliance_protocol import decrypt_camera_credentials
from database_backend import override_target
from partner_db import connection, initialize_database


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    def __init__(self, host, headers=None):
        self.client = _FakeClient(host) if host is not None else None
        self.headers = headers or {}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_local_credential_endpoint.db"


@pytest.fixture(autouse=True)
def _seeded_db(db_path, monkeypatch):
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
    monkeypatch.setenv("ANYAICAM_APPLIANCE_ID", "appl-self")
    monkeypatch.setenv("ANYAICAM_APPLIANCE_CLOUD_ID", "AIC-SELF0001")
    monkeypatch.setenv("ANYAICAM_APPLIANCE_CREDENTIAL", "self-credential")
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", "xdPNoveA5Njb5qzIJHY2ZDFQdwnodQbL_u7ZDEqtaoY=")
    with override_target(sqlite_path=str(db_path)):
        yield


def _call(db_path, host, authorization, payload):
    with override_target(sqlite_path=str(db_path)):
        headers = {"Authorization": authorization} if authorization is not None else {}
        return main.provisioned_camera_credential(_FakeRequest(host, headers), payload)


def _pending_rows(db_path):
    with override_target(sqlite_path=str(db_path)):
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute("SELECT * FROM pending_camera_credentials")]


# ------------------------------------------------------------- access control


def test_non_loopback_client_is_rejected(db_path):
    with pytest.raises(Exception) as excinfo:
        _call(db_path, "192.168.0.55", "Bearer self-credential", {"device_key": "urn:uuid:aaaa", "username": "admin", "password": "hunter2"})
    assert getattr(excinfo.value, "status_code", None) == 403
    assert _pending_rows(db_path) == []


def test_missing_client_is_rejected(db_path):
    with pytest.raises(Exception) as excinfo:
        _call(db_path, None, "Bearer self-credential", {"device_key": "urn:uuid:aaaa", "username": "admin", "password": "hunter2"})
    assert getattr(excinfo.value, "status_code", None) == 403


def test_wrong_bearer_credential_from_loopback_is_rejected(db_path):
    with pytest.raises(Exception) as excinfo:
        _call(db_path, "127.0.0.1", "Bearer not-the-real-credential", {"device_key": "urn:uuid:aaaa", "username": "admin", "password": "hunter2"})
    assert getattr(excinfo.value, "status_code", None) == 403
    assert _pending_rows(db_path) == []


def test_missing_bearer_credential_from_loopback_is_rejected(db_path):
    with pytest.raises(Exception) as excinfo:
        _call(db_path, "127.0.0.1", None, {"device_key": "urn:uuid:aaaa", "username": "admin", "password": "hunter2"})
    assert getattr(excinfo.value, "status_code", None) == 403


def test_no_completed_activation_identity_fails_closed(db_path, monkeypatch):
    monkeypatch.delenv("ANYAICAM_APPLIANCE_ID", raising=False)
    monkeypatch.delenv("ANYAICAM_APPLIANCE_CLOUD_ID", raising=False)
    monkeypatch.delenv("ANYAICAM_APPLIANCE_CREDENTIAL", raising=False)
    with pytest.raises(Exception) as excinfo:
        _call(db_path, "127.0.0.1", "Bearer self-credential", {"device_key": "urn:uuid:aaaa", "username": "admin", "password": "hunter2"})
    assert getattr(excinfo.value, "status_code", None) == 403


# --------------------------------------------------------------- happy path


def test_accepted_from_loopback_with_correct_credential_stores_only_ciphertext(db_path):
    result = _call(db_path, "127.0.0.1", "Bearer self-credential", {"device_key": "urn:uuid:aaaa", "username": "admin", "password": "hunter2-secret"})
    assert "message" in result
    assert "hunter2-secret" not in str(result)
    assert "admin" not in str(result)

    rows = _pending_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["device_key"] == "urn:uuid:aaaa"
    assert b"hunter2-secret" not in bytes(rows[0]["encrypted_blob"])
    assert b"admin" not in bytes(rows[0]["encrypted_blob"])
    decrypted = decrypt_camera_credentials(rows[0]["encrypted_blob"])
    assert decrypted == {"username": "admin", "password": "hunter2-secret"}


def test_ipv6_loopback_is_also_accepted(db_path):
    result = _call(db_path, "::1", "Bearer self-credential", {"device_key": "urn:uuid:bbbb", "username": "admin", "password": "hunter2"})
    assert "message" in result
    assert len(_pending_rows(db_path)) == 1


def test_redelivery_for_the_same_device_key_updates_in_place_not_duplicated(db_path):
    _call(db_path, "127.0.0.1", "Bearer self-credential", {"device_key": "urn:uuid:aaaa", "username": "admin", "password": "first-password"})
    _call(db_path, "127.0.0.1", "Bearer self-credential", {"device_key": "urn:uuid:aaaa", "username": "admin", "password": "second-password"})
    rows = _pending_rows(db_path)
    assert len(rows) == 1
    decrypted = decrypt_camera_credentials(rows[0]["encrypted_blob"])
    assert decrypted["password"] == "second-password"


def test_missing_device_key_is_rejected(db_path):
    with pytest.raises(Exception) as excinfo:
        _call(db_path, "127.0.0.1", "Bearer self-credential", {"device_key": "", "username": "admin", "password": "hunter2"})
    assert getattr(excinfo.value, "status_code", None) == 400
    assert _pending_rows(db_path) == []


# --------------------------------------------- Docker hairpin-NAT gateway (2026-09-13)
#
# Confirmed live: the appliance-agent's own call to this exact box's
# published loopback port (http://127.0.0.1:8000/...) arrives inside the
# anyaicam-vms container with source address 172.18.0.1, not 127.0.0.1 --
# standard Docker hairpin-NAT behavior for "the host talking to its own
# published port", not a misconfiguration. main._docker_bridge_gateway_ip()
# is monkeypatched directly rather than relying on a real /proc/net/route
# (not present on every test-running OS, and irrelevant to what these
# tests actually verify: that trusting the resolved gateway is narrow,
# not a broadened subnet/RFC1918 allowance).


def test_the_resolved_docker_gateway_ip_is_accepted_with_correct_credential(db_path, monkeypatch):
    monkeypatch.setattr(main, "_docker_bridge_gateway_ip", lambda: "172.18.0.1")
    result = _call(db_path, "172.18.0.1", "Bearer self-credential", {"device_key": "urn:uuid:gw1", "username": "admin", "password": "hunter2"})
    assert "message" in result
    assert len(_pending_rows(db_path)) == 1


def test_a_different_nonlocal_address_is_still_rejected_even_with_a_gateway_configured(db_path, monkeypatch):
    """Proves the fix is exactly one dynamically-resolved address, never a
    broadened subnet or arbitrary RFC1918 allowance: a real LAN/remote
    address distinct from the one true gateway must still be refused."""
    monkeypatch.setattr(main, "_docker_bridge_gateway_ip", lambda: "172.18.0.1")
    with pytest.raises(Exception) as excinfo:
        _call(db_path, "192.168.0.55", "Bearer self-credential", {"device_key": "urn:uuid:gw2", "username": "admin", "password": "hunter2"})
    assert getattr(excinfo.value, "status_code", None) == 403
    assert _pending_rows(db_path) == []


def test_gateway_address_without_the_correct_bearer_credential_is_still_rejected(db_path, monkeypatch):
    """The bearer credential remains the actual authority even from the
    trusted gateway address -- the IP check narrows WHERE a call can come
    from, it never substitutes for WHO is allowed to call."""
    monkeypatch.setattr(main, "_docker_bridge_gateway_ip", lambda: "172.18.0.1")
    with pytest.raises(Exception) as excinfo:
        _call(db_path, "172.18.0.1", "Bearer wrong-credential", {"device_key": "urn:uuid:gw3", "username": "admin", "password": "hunter2"})
    assert getattr(excinfo.value, "status_code", None) == 403
    assert _pending_rows(db_path) == []


def test_when_the_gateway_cannot_be_determined_only_literal_loopback_is_accepted(db_path, monkeypatch):
    """Fails closed, never open: if /proc/net/route can't be read or
    parsed, no address is silently trusted beyond literal loopback."""
    monkeypatch.setattr(main, "_docker_bridge_gateway_ip", lambda: None)
    with pytest.raises(Exception) as excinfo:
        _call(db_path, "172.18.0.1", "Bearer self-credential", {"device_key": "urn:uuid:gw4", "username": "admin", "password": "hunter2"})
    assert getattr(excinfo.value, "status_code", None) == 403
    result = _call(db_path, "127.0.0.1", "Bearer self-credential", {"device_key": "urn:uuid:gw4", "username": "admin", "password": "hunter2"})
    assert "message" in result


# ------------------------------------- exempt from the global session-auth middleware


def test_route_is_exempt_from_the_global_browser_auth_middleware():
    """Regression for the actual confirmed-live bug: this file's own
    _call() helper invokes provisioned_camera_credential() directly, so
    every test above it passes even when the real deployed app would 401
    every appliance-agent request before this route's own loopback+bearer
    checks are ever reached. Confirmed via a real failed delivery in
    anyaicam-vms's own access log (Camera 1, AIC-C814766E): a genuine
    loopback call with the correct bearer credential got a generic 401
    "Authentication required" from main.authentication_middleware, not
    from this route. Fixed the same way /api/provisioning/refresh was:
    an exact-path entry in PUBLIC_PATH_PREFIXES, not a broader "/api/local/"
    prefix (this is currently the only route under that prefix, but an
    exact path is the more conservative choice regardless)."""
    assert "/api/local/provisioned-camera-credential" in main.PUBLIC_PATH_PREFIXES
    covered = lambda path: any(path == prefix or path.startswith(prefix) for prefix in main.PUBLIC_PATH_PREFIXES)
    assert covered("/api/local/provisioned-camera-credential")
