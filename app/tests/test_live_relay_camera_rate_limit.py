"""Live Relay per-camera rate limit (2026-09-13): regression coverage for
the fix to a confirmed-live defect where /api/appliance/live/{camera_id}/
segment-available's own legitimate traffic (~30 requests/minute per
actively-relayed camera -- one per 2-second HLS segment) shared the
appliance-wide request_limiter(120,60) with heartbeat/commands/
configuration/every other appliance endpoint, and already exceeded that
120/60 budget on its own math at just 5 concurrent cameras (measured live:
~154 req/min at 5 cameras, ~244 req/min at the 8-camera Starter tier).

Deliberately NOT a second fixed appliance-wide ceiling sized for one
particular fleet size (that just relocates the "wrong number for some
tier" problem) -- live_relay_camera_limiter is keyed by camera_id instead
of appliance_id, so the effective allowed aggregate for one appliance
scales automatically with however many cameras it actually has actively
relaying (N cameras x 60/min), always ~2x the ~30/min legitimate cadence
at any fleet size, while independently bounding a single malfunctioning
camera's own runaway traffic and never touching the original
request_limiter's protection for every other appliance endpoint.

Two test classes:
  - HTTP-level tests (real FastAPI routes, matching test_live_relay_
    session_endpoint.py's own established seed/client pattern) for
    isolation between cameras, isolation from normal appliance traffic,
    and the original request_limiter's behavior being completely
    unchanged.
  - Pure RateLimiter-class tests (no HTTP layer -- this is a property of
    the class + per-camera-keying design itself, not endpoint-specific)
    for the 5/8/16/32/64-camera modeled-load math and single-camera
    runaway protection, using RateLimiter.allow()'s own `now` parameter
    to simulate compressed wall-clock time deterministically and fast,
    with no real sleep.
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from appliance_protocol import RateLimiter
from database_backend import override_target

with override_target(sqlite_path="/tmp/test_live_relay_camera_rate_limit.db"):
    import appliance_cloud
    from partner_db import connection, password_hash


def _seed(db, *, appliance_id, cloud_id, credential, camera_id, camera_number=1,
          device_key="urn:uuid:fake", customer_id="cust-1", site_id="site-1"):
    now = "2026-09-13T00:00:00"
    db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('partner-1','Test Partner','approved','real',?)", (now,))
    db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)",
               (customer_id, "partner-1", "Test Customer", f"{customer_id}@example.test", "active", "real", now))
    db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Test Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,live_relay_pilot,created_at) VALUES(?,?,?,?,?,?)",
               (appliance_id, customer_id, site_id, cloud_id, 1, now))
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)",
               (f"cred-{appliance_id}", appliance_id, password_hash(credential), now))
    db.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,device_key,status,name,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (camera_id, customer_id, site_id, appliance_id, camera_number, device_key, "configured", "Camera", now),
    )


def _add_camera(db, *, appliance_id, camera_id, camera_number, customer_id="cust-1", site_id="site-1"):
    db.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,device_key,status,name,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (camera_id, customer_id, site_id, appliance_id, camera_number, f"urn:uuid:fake-{camera_number}", "configured", "Camera", "2026-09-13T00:00:00"),
    )


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
    return tmp_path / "test_live_relay_camera_rate_limit.db"


@pytest.fixture(autouse=True)
def _fresh_limiters(monkeypatch):
    """Every test gets a clean slate on both shared, process-wide limiter
    singletons -- reset IN PLACE (clearing each object's own `.events`
    dict), not replaced with a new RateLimiter instance. This matters
    specifically for request_limiter: authenticate_appliance()'s own
    `limiter=request_limiter` default parameter is bound to the object
    that existed at function-DEFINITION time (ordinary Python late-vs-
    early-binding), so monkeypatch.setattr(appliance_cloud,
    'request_limiter', RateLimiter(...)) would rebind the module-level
    NAME but never reach that already-frozen default -- confirmed by this
    exact failure mode during development of this test file. Resetting
    the existing object's internal state avoids the whole issue, and
    live_relay_camera_limiter (looked up fresh by name inside each
    route's own body, not passed as a default) is reset the same way for
    consistency."""
    appliance_cloud.request_limiter.events.clear()
    appliance_cloud.live_relay_camera_limiter.events.clear()


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


class _FakeSTS:
    def __init__(self, *a, **k):
        pass

    def assume_role(self, RoleArn, RoleSessionName, Policy, DurationSeconds):
        from datetime import datetime, timezone
        return {
            "Credentials": {
                "AccessKeyId": "FAKEACCESSKEYID",
                "SecretAccessKey": "fake-secret-access-key-never-real",
                "SessionToken": "fake-session-token-never-real",
                "Expiration": datetime.now(timezone.utc),
            }
        }


def _configure_relay(monkeypatch):
    monkeypatch.setattr(appliance_cloud, "LIVE_RELAY_ENABLED", True)
    monkeypatch.setattr(appliance_cloud, "LIVE_UPLOAD_ROLE_ARN", "arn:aws:iam::123456789012:role/anyaicam-live-relay-upload")
    monkeypatch.setattr(appliance_cloud, "LIVE_RELAY_S3_BUCKET", "anyaicam2026")
    monkeypatch.setattr(appliance_cloud, "LIVE_RELAY_AWS_REGION", "us-east-1")
    monkeypatch.setattr(appliance_cloud, "boto3", type("_B", (), {"client": staticmethod(lambda service, region_name=None: _FakeSTS())}))


# ==================================================================== HTTP-level tests


def test_one_cameras_exhausted_budget_does_not_affect_a_different_camera(client, db_path, monkeypatch):
    _configure_relay(monkeypatch)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1")
            _add_camera(db, appliance_id="appl-1", camera_id="cam-2", camera_number=2)

    # Exhaust cam-1's own 60/60 budget -- a fresh nonce per call, since the
    # replay-nonce check (a separate, unrelated protection) would otherwise
    # reject every call after the first as a duplicate.
    for _ in range(60):
        response = client.post("/api/appliance/live/cam-1/session", headers=_auth_headers("appl-1", "cred"))
        assert response.status_code == 200
    exhausted = client.post("/api/appliance/live/cam-1/session", headers=_auth_headers("appl-1", "cred"))
    assert exhausted.status_code == 429

    # cam-2's own, independent budget is completely untouched.
    still_ok = client.post("/api/appliance/live/cam-2/session", headers=_auth_headers("appl-1", "cred"))
    assert still_ok.status_code == 200


def test_exhausting_live_relay_budget_does_not_affect_normal_appliance_traffic(client, db_path, monkeypatch):
    _configure_relay(monkeypatch)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1")

    for _ in range(60):
        client.post("/api/appliance/live/cam-1/session", headers=_auth_headers("appl-1", "cred"))
    exhausted = client.post("/api/appliance/live/cam-1/session", headers=_auth_headers("appl-1", "cred"))
    assert exhausted.status_code == 429

    # A completely unrelated appliance endpoint (heartbeat/status-style,
    # governed by the ORIGINAL request_limiter) is still fully available --
    # proves the two traffic classes are genuinely isolated, not just
    # sharing a bigger combined pool.
    unrelated = client.get("/api/appliance/recordings/status", headers=_auth_headers("appl-1", "cred"))
    assert unrelated.status_code == 200


def test_original_request_limiter_threshold_and_enforcement_is_completely_unchanged(client, db_path):
    """Regression lock for the explicit non-negotiable requirement: no
    security control on any unrelated appliance endpoint was weakened.
    Exhausts the ORIGINAL 120/60 budget via a plain, unrelated endpoint
    that has never called live_relay_camera_limiter at all."""
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1")

    for i in range(120):
        response = client.get("/api/appliance/recordings/status", headers=_auth_headers("appl-1", "cred"))
        assert response.status_code == 200, f"request {i} unexpectedly failed"
    exhausted = client.get("/api/appliance/recordings/status", headers=_auth_headers("appl-1", "cred"))
    assert exhausted.status_code == 429
    assert exhausted.json()["detail"] == "Appliance request rate exceeded."


def test_live_relay_endpoints_still_require_valid_camera_ownership(client, db_path, monkeypatch):
    """The new per-camera limiter is checked AFTER _authorized_camera() --
    confirms authentication and camera-ownership authorization are both
    still fully intact, not bypassed by the limiter refactor."""
    _configure_relay(monkeypatch)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-1", cloud_id="AIC-TEST", credential="cred", camera_id="cam-1")
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,live_relay_pilot,created_at) VALUES('appl-other','cust-1','site-1','AIC-OTHER',1,'2026-09-13T00:00:00')")
            db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES('cred-other','appl-other',?,?)",
                       (password_hash("other-credential"), "2026-09-13T00:00:00"))

    # appl-other has never touched cam-1; ownership check must still reject it.
    response = client.post("/api/appliance/live/cam-1/session", headers=_auth_headers("appl-other", "other-credential"))
    assert response.status_code == 403

    # Unauthenticated (no headers at all) still 401s, same as before.
    response = client.post("/api/appliance/live/cam-1/session")
    assert response.status_code == 401


# ==================================================================== RateLimiter-class capacity math


@pytest.mark.parametrize("camera_count", [5, 8, 16, 32, 64])
def test_legitimate_load_passes_cleanly_at_every_modeled_fleet_size(camera_count):
    """Simulates each camera's own real cadence (one call every 2 seconds,
    for a full simulated 60-second window) against a fresh RateLimiter(60,60)
    -- proves the SAME per-camera constant gives clean headroom at every
    tier from 5 to 64 cameras, with no per-tier re-tuning."""
    limiter = RateLimiter(60, 60)
    base = 1_000_000.0
    rejected = 0
    for camera_index in range(camera_count):
        camera_id = f"cam-{camera_index}"
        # One call every 2 seconds for 60 simulated seconds = 30 calls.
        for tick in range(30):
            if not limiter.allow(camera_id, now=base + tick * 2.0):
                rejected += 1
    assert rejected == 0, f"{rejected} legitimate calls rejected at {camera_count} cameras"


def test_aggregate_allowed_capacity_scales_linearly_with_camera_count():
    """Direct proof of the core design property: the allowed aggregate for
    an appliance is N x 60/min, not a fixed ceiling -- explicitly what was
    rejected about the prior single-number proposal."""
    for camera_count in (5, 8, 16, 32, 64):
        limiter = RateLimiter(60, 60)
        allowed = 0
        for camera_index in range(camera_count):
            camera_id = f"cam-{camera_index}"
            for _ in range(60):
                if limiter.allow(camera_id, now=1_000_000.0):
                    allowed += 1
        assert allowed == camera_count * 60


def test_a_single_runaway_camera_is_capped_regardless_of_fleet_size():
    """A malfunctioning camera stuck in a tight retry loop is still
    bounded to its own 60/60 budget -- this protection needs no knowledge
    of how many cameras the appliance legitimately has."""
    limiter = RateLimiter(60, 60)
    now = 1_000_000.0
    allowed = sum(1 for _ in range(500) if limiter.allow("runaway-camera", now=now))
    assert allowed == 60  # capped, not 500


def test_a_runaway_camera_never_consumes_a_different_cameras_budget():
    limiter = RateLimiter(60, 60)
    now = 1_000_000.0
    for _ in range(500):
        limiter.allow("runaway-camera", now=now)
    # A different, well-behaved camera on the same (simulated) appliance
    # still gets its own full, untouched budget.
    well_behaved_allowed = sum(1 for _ in range(60) if limiter.allow("well-behaved-camera", now=now))
    assert well_behaved_allowed == 60
