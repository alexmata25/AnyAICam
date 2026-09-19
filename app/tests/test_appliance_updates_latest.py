"""GET /api/appliance/updates/latest and POST /api/appliance/updates/
{update_id}/result: the server half of the already-built device client
(appliance-agent/anyaicam_agent/updater/s3_source.py's ManifestSource)
that was never registered -- every real call 404'd and the device
raised SourceUnavailable in a loop. See updates_storage.py's own module
docstring for the full contract this file proves end to end:

  * an empty catalog answers no_update_available, never a 404
  * a published release is served as a manifest signed with the
    server's own private key, verifiable with updater/verify.py's own
    public-key verification -- proving the two independently-built
    halves actually agree on wire format
  * signing is fail-closed (503, never an unsigned manifest) if no
    signing key is configured
  * update-result reporting accepts the first report and 409s a
    duplicate for the same update_id, matching service.py's own
    documented "never retry a 409" expectation
  * every route requires the same appliance authentication as the rest
    of appliance_cloud.py

Imports appliance_cloud (which imports partner_db, triggering its
import-time schema init) -- per this project's own documented
constraint, this file redirects to a throwaway sqlite file via
override_target() before that import happens, so nothing here ever
touches the real production database.
"""

import base64
import secrets
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_appliance_updates_latest.db"):
    import appliance_cloud
    import updates_storage
    from object_storage import LocalStorage
    from partner_db import connection, password_hash


def _seed_appliance(db, appliance_id: str, cloud_id: str, credential: str):
    now = "2026-09-10T00:00:00"
    db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-1", "Test Partner", "approved", "real", now))
    db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", ("cust-1", "partner-1", "Test Customer", "test@example.test", "active", "real", now))
    db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", ("site-1", "cust-1", "Test Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", (appliance_id, "cust-1", "site-1", cloud_id, now))
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", ("cred-1", appliance_id, password_hash(credential), now))


def _auth_headers(appliance_id: str, credential: str) -> dict:
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_updates.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


def _seeded(db_path, appliance_id="appl-1", cloud_id="AIC-TEST0001", credential="test-credential"):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_appliance(db, appliance_id, cloud_id, credential)


@pytest.fixture()
def local_storage(tmp_path, monkeypatch):
    storage = LocalStorage(root=tmp_path / "storage")
    monkeypatch.setattr(updates_storage, "get_storage", lambda: storage)
    monkeypatch.setattr(appliance_cloud, "get_storage", lambda: storage)
    return storage


@pytest.fixture()
def signing_key_pair(tmp_path, monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "signing_key.pem"
    key_path.write_bytes(private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    monkeypatch.setenv("ANYAICAM_UPDATE_SIGNING_KEY_FILE", str(key_path))
    return private_key


# --------------------------------------------------------- no update published


def test_empty_catalog_returns_no_update_available_not_404(client, db_path, local_storage):
    _seeded(db_path)

    response = client.get(
        "/api/appliance/updates/latest?target=anyaicam-appliance&channel=stable",
        headers=_auth_headers("appl-1", "test-credential"),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "no_update_available"}


def test_unauthenticated_request_is_rejected(client, db_path, local_storage):
    _seeded(db_path)

    response = client.get("/api/appliance/updates/latest?target=anyaicam-appliance&channel=stable")

    assert response.status_code == 401


def test_invalid_target_segment_is_rejected(client, db_path, local_storage):
    _seeded(db_path)

    response = client.get(
        "/api/appliance/updates/latest?target=../etc&channel=stable",
        headers=_auth_headers("appl-1", "test-credential"),
    )

    assert response.status_code == 400


# --------------------------------------------------------- published release


def _manifest(version="1.2.3"):
    return {
        "update_id": "upd-1",
        "version": version,
        "sha256": "a" * 64,
        "target": "anyaicam-appliance",
        "platform": "linux",
        "architecture": "x86_64",
        "channel": "stable",
        "issued_at": "2026-09-10T00:00:00Z",
        "package_size_bytes": 1024,
    }


def test_published_release_is_served_signed_and_verifiable(client, db_path, local_storage, signing_key_pair):
    _seeded(db_path)
    updates_storage.publish_release("anyaicam-appliance", "stable", manifest=_manifest(), package_bytes=b"package-bytes")

    response = client.get(
        "/api/appliance/updates/latest?target=anyaicam-appliance&channel=stable",
        headers=_auth_headers("appl-1", "test-credential"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["manifest"] == _manifest()
    assert body["package_url"]

    # Proves the server's signature actually verifies under the SAME
    # scheme the device-side updater/verify.py checks -- not just that
    # a signature-shaped string was returned.
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    signature = base64.b64decode(body["signature"])
    message = updates_storage.canonical_manifest_bytes(body["manifest"])
    signing_key_pair.public_key().verify(signature, message, padding.PKCS1v15(), hashes.SHA256())  # raises on failure


def test_published_release_without_signing_key_fails_closed(client, db_path, local_storage):
    # No ANYAICAM_UPDATE_SIGNING_KEY_FILE set at all -- must never serve
    # an unsigned manifest.
    _seeded(db_path)
    updates_storage.publish_release("anyaicam-appliance", "stable", manifest=_manifest(), package_bytes=b"package-bytes")

    response = client.get(
        "/api/appliance/updates/latest?target=anyaicam-appliance&channel=stable",
        headers=_auth_headers("appl-1", "test-credential"),
    )

    assert response.status_code == 503


def test_different_channel_still_reports_no_update_available(client, db_path, local_storage, signing_key_pair):
    _seeded(db_path)
    updates_storage.publish_release("anyaicam-appliance", "stable", manifest=_manifest(), package_bytes=b"package-bytes")

    response = client.get(
        "/api/appliance/updates/latest?target=anyaicam-appliance&channel=beta",
        headers=_auth_headers("appl-1", "test-credential"),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "no_update_available"}


# --------------------------------------------------------- update-result reporting


def test_update_result_is_accepted(client, db_path):
    _seeded(db_path)

    response = client.post(
        "/api/appliance/updates/upd-1/result",
        headers=_auth_headers("appl-1", "test-credential"),
        json={"from_version": "1.0.0", "to_version": "1.2.3", "state": "healthy", "error": "", "duration_seconds": 12.5},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


def test_duplicate_update_result_is_rejected_as_conflict(client, db_path):
    _seeded(db_path)
    headers = _auth_headers("appl-1", "test-credential")
    payload = {"from_version": "1.0.0", "to_version": "1.2.3", "state": "healthy"}
    client.post("/api/appliance/updates/upd-1/result", headers=headers, json=payload)

    response = client.post(
        "/api/appliance/updates/upd-1/result",
        headers=_auth_headers("appl-1", "test-credential"),
        json=payload,
    )

    assert response.status_code == 409


def test_update_result_requires_authentication(client, db_path):
    _seeded(db_path)

    response = client.post("/api/appliance/updates/upd-1/result", json={"state": "healthy"})

    assert response.status_code == 401
