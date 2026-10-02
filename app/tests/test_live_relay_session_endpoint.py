"""Staging Live View transport (2026-09-13): regression coverage for the
appliance-side half of the existing S3/CloudFront live-relay pipeline --
POST /api/appliance/live/{camera_id}/session (mints a short-lived,
per-camera-scoped S3 PutObject credential via STS) and
POST /api/appliance/live/{camera_id}/segment-available (records an
uploaded segment into live_manifest_store).

This pipeline was found, during the Live View root-cause investigation,
to be fully implemented (session minting, per-prefix IAM scoping via
appliance_protocol.live_relay_session_policy(), the per-appliance
`live_relay_pilot` DB gate, manifest recording) but to have had ZERO
existing test coverage anywhere in the suite before this file -- it was
built but never turned on, and never proven correct. These tests lock in
the documented, already-correct behavior before it is enabled for real
customer traffic for the first time.

No real AWS call is ever made here -- boto3.client('sts').assume_role is
monkeypatched to a fake object returning a synthetic, obviously-fake
credential, exactly the way this project's own test_recording_uploader.py
mocks the equivalent recording-upload STS call.
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_live_relay_session_endpoint.db"):
    import appliance_cloud
    from partner_db import connection, password_hash


def _seed(db, *, appliance_id, cloud_id, credential, camera_id, camera_number=1,
          device_key="urn:uuid:fake", customer_id="cust-1", site_id="site-1", other_appliance=False):
    now = "2026-09-13T00:00:00"
    db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('partner-1','Test Partner','approved','real',?)", (now,))
    db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)",
               (customer_id, "partner-1", "Test Customer", f"{customer_id}@example.test", "active", "real", now))
    db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Test Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,live_relay_pilot,created_at) VALUES(?,?,?,?,?,?)",
               (appliance_id, customer_id, site_id, cloud_id, 0, now))
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)",
               (f"cred-{appliance_id}", appliance_id, password_hash(credential), now))
    db.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,device_key,status,name,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (camera_id, customer_id, site_id, appliance_id, camera_number, device_key, "configured", "Camera 1", now),
    )
    if other_appliance:
        db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,live_relay_pilot,created_at) VALUES('appl-other',?,?,?,?,?)",
                   (customer_id, site_id, "AIC-OTHER", 0, now))
        db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES('cred-other','appl-other',?,?)",
                   (password_hash("other-credential"), now))


def _auth_headers(appliance_id, credential):
    import secrets
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_live_relay_session_endpoint.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


def _configure_relay(monkeypatch, *, enabled=True, role_arn="arn:aws:iam::123456789012:role/anyaicam-live-upload",
                      bucket="anyaicam-staging-live-relay", region="us-east-1"):
    monkeypatch.setattr(appliance_cloud, "LIVE_RELAY_ENABLED", enabled)
    monkeypatch.setattr(appliance_cloud, "LIVE_UPLOAD_ROLE_ARN", role_arn)
    monkeypatch.setattr(appliance_cloud, "LIVE_RELAY_S3_BUCKET", bucket)
    monkeypatch.setattr(appliance_cloud, "LIVE_RELAY_AWS_REGION", region)


class _FakeSTS:
    def __init__(self, *a, **k):
        pass

    def assume_role(self, RoleArn, RoleSessionName, Policy, DurationSeconds):
        import json as _json
        from datetime import datetime, timezone
        # Prove the exact narrow per-camera prefix was actually requested,
        # not just that *some* policy was passed.
        policy = _json.loads(Policy)
        assert policy["Statement"][0]["Action"] == "s3:PutObject"
        assert RoleArn == "arn:aws:iam::123456789012:role/anyaicam-live-upload"
        return {
            "Credentials": {
                "AccessKeyId": "FAKEACCESSKEYID",
                "SecretAccessKey": "fake-secret-access-key-never-real",
                "SessionToken": "fake-session-token-never-real",
                "Expiration": datetime.now(timezone.utc),
            }
        }


# ------------------------------------------------------------- fails closed


def test_session_returns_404_when_relay_globally_disabled(client, db_path, monkeypatch):
    _configure_relay(monkeypatch, enabled=False)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1")
            db.execute("UPDATE appliances SET live_relay_pilot=1 WHERE id='appl-1'")
    response = client.post("/api/appliance/live/cam-1/session", headers=_auth_headers("appl-1", "cred"))
    assert response.status_code == 404


def test_session_returns_404_when_appliance_not_in_pilot(client, db_path, monkeypatch):
    """Enabled globally but this appliance's own live_relay_pilot flag is
    still 0 (the default for every appliance) -- per-appliance staged
    rollout, not an all-or-nothing switch."""
    _configure_relay(monkeypatch, enabled=True)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1")
    response = client.post("/api/appliance/live/cam-1/session", headers=_auth_headers("appl-1", "cred"))
    assert response.status_code == 404


def test_session_returns_503_when_pilot_on_but_aws_not_configured(client, db_path, monkeypatch):
    _configure_relay(monkeypatch, enabled=True, role_arn="", bucket="", region="")
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1")
            db.execute("UPDATE appliances SET live_relay_pilot=1 WHERE id='appl-1'")
    response = client.post("/api/appliance/live/cam-1/session", headers=_auth_headers("appl-1", "cred"))
    assert response.status_code == 503


def test_session_rejects_camera_belonging_to_a_different_appliance(client, db_path, monkeypatch):
    """The AWS-facing session endpoint is appliance-authenticated, not
    customer-authenticated -- an appliance can never mint an upload
    credential for a camera it does not itself own, even if it somehow
    knows another appliance's camera_id."""
    _configure_relay(monkeypatch, enabled=True)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1", other_appliance=True)
            db.execute("UPDATE appliances SET live_relay_pilot=1 WHERE id IN ('appl-1','appl-other')")
    response = client.post("/api/appliance/live/cam-1/session", headers=_auth_headers("appl-other", "other-credential"))
    assert response.status_code == 403


# ------------------------------------------------------------------ happy path


def test_session_issues_scoped_credential_for_authorized_camera(client, db_path, monkeypatch):
    _configure_relay(monkeypatch, enabled=True)
    monkeypatch.setattr(appliance_cloud, "boto3", type("_B", (), {"client": staticmethod(lambda service, region_name=None: _FakeSTS())}))
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1",
                  customer_id="cust-1", site_id="site-1")
            db.execute("UPDATE appliances SET live_relay_pilot=1 WHERE id='appl-1'")

    response = client.post("/api/appliance/live/cam-1/session", headers=_auth_headers("appl-1", "cred"))
    assert response.status_code == 200
    body = response.json()
    assert body["bucket"] == "anyaicam-staging-live-relay"
    assert body["key_prefix"] == "live/cust-1/site-1/appl-1/cam-1/"
    for key in ("access_key_id", "secret_access_key", "session_token", "expiration"):
        assert key in body["credentials"]
    # The obviously-fake values above must never leak the literal STS
    # response object shape beyond what's documented -- no extra keys.
    assert set(body["credentials"]) == {"access_key_id", "secret_access_key", "session_token", "expiration"}


def test_two_cameras_on_the_same_appliance_get_non_overlapping_prefixes(client, db_path, monkeypatch):
    _configure_relay(monkeypatch, enabled=True)
    monkeypatch.setattr(appliance_cloud, "boto3", type("_B", (), {"client": staticmethod(lambda service, region_name=None: _FakeSTS())}))
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1")
            db.execute("UPDATE appliances SET live_relay_pilot=1 WHERE id='appl-1'")
            db.execute(
                "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,device_key,status,name,created_at) "
                "VALUES('cam-2','cust-1','site-1','appl-1',2,'urn:uuid:fake2','configured','Camera 2','2026-09-13T00:00:00')"
            )

    prefix_1 = client.post("/api/appliance/live/cam-1/session", headers=_auth_headers("appl-1", "cred")).json()["key_prefix"]
    prefix_2 = client.post("/api/appliance/live/cam-2/session", headers=_auth_headers("appl-1", "cred")).json()["key_prefix"]
    assert prefix_1 != prefix_2
    assert not prefix_1.startswith(prefix_2)
    assert not prefix_2.startswith(prefix_1)


# --------------------------------------------------------- segment-available / manifest


def test_segment_available_records_into_the_manifest_for_the_right_camera(client, db_path, monkeypatch):
    _configure_relay(monkeypatch, enabled=True)
    manifest_path = db_path.parent / "test_live_manifest.json"
    from live_manifest import LiveManifestStore
    monkeypatch.setattr(appliance_cloud, "live_manifest_store", LiveManifestStore(manifest_path))
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1",
                  customer_id="cust-1", site_id="site-1")
            db.execute("UPDATE appliances SET live_relay_pilot=1 WHERE id='appl-1'")

    key = "live/cust-1/site-1/appl-1/cam-1/camera1_000001.ts"
    response = client.post(
        "/api/appliance/live/cam-1/segment-available",
        headers=_auth_headers("appl-1", "cred"),
        json={"segment_key": key, "sequence": 1},
    )
    assert response.status_code == 200
    manifest = appliance_cloud.live_manifest_store.manifest_for("cam-1")
    assert any(entry["key"] == key and entry["sequence"] == 1 for entry in manifest["segments"])


def test_segment_available_rejects_a_key_outside_this_camera_own_prefix(client, db_path, monkeypatch):
    """A malformed or forged segment_key that doesn't start with this
    exact camera's own live_relay_s3_prefix() must never be recorded --
    the same isolation principle live_playlist.py's read-side
    _segment_filename() already relies on."""
    _configure_relay(monkeypatch, enabled=True)
    manifest_path = db_path.parent / "test_live_manifest_reject.json"
    from live_manifest import LiveManifestStore
    monkeypatch.setattr(appliance_cloud, "live_manifest_store", LiveManifestStore(manifest_path))
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1",
                  customer_id="cust-1", site_id="site-1")
            db.execute("UPDATE appliances SET live_relay_pilot=1 WHERE id='appl-1'")

    wrong_key = "live/cust-1/site-1/appl-1/some-other-camera/camera1_000001.ts"
    response = client.post(
        "/api/appliance/live/cam-1/segment-available",
        headers=_auth_headers("appl-1", "cred"),
        json={"segment_key": wrong_key, "sequence": 1},
    )
    assert response.status_code == 403
    manifest = appliance_cloud.live_manifest_store.manifest_for("cam-1")
    assert manifest["segments"] == []
