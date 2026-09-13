"""Event-media credential path (2026-09-13): POST /api/appliance/
recordings/{camera_id}/credentials now issues short-lived S3
PutObject credentials when EITHER RECORDING_UPLOAD_ENABLED (bulk/
continuous recording upload) OR EVENT_MEDIA_UPLOAD_ENABLED
(motion-event thumbnail/clip upload, event_media_uploader.py's own
edge-side gate) is true -- previously only the former, which meant
event-media could never get a working credential without also
enabling the much bigger, continuous bulk-recording pipeline.

Real, live root cause this fixes: event_media_uploader.py's
_ensure_session() call was returning "session_unavailable" for every
real Camera 1/2/3 motion event on Ryzen, confirmed via
`OSError`-free logs showing a 404 from this exact route, because
RECORDING_UPLOAD_ENABLED was (deliberately) still false while
EVENT_MEDIA_UPLOAD_ENABLED was already true.

Mirrors test_live_relay_session_endpoint.py's own established pattern
exactly: a bare FastAPI() app with register_appliance_cloud_routes(),
a fresh sqlite DB per test, and boto3.client('sts').assume_role
monkeypatched to a fake object returning a synthetic, obviously-fake
credential -- no real AWS call is ever made.
"""

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_event_media_credential_path.db"):
    import appliance_cloud
    from partner_db import connection, password_hash


def _seed(db, *, appliance_id, cloud_id, credential, camera_id, camera_number=1,
          device_key="urn:uuid:fake", customer_id="cust-1", site_id="site-1", other_appliance=False):
    now = "2026-09-13T00:00:00"
    db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('partner-1','Test Partner','approved','real',?)", (now,))
    db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)",
               (customer_id, "partner-1", "Test Customer", f"{customer_id}@example.test", "active", "real", now))
    db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Test Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
               (appliance_id, customer_id, site_id, cloud_id, now))
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)",
               (f"cred-{appliance_id}", appliance_id, password_hash(credential), now))
    db.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,device_key,status,name,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (camera_id, customer_id, site_id, appliance_id, camera_number, device_key, "configured", "Camera 1", now),
    )
    if other_appliance:
        db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-other',?,?,?,?)",
                   (customer_id, site_id, "AIC-OTHER", now))
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
    return tmp_path / "test_event_media_credential_path.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


def _configure(monkeypatch, *, recording_enabled=False, event_media_enabled=False, pilot_cameras=frozenset(),
               role_arn="arn:aws:iam::880690594006:role/anyaicam-recording-upload-role",
               bucket="anyaicam-recordings-prod-20260820", region="us-east-1"):
    monkeypatch.setattr(appliance_cloud, "RECORDING_UPLOAD_ENABLED", recording_enabled)
    monkeypatch.setattr(appliance_cloud, "EVENT_MEDIA_UPLOAD_ENABLED", event_media_enabled)
    monkeypatch.setattr(appliance_cloud, "RECORDING_UPLOAD_PILOT_CAMERAS", frozenset(pilot_cameras))
    monkeypatch.setattr(appliance_cloud, "RECORDING_UPLOAD_ROLE_ARN", role_arn)
    monkeypatch.setattr(appliance_cloud, "RECORDING_S3_BUCKET", bucket)
    monkeypatch.setattr(appliance_cloud, "RECORDING_AWS_REGION", region)


class _FakeSTS:
    """Records exactly what was requested so tests can assert on the
    real policy/role passed in, not just that *some* credential came
    back."""

    last_policy = None
    last_role_arn = None

    def __init__(self, *a, **k):
        pass

    def assume_role(self, RoleArn, RoleSessionName, Policy, DurationSeconds):
        from datetime import datetime, timezone
        _FakeSTS.last_policy = json.loads(Policy)
        _FakeSTS.last_role_arn = RoleArn
        return {
            "Credentials": {
                "AccessKeyId": "FAKEACCESSKEYID",
                "SecretAccessKey": "fake-secret-access-key-never-real",
                "SessionToken": "fake-session-token-never-real",
                "Expiration": datetime.now(timezone.utc),
            }
        }


def _install_fake_boto3(monkeypatch):
    monkeypatch.setattr(appliance_cloud, "boto3", type("_B", (), {"client": staticmethod(lambda service, region_name=None: _FakeSTS())}))


# --------------------------------------------------- the fix itself: OR-gated


def test_event_media_alone_can_obtain_credentials_while_bulk_recording_stays_disabled(client, db_path, monkeypatch):
    """The real, live scenario this fixes: event-media enabled, bulk
    recording deliberately left off."""
    _configure(monkeypatch, recording_enabled=False, event_media_enabled=True)
    _install_fake_boto3(monkeypatch)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1",
                  customer_id="cust-1", site_id="site-1")

    response = client.post("/api/appliance/recordings/cam-1/credentials", headers=_auth_headers("appl-1", "cred"))

    assert response.status_code == 200
    body = response.json()
    for key in ("access_key_id", "secret_access_key", "session_token", "expiration"):
        assert key in body["credentials"]
    # Confirm bulk recording's own flag was never consulted as true --
    # this success came purely from EVENT_MEDIA_UPLOAD_ENABLED.
    assert appliance_cloud.RECORDING_UPLOAD_ENABLED is False


def test_bulk_recording_upload_endpoints_remain_disabled_when_only_event_media_is_on(client, db_path, monkeypatch):
    """recording_available() (R2's catalog route) and the /status route
    must stay gated purely by RECORDING_UPLOAD_ENABLED -- enabling
    event-media must never also light up the bulk-recording pipeline's
    other endpoints."""
    _configure(monkeypatch, recording_enabled=False, event_media_enabled=True)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1")

    status = client.get("/api/appliance/recordings/status", headers=_auth_headers("appl-1", "cred"))
    assert status.json() == {"enabled": False}

    available = client.post(
        "/api/appliance/recordings/cam-1/available",
        json={"s3_key": "recordings/cust-1/site-1/appl-1/cam-1/2026/09/13/x.mp4", "started_at": "2026-09-13T00:00:00", "ended_at": "2026-09-13T00:00:10"},
        headers=_auth_headers("appl-1", "cred"),
    )
    assert available.status_code == 404


def test_credentials_route_still_404s_when_neither_flag_is_enabled(client, db_path, monkeypatch):
    _configure(monkeypatch, recording_enabled=False, event_media_enabled=False)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1")
    response = client.post("/api/appliance/recordings/cam-1/credentials", headers=_auth_headers("appl-1", "cred"))
    assert response.status_code == 404


def test_credentials_route_503_when_event_media_enabled_but_aws_not_configured(client, db_path, monkeypatch):
    _configure(monkeypatch, recording_enabled=False, event_media_enabled=True, role_arn="", bucket="", region="")
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1")
    response = client.post("/api/appliance/recordings/cam-1/credentials", headers=_auth_headers("appl-1", "cred"))
    assert response.status_code == 503


# ---------------------------------------------- existing bulk-recording behavior preserved


def test_bulk_recording_still_gets_the_original_broad_policy_unchanged(client, db_path, monkeypatch):
    """Regression lock: when RECORDING_UPLOAD_ENABLED is the flag that
    authorized the request, the credential's scope must be byte-for-
    byte the same as before this change -- the whole camera prefix, not
    the new narrower events-only one."""
    _configure(monkeypatch, recording_enabled=True, event_media_enabled=False)
    _install_fake_boto3(monkeypatch)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1",
                  customer_id="cust-1", site_id="site-1")

    response = client.post("/api/appliance/recordings/cam-1/credentials", headers=_auth_headers("appl-1", "cred"))

    assert response.status_code == 200
    assert _FakeSTS.last_policy["Statement"][0]["Resource"] == "arn:aws:s3:::anyaicam-recordings-prod-20260820/recordings/cust-1/site-1/appl-1/cam-1/*"


def test_both_flags_enabled_prefers_the_broad_bulk_policy(client, db_path, monkeypatch):
    """A request authorized by both simply gets the broader, already-
    established bulk policy -- there is nothing extra for the narrower
    one to restrict in that case."""
    _configure(monkeypatch, recording_enabled=True, event_media_enabled=True)
    _install_fake_boto3(monkeypatch)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1",
                  customer_id="cust-1", site_id="site-1")

    client.post("/api/appliance/recordings/cam-1/credentials", headers=_auth_headers("appl-1", "cred"))
    assert "/events/" not in _FakeSTS.last_policy["Statement"][0]["Resource"]


# ------------------------------------------------------- the narrower scope itself


def test_event_media_only_credential_is_scoped_to_the_events_subprefix(client, db_path, monkeypatch):
    _configure(monkeypatch, recording_enabled=False, event_media_enabled=True)
    _install_fake_boto3(monkeypatch)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1",
                  customer_id="cust-1", site_id="site-1")

    client.post("/api/appliance/recordings/cam-1/credentials", headers=_auth_headers("appl-1", "cred"))

    resource = _FakeSTS.last_policy["Statement"][0]["Resource"]
    assert resource == "arn:aws:s3:::anyaicam-recordings-prod-20260820/recordings/cust-1/site-1/appl-1/cam-1/*/events/*"
    assert _FakeSTS.last_policy["Statement"][0]["Action"] == "s3:PutObject"
    assert {statement["Action"] for statement in _FakeSTS.last_policy["Statement"]} == {"s3:PutObject"}


def test_event_media_credential_cannot_write_to_a_different_camera(client, db_path, monkeypatch):
    _configure(monkeypatch, recording_enabled=False, event_media_enabled=True)
    _install_fake_boto3(monkeypatch)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1")
            db.execute(
                "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,device_key,status,name,created_at) "
                "VALUES('cam-2','cust-1','site-1','appl-1',2,'urn:uuid:fake2','configured','Camera 2','2026-09-13T00:00:00')"
            )

    resource_1 = _issue_and_get_resource(client, "cam-1", "appl-1", "cred")
    resource_2 = _issue_and_get_resource(client, "cam-2", "appl-1", "cred")
    assert resource_1 != resource_2
    assert not resource_1.startswith(resource_2.replace("/events/*", ""))
    assert not resource_2.startswith(resource_1.replace("/events/*", ""))


def test_event_media_credential_cannot_write_to_a_different_customer_site_or_appliance(client, db_path, monkeypatch):
    _configure(monkeypatch, recording_enabled=False, event_media_enabled=True)
    _install_fake_boto3(monkeypatch)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred-1", camera_id="cam-1",
                  customer_id="cust-1", site_id="site-1")
            _seed(db, appliance_id="appl-2", cloud_id="AIC-TEST-2", credential="cred-2", camera_id="cam-2",
                  customer_id="cust-2", site_id="site-2")

    resource_1 = _issue_and_get_resource(client, "cam-1", "appl-1", "cred-1")
    resource_2 = _issue_and_get_resource(client, "cam-2", "appl-2", "cred-2")
    assert resource_1 != resource_2
    assert "cust-1" in resource_1 and "cust-2" not in resource_1
    assert "cust-2" in resource_2 and "cust-1" not in resource_2


def test_event_media_credential_cannot_be_issued_for_a_camera_on_another_appliance(client, db_path, monkeypatch):
    """The route is appliance-authenticated: an appliance can never mint
    an event-media credential for a camera it does not itself own, even
    if it somehow knows another appliance's camera_id."""
    _configure(monkeypatch, recording_enabled=False, event_media_enabled=True)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1", other_appliance=True)

    response = client.post("/api/appliance/recordings/cam-1/credentials", headers=_auth_headers("appl-other", "other-credential"))
    assert response.status_code == 403


def _issue_and_get_resource(client, camera_id, appliance_id, credential) -> str:
    response = client.post(f"/api/appliance/recordings/{camera_id}/credentials", headers=_auth_headers(appliance_id, credential))
    assert response.status_code == 200
    return _FakeSTS.last_policy["Statement"][0]["Resource"]


# ------------------------------------------- pilot-camera allowlist (Phase 2)


def test_pilot_camera_allowlist_unset_restricts_nothing():
    assert appliance_cloud.RECORDING_UPLOAD_PILOT_CAMERAS == frozenset()


def test_pilot_listed_camera_gets_the_broad_bulk_policy_with_the_global_flag_off(client, db_path, monkeypatch):
    """The real scenario this mechanism exists for: RECORDING_UPLOAD_ENABLED
    stays globally false, but one explicitly-approved camera still gets
    the real bulk-recording session scope."""
    _configure(monkeypatch, recording_enabled=False, event_media_enabled=True, pilot_cameras={"cam-1"})
    _install_fake_boto3(monkeypatch)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1",
                  customer_id="cust-1", site_id="site-1")

    resource = _issue_and_get_resource(client, "cam-1", "appl-1", "cred")

    assert resource == "arn:aws:s3:::anyaicam-recordings-prod-20260820/recordings/cust-1/site-1/appl-1/cam-1/*"
    assert "/events/" not in resource
    assert appliance_cloud.RECORDING_UPLOAD_ENABLED is False


def test_a_non_pilot_camera_still_gets_the_narrow_event_media_policy(client, db_path, monkeypatch):
    """Regression lock: listing one camera must not widen scope for any
    other camera, even on the same appliance."""
    _configure(monkeypatch, recording_enabled=False, event_media_enabled=True, pilot_cameras={"cam-1"})
    _install_fake_boto3(monkeypatch)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-2",
                  customer_id="cust-1", site_id="site-1")

    resource = _issue_and_get_resource(client, "cam-2", "appl-1", "cred")

    assert resource == "arn:aws:s3:::anyaicam-recordings-prod-20260820/recordings/cust-1/site-1/appl-1/cam-2/*/events/*"


def test_pilot_listed_camera_can_catalog_a_recording_with_the_global_flag_off(client, db_path, monkeypatch):
    """recording_available() (R2) must also accept a pilot-listed
    camera_id even while RECORDING_UPLOAD_ENABLED stays globally false --
    without this, the credential above would be issued but every
    catalog call would still 404."""
    _configure(monkeypatch, recording_enabled=False, event_media_enabled=False, pilot_cameras={"cam-1"})
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1",
                  customer_id="cust-1", site_id="site-1")

    response = client.post(
        "/api/appliance/recordings/cam-1/available",
        json={"s3_key": "recordings/cust-1/site-1/appl-1/cam-1/2026/09/13/x.mp4", "started_at": "2026-09-13T00:00:00", "ended_at": "2026-09-13T00:00:10"},
        headers=_auth_headers("appl-1", "cred"),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


def test_a_non_pilot_camera_still_404s_on_available_with_the_global_flag_off(client, db_path, monkeypatch):
    _configure(monkeypatch, recording_enabled=False, event_media_enabled=False, pilot_cameras={"cam-1"})
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-2")

    response = client.post(
        "/api/appliance/recordings/cam-2/available",
        json={"s3_key": "recordings/cust-1/site-1/appl-1/cam-2/2026/09/13/x.mp4", "started_at": "2026-09-13T00:00:00", "ended_at": "2026-09-13T00:00:10"},
        headers=_auth_headers("appl-1", "cred"),
    )
    assert response.status_code == 404


def test_status_route_still_reports_only_the_global_flag_unaffected_by_the_pilot_list(client, db_path, monkeypatch):
    """The pilot list changes credential scope and the catalog gate only
    -- it must never make /status claim bulk recording is broadly on."""
    _configure(monkeypatch, recording_enabled=False, event_media_enabled=False, pilot_cameras={"cam-1"})
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1")

    status = client.get("/api/appliance/recordings/status", headers=_auth_headers("appl-1", "cred"))
    assert status.json() == {"enabled": False}


def test_pilot_camera_on_another_appliance_still_rejects_an_unauthorized_caller(client, db_path, monkeypatch):
    """Ownership is checked independently of pilot-list membership --
    being on the allowlist never substitutes for actually owning the
    camera."""
    _configure(monkeypatch, recording_enabled=False, event_media_enabled=True, pilot_cameras={"cam-1"})
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1", other_appliance=True)

    response = client.post("/api/appliance/recordings/cam-1/credentials", headers=_auth_headers("appl-other", "other-credential"))
    assert response.status_code == 403

    available = client.post(
        "/api/appliance/recordings/cam-1/available",
        json={"s3_key": "recordings/cust-1/site-1/appl-1/cam-1/2026/09/13/x.mp4", "started_at": "2026-09-13T00:00:00", "ended_at": "2026-09-13T00:00:10"},
        headers=_auth_headers("appl-other", "other-credential"),
    )
    assert available.status_code == 403


def test_bulk_recording_globally_enabled_behavior_is_unchanged_regardless_of_pilot_list(client, db_path, monkeypatch):
    """Regression lock: a camera that's both pilot-listed AND covered by
    the global flag behaves exactly as it always did under the global
    flag -- the pilot list adds a second door, it doesn't change what's
    behind the existing one."""
    _configure(monkeypatch, recording_enabled=True, event_media_enabled=False, pilot_cameras={"cam-1"})
    _install_fake_boto3(monkeypatch)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1",
                  customer_id="cust-1", site_id="site-1")

    resource = _issue_and_get_resource(client, "cam-1", "appl-1", "cred")
    assert resource == "arn:aws:s3:::anyaicam-recordings-prod-20260820/recordings/cust-1/site-1/appl-1/cam-1/*"
