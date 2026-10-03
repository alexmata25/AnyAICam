"""GET /api/appliance/updates/latest and POST /api/appliance/updates/
{update_id}/result: the server half of the already-built device client
(appliance-agent/anyaicam_agent/updater/s3_source.py's ManifestSource)
that was never registered -- every real call 404'd and the device
raised SourceUnavailable in a loop. See updates_storage.py's own module
docstring for the full contract this file proves end to end:

  * an empty catalog answers no_update_available, never a 404
  * a published release is served with the signature it was signed
    with OFFLINE (Software Update, 2026-10-03: the server holds no
    private key and never signs; these tests replace the earlier
    server-signing ones), verifiable with updater/verify.py's scheme
  * publishing refuses a bad signature, a package that does not match
    the manifest, or a missing public key; a catalog record without a
    signature is never served (503)
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
    """The test plays the OFFLINE signer: it keeps the private key. The
    server is configured with the public key only."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_path = tmp_path / "update_signing_public.pem"
    public_path.write_bytes(private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    monkeypatch.setenv("ANYAICAM_UPDATE_SIGNING_PUBLIC_KEY_FILE", str(public_path))
    monkeypatch.delenv("ANYAICAM_UPDATE_SIGNING_KEY_FILE", raising=False)
    return private_key


def _offline_sign(private_key, manifest: dict) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    return private_key.sign(updates_storage.canonical_manifest_bytes(manifest), padding.PKCS1v15(), hashes.SHA256())


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


PACKAGE = b"package-bytes"


def _manifest(version="1.2.3", package=PACKAGE):
    import hashlib
    return {
        "update_id": "upd-1",
        "version": version,
        "sha256": hashlib.sha256(package).hexdigest(),
        "target": "anyaicam-appliance",
        "platform": "ubuntu",
        "architecture": "x86_64",
        "channel": "stable",
        "issued_at": "2026-09-10T00:00:00Z",
        "package_size_bytes": len(package),
        "build_id": "b" * 40,
        "migration_safety": "additive",
    }


def test_published_release_is_served_with_its_offline_signature(client, db_path, local_storage, signing_key_pair, monkeypatch):
    # The deployment shape: update storage on S3 (presigned links), the
    # general backend untouched (test_update_storage_backend.py).
    from update_storage_fakes import use_fake_s3_update_storage
    use_fake_s3_update_storage(monkeypatch)
    _seeded(db_path)
    signature = _offline_sign(signing_key_pair, _manifest())
    updates_storage.publish_release("anyaicam-appliance", "stable", manifest=_manifest(), package_bytes=PACKAGE,
                                    signature=signature)

    response = client.get(
        "/api/appliance/updates/latest?target=anyaicam-appliance&channel=stable",
        headers=_auth_headers("appl-1", "test-credential"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["manifest"] == _manifest()
    assert body["package_url"].startswith("https://anyaicam2026.s3.amazonaws.com/updates/")
    assert base64.b64decode(body["signature"]) == signature  # the stored offline signature, not a new one
    # Verifies under the SAME scheme the device-side updater/verify.py checks.
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    message = updates_storage.canonical_manifest_bytes(body["manifest"])
    signing_key_pair.public_key().verify(signature, message, padding.PKCS1v15(), hashes.SHA256())  # raises on failure


def test_a_release_on_local_update_storage_is_not_served_with_an_undownloadable_link(client, db_path, local_storage,
                                                                                    signing_key_pair, monkeypatch):
    monkeypatch.delenv("ANYAICAM_UPDATE_STORAGE_BACKEND", raising=False)
    _seeded(db_path)
    updates_storage.publish_release("anyaicam-appliance", "stable", manifest=_manifest(), package_bytes=PACKAGE,
                                    signature=_offline_sign(signing_key_pair, _manifest()))
    response = client.get("/api/appliance/updates/latest?target=anyaicam-appliance&channel=stable",
                          headers=_auth_headers("appl-1", "test-credential"))
    assert response.status_code == 503  # a relative /storage/ path is never handed to an appliance


def test_the_server_has_no_private_key_signing_code(signing_key_pair):
    assert not hasattr(updates_storage, "load_signing_key")
    assert not hasattr(updates_storage, "sign_manifest")


def test_a_catalog_record_without_a_signature_fails_closed(client, db_path, local_storage):
    import json
    _seeded(db_path)
    local_storage.put("updates", "anyaicam-appliance/stable/upd-1/package.bin", PACKAGE)
    local_storage.put("updates", "anyaicam-appliance/stable/latest.json",
                      json.dumps({"manifest": _manifest(), "package_key": "anyaicam-appliance/stable/upd-1/package.bin"}).encode())

    response = client.get(
        "/api/appliance/updates/latest?target=anyaicam-appliance&channel=stable",
        headers=_auth_headers("appl-1", "test-credential"),
    )

    assert response.status_code == 503


@pytest.mark.parametrize("tamper", ["signature", "package", "manifest"])
def test_publishing_refuses_anything_that_was_not_signed_as_is(local_storage, signing_key_pair, tamper):
    manifest = _manifest()
    signature = _offline_sign(signing_key_pair, manifest)
    package = PACKAGE
    if tamper == "signature":
        signature = bytes([signature[0] ^ 1]) + signature[1:]
    elif tamper == "package":
        package = b"other-bytes!!"
    else:
        manifest = dict(manifest, version="9.9.9")
    with pytest.raises(updates_storage.PublishError):
        updates_storage.publish_release("anyaicam-appliance", "stable", manifest=manifest, package_bytes=package,
                                        signature=signature)
    assert updates_storage.get_latest_release("anyaicam-appliance", "stable") is None


def test_publishing_without_a_configured_public_key_fails_closed(local_storage, monkeypatch):
    monkeypatch.delenv("ANYAICAM_UPDATE_SIGNING_PUBLIC_KEY_FILE", raising=False)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(updates_storage.PublishError):
        updates_storage.publish_release("anyaicam-appliance", "stable", manifest=_manifest(), package_bytes=PACKAGE,
                                        signature=_offline_sign(private_key, _manifest()))


def test_different_channel_still_reports_no_update_available(client, db_path, local_storage, signing_key_pair):
    _seeded(db_path)
    updates_storage.publish_release("anyaicam-appliance", "stable", manifest=_manifest(), package_bytes=PACKAGE,
                                    signature=_offline_sign(signing_key_pair, _manifest()))

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


# --------------------------------------------------------- publish_update_release.py (the staging publish command)


def _cli_files(tmp_path, private_key, manifest=None, package=PACKAGE, tamper_signature=False):
    import json
    manifest = manifest or _manifest()
    signature = _offline_sign(private_key, manifest)
    if tamper_signature:
        signature = bytes([signature[0] ^ 1]) + signature[1:]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "manifest.sig").write_bytes(base64.b64encode(signature) + b"\n")
    (tmp_path / "package.tar.gz").write_bytes(package)
    return ["--manifest", str(tmp_path / "manifest.json"), "--signature", str(tmp_path / "manifest.sig"),
            "--package", str(tmp_path / "package.tar.gz")]


def test_publish_command_verify_only_checks_everything_and_stores_nothing(tmp_path, local_storage, signing_key_pair, capsys):
    import publish_update_release
    assert publish_update_release.main(_cli_files(tmp_path, signing_key_pair) + ["--verify-only"]) == 0
    assert '"status": "verified"' in capsys.readouterr().out
    assert updates_storage.get_latest_release("anyaicam-appliance", "stable") is None


def test_publish_command_publishes_a_correctly_signed_release(client, db_path, tmp_path, local_storage, signing_key_pair, capsys,
                                                               monkeypatch):
    import publish_update_release
    from update_storage_fakes import use_fake_s3_update_storage
    use_fake_s3_update_storage(monkeypatch)
    _seeded(db_path)
    assert publish_update_release.main(_cli_files(tmp_path, signing_key_pair)) == 0
    assert '"status": "published"' in capsys.readouterr().out
    served = client.get("/api/appliance/updates/latest?target=anyaicam-appliance&channel=stable",
                        headers=_auth_headers("appl-1", "test-credential")).json()
    assert served["manifest"] == _manifest()
    assert base64.b64decode(served["signature"]) == _offline_sign(signing_key_pair, _manifest())


@pytest.mark.parametrize("problem", ["bad_signature", "wrong_package", "no_public_key"])
def test_publish_command_refuses_and_stores_nothing(tmp_path, local_storage, signing_key_pair, monkeypatch, capsys, problem):
    import publish_update_release
    args = _cli_files(tmp_path, signing_key_pair, tamper_signature=problem == "bad_signature")
    if problem == "wrong_package":
        (tmp_path / "package.tar.gz").write_bytes(b"a different package")
    if problem == "no_public_key":
        monkeypatch.delenv("ANYAICAM_UPDATE_SIGNING_PUBLIC_KEY_FILE")
    assert publish_update_release.main(args) == 1
    assert '"status": "refused"' in capsys.readouterr().out
    assert updates_storage.get_latest_release("anyaicam-appliance", "stable") is None
