"""Cloud-side authorization for a correlated Smart Motion event sharing
its base Motion event's already-uploaded clip/thumbnail (2026-09-14,
Phase A -- revised design after independent security review).

The parent/child relationship is established and frozen at analytics
ingestion time (analytics_event_available()), resolved from a
submitted LOCAL id under (this camera, this authenticated appliance,
parent event_type='motion') -- never trusted again later. The actual
media-sharing request (analytics_event_media_shared()) carries ONLY
the parent's own local id in its body -- no S3 key, bucket, timing,
duration, or size field exists in its schema at all -- and the cloud
derives every approved value from the verified parent's own already-
registered detection_event_media row.

Uses the same real FastAPI TestClient + real SQLite pattern already
established in test_event_media_cloud_flow.py -- never a mocked route.
"""

import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_shared_smart_motion_media_authorization_import.db"):
    import appliance_cloud
    from partner_db import connection, initialize_database, password_hash


def _headers(appliance_id="appl-1", credential="credential"):
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


def _seed(db):
    """Primary tenant (appliance-1/customer-1), two cameras (cam-1,
    cam-2) for the same-appliance-different-camera case -- plus a fully
    separate second tenant (appliance-2/customer-2/cam-3) for the
    cross-appliance/cross-customer case."""
    now = "2026-08-21T00:00:00"
    db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-1", "Partner", "approved", "real", now))
    db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", ("cust-1", "partner-1", "Customer", "customer@example.test", "active", "real", now))
    db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", ("site-1", "cust-1", "Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", ("appl-1", "cust-1", "site-1", "AIC-TEST-1", now))
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", ("cred-1", "appl-1", password_hash("credential"), now))
    db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,cloud_recording_mode,created_at) VALUES(?,?,?,?,?,?,?)", ("cam-1", "cust-1", "site-1", "appl-1", "Driveway", "motion", now))
    db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,cloud_recording_mode,created_at) VALUES(?,?,?,?,?,?,?)", ("cam-2", "cust-1", "site-1", "appl-1", "Backyard", "motion", now))
    db.execute("INSERT INTO plans(id,customer_id,recording_mode,retention_days,created_at) VALUES(?,?,?,?,?)", ("plan-1", "cust-1", "motion", 7, now))

    db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", ("cust-2", "partner-1", "Other Customer", "other@example.test", "active", "real", now))
    db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", ("site-2", "cust-2", "Site 2", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", ("appl-2", "cust-2", "site-2", "AIC-TEST-2", now))
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", ("cred-2", "appl-2", password_hash("credential-2"), now))
    db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,cloud_recording_mode,created_at) VALUES(?,?,?,?,?,?,?)", ("cam-3", "cust-2", "site-2", "appl-2", "Front Door", "motion", now))
    db.execute("INSERT INTO plans(id,customer_id,recording_mode,retention_days,created_at) VALUES(?,?,?,?,?)", ("plan-2", "cust-2", "motion", 7, now))


@pytest.fixture(autouse=True)
def _analytics_sync_enabled(monkeypatch):
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True)


@pytest.fixture()
def client(tmp_path):
    database = tmp_path / "shared-media.db"
    with override_target(sqlite_path=str(database)):
        initialize_database()
        with connection() as db:
            _seed(db)
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *args, **kwargs: "")
        with TestClient(app) as test_client:
            yield test_client, database


def _event_payload(local_event_id, event_type="motion", parent_local_event_id=None, timestamp="2026-08-21T00:00:03"):
    payload = {
        "local_event_id": local_event_id,
        "event_type": event_type,
        "confidence": 0.9,
        "object_count": 1,
        "detections": [],
        "event_timestamp": timestamp,
    }
    if parent_local_event_id is not None:
        payload["parent_local_event_id"] = parent_local_event_id
    return payload


def _media_payload(local_event_id, camera_id="cam-1", prefix="cust-1/site-1/appl-1", **overrides):
    value = {
        "s3_key": f"recordings/{prefix}/{camera_id}/2026/08/21/events/motion_{local_event_id}.mp4",
        "thumbnail_s3_key": f"recordings/{prefix}/{camera_id}/2026/08/21/events/motion_{local_event_id}.jpg",
        "started_at": "2026-08-21T00:00:00",
        "ended_at": "2026-08-21T00:00:10",
        "duration_seconds": 10.0,
        "size_bytes": 12345,
    }
    value.update(overrides)
    return value


def _sync_event(test_client, camera_id, local_event_id, event_type="motion", parent_local_event_id=None, appliance_id="appl-1", credential="credential", timestamp="2026-08-21T00:00:03"):
    return test_client.post(
        f"/api/appliance/analytics/{camera_id}/events",
        headers=_headers(appliance_id, credential),
        json=_event_payload(local_event_id, event_type=event_type, parent_local_event_id=parent_local_event_id, timestamp=timestamp),
    )


def _register_base_motion_with_media(test_client, camera_id, local_event_id, appliance_id="appl-1", credential="credential", prefix="cust-1/site-1/appl-1"):
    """Real two-step flow: sync the base Motion analytics event, then
    register its own (self-derived-key) media -- exactly the existing,
    unchanged path every base Motion event already uses. Each request
    gets its OWN freshly-generated header set (own nonce/timestamp) --
    reusing one headers dict across two real requests trips the real
    replay-protection guard."""
    assert _sync_event(test_client, camera_id, local_event_id, appliance_id=appliance_id, credential=credential).status_code == 200
    response = test_client.post(
        f"/api/appliance/analytics/{camera_id}/events/{local_event_id}/media",
        headers=_headers(appliance_id, credential),
        json=_media_payload(local_event_id, camera_id=camera_id, prefix=prefix),
    )
    assert response.status_code == 200 and response.json()["status"] == "accepted"
    return _media_payload(local_event_id, camera_id=camera_id, prefix=prefix)


def _share(test_client, camera_id, local_event_id, parent_local_event_id, appliance_id="appl-1", credential="credential"):
    return test_client.post(
        f"/api/appliance/analytics/{camera_id}/events/{local_event_id}/media/shared",
        headers=_headers(appliance_id, credential),
        json={"parent_local_event_id": parent_local_event_id},
    )


# --------------------------------------------------------- ingestion-time correlation


def test_parent_resolves_and_freezes_at_ingestion(client, monkeypatch):
    test_client, database = client
    assert _sync_event(test_client, "cam-1", "motion-1").status_code == 200
    assert _sync_event(test_client, "cam-1", "smart-1", event_type="smart_motion", parent_local_event_id="motion-1").status_code == 200

    with override_target(sqlite_path=str(database)):
        with connection() as db:
            row = db.execute("SELECT parent_detection_event_id FROM detection_events WHERE local_event_id='smart-1'").fetchone()
            parent = db.execute("SELECT id FROM detection_events WHERE local_event_id='motion-1'").fetchone()
    assert row["parent_detection_event_id"] == parent["id"]


def test_child_before_parent_stays_unresolved_then_resolves_on_a_later_resync(client):
    """Self-healing convergence: the child can sync before its parent
    exists at all -- it stays unresolved, never errors, and a later
    resync (once the parent has since arrived) completes the
    relationship for the first time."""
    test_client, database = client
    assert _sync_event(test_client, "cam-1", "smart-2", event_type="smart_motion", parent_local_event_id="motion-2").status_code == 200

    with override_target(sqlite_path=str(database)):
        with connection() as db:
            row = db.execute("SELECT parent_detection_event_id FROM detection_events WHERE local_event_id='smart-2'").fetchone()
    assert row["parent_detection_event_id"] is None

    assert _sync_event(test_client, "cam-1", "motion-2").status_code == 200
    # Later resync of the SAME child, now that the parent exists.
    assert _sync_event(test_client, "cam-1", "smart-2", event_type="smart_motion", parent_local_event_id="motion-2").status_code == 200

    with override_target(sqlite_path=str(database)):
        with connection() as db:
            row = db.execute("SELECT parent_detection_event_id FROM detection_events WHERE local_event_id='smart-2'").fetchone()
            parent = db.execute("SELECT id FROM detection_events WHERE local_event_id='motion-2'").fetchone()
    assert row["parent_detection_event_id"] == parent["id"]


def test_replay_with_a_different_event_type_is_a_conflict(client):
    test_client, _ = client
    assert _sync_event(test_client, "cam-1", "evt-x", event_type="motion").status_code == 200
    response = _sync_event(test_client, "cam-1", "evt-x", event_type="person")
    assert response.status_code == 409


def test_replay_with_a_different_timestamp_is_a_conflict(client):
    test_client, _ = client
    assert _sync_event(test_client, "cam-1", "evt-y", timestamp="2026-08-21T00:00:03").status_code == 200
    response = _sync_event(test_client, "cam-1", "evt-y", timestamp="2026-08-21T00:05:00")
    assert response.status_code == 409


def test_replay_asserting_a_different_already_resolved_parent_is_a_conflict(client):
    test_client, _ = client
    assert _sync_event(test_client, "cam-1", "motion-a").status_code == 200
    assert _sync_event(test_client, "cam-1", "motion-b").status_code == 200
    assert _sync_event(test_client, "cam-1", "smart-3", event_type="smart_motion", parent_local_event_id="motion-a").status_code == 200

    response = _sync_event(test_client, "cam-1", "smart-3", event_type="smart_motion", parent_local_event_id="motion-b")
    assert response.status_code == 409


def test_identical_replay_of_an_already_resolved_child_is_a_harmless_duplicate(client):
    test_client, _ = client
    assert _sync_event(test_client, "cam-1", "motion-c").status_code == 200
    assert _sync_event(test_client, "cam-1", "smart-4", event_type="smart_motion", parent_local_event_id="motion-c").status_code == 200

    response = _sync_event(test_client, "cam-1", "smart-4", event_type="smart_motion", parent_local_event_id="motion-c")
    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"


# --------------------------------------------------------- the valid shared-media case


def test_valid_correlated_pair_shares_media_with_independent_ownership(client):
    test_client, database = client
    base_media = _register_base_motion_with_media(test_client, "cam-1", "motion-1")
    assert _sync_event(test_client, "cam-1", "smart-1", event_type="smart_motion", parent_local_event_id="motion-1").status_code == 200

    response = _share(test_client, "cam-1", "smart-1", parent_local_event_id="motion-1")
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"

    with override_target(sqlite_path=str(database)):
        with connection() as db:
            rows = db.execute(
                "SELECT dem.id AS media_row_id,dem.detection_event_id,dem.s3_key,dem.thumbnail_s3_key,dem.source_media_id,de.local_event_id "
                "FROM detection_event_media dem JOIN detection_events de ON de.id=dem.detection_event_id "
                "ORDER BY de.local_event_id"
            ).fetchall()
    assert len(rows) == 2
    by_local_id = {r["local_event_id"]: r for r in rows}
    assert set(by_local_id) == {"motion-1", "smart-1"}
    assert by_local_id["motion-1"]["detection_event_id"] != by_local_id["smart-1"]["detection_event_id"]
    assert by_local_id["motion-1"]["s3_key"] == by_local_id["smart-1"]["s3_key"] == base_media["s3_key"]
    assert by_local_id["motion-1"]["thumbnail_s3_key"] == by_local_id["smart-1"]["thumbnail_s3_key"] == base_media["thumbnail_s3_key"]
    # Provenance: the child's row is explicitly a reference (to the
    # ROOT MEDIA row's own id, not the event id), never an owner.
    assert by_local_id["motion-1"]["source_media_id"] is None
    assert by_local_id["smart-1"]["source_media_id"] == by_local_id["motion-1"]["media_row_id"]


def test_duplicate_replayed_share_is_idempotent(client, monkeypatch):
    test_client, database = client
    _register_base_motion_with_media(test_client, "cam-1", "motion-1")
    assert _sync_event(test_client, "cam-1", "smart-1", event_type="smart_motion", parent_local_event_id="motion-1").status_code == 200

    first = _share(test_client, "cam-1", "smart-1", parent_local_event_id="motion-1")
    second = _share(test_client, "cam-1", "smart-1", parent_local_event_id="motion-1")

    assert first.status_code == 200 and first.json()["status"] == "accepted"
    assert second.status_code == 200 and second.json()["status"] == "duplicate"
    assert first.json()["media_id"] == second.json()["media_id"]

    with override_target(sqlite_path=str(database)):
        with connection() as db:
            count = db.execute(
                "SELECT COUNT(*) c FROM detection_event_media dem JOIN detection_events de ON de.id=dem.detection_event_id WHERE de.local_event_id='smart-1'"
            ).fetchone()["c"]
    assert count == 1


# --------------------------------------------------------- negative / security cases


def test_unrelated_motion_event_is_rejected_even_though_it_is_real_and_registered(client):
    """The child is genuinely correlated with motion-1, but the request
    claims motion-2 (a real, unrelated, registered event) instead."""
    test_client, _ = client
    _register_base_motion_with_media(test_client, "cam-1", "motion-1")
    _register_base_motion_with_media(test_client, "cam-1", "motion-2")
    assert _sync_event(test_client, "cam-1", "smart-1", event_type="smart_motion", parent_local_event_id="motion-1").status_code == 200

    response = _share(test_client, "cam-1", "smart-1", parent_local_event_id="motion-2")
    assert response.status_code == 403


def test_fake_parent_local_event_id_is_rejected(client):
    test_client, _ = client
    _register_base_motion_with_media(test_client, "cam-1", "motion-1")
    assert _sync_event(test_client, "cam-1", "smart-1", event_type="smart_motion", parent_local_event_id="motion-1").status_code == 200

    response = _share(test_client, "cam-1", "smart-1", parent_local_event_id="totally-made-up")
    assert response.status_code == 403


def test_nonexistent_parent_before_any_correlation_established_is_retryable_not_a_hard_failure(client):
    """The child never resolved a parent at all -- the request is
    rejected as retryable-pending, never treated as an authorization
    bypass attempt."""
    test_client, _ = client
    assert _sync_event(test_client, "cam-1", "smart-1", event_type="smart_motion", parent_local_event_id="never-existed").status_code == 200

    response = _share(test_client, "cam-1", "smart-1", parent_local_event_id="never-existed")
    assert response.status_code == 409
    assert "parent_media_pending" in response.json()["detail"]


def test_parent_from_a_different_camera_is_rejected(client):
    test_client, _ = client
    _register_base_motion_with_media(test_client, "cam-2", "motion-on-cam-2")
    # The child's own local_event_id namespace is per-appliance; simulate
    # a smart_motion event on cam-1 that (incorrectly) resolved against
    # cam-2's motion event -- resolution itself is camera-scoped, so this
    # can only happen if cam-1's own sync never resolves it (since
    # _resolve_parent_motion_event scopes by camera_id) -- confirm it
    # stays unresolved rather than crossing cameras.
    assert _sync_event(test_client, "cam-1", "smart-1", event_type="smart_motion", parent_local_event_id="motion-on-cam-2").status_code == 200

    response = _share(test_client, "cam-1", "smart-1", parent_local_event_id="motion-on-cam-2")
    assert response.status_code == 409  # never resolved -- cross-camera resolution never happened
    assert "parent_media_pending" in response.json()["detail"]


def test_parent_from_a_different_appliance_and_customer_is_rejected(client):
    test_client, _ = client
    _register_base_motion_with_media(test_client, "cam-3", "motion-shared-id", appliance_id="appl-2", credential="credential-2", prefix="cust-2/site-2/appl-2")
    # appl-1 tries to correlate against appl-2's own local_event_id --
    # resolution is scoped to (this camera, THIS appliance), so it can
    # never resolve to appl-2's event even if the id string collides.
    assert _sync_event(test_client, "cam-1", "smart-1", event_type="smart_motion", parent_local_event_id="motion-shared-id").status_code == 200

    response = _share(test_client, "cam-1", "smart-1", parent_local_event_id="motion-shared-id")
    assert response.status_code == 409
    assert "parent_media_pending" in response.json()["detail"]


def test_parent_without_registered_media_is_retryable(client):
    test_client, _ = client
    assert _sync_event(test_client, "cam-1", "motion-no-media").status_code == 200
    assert _sync_event(test_client, "cam-1", "smart-1", event_type="smart_motion", parent_local_event_id="motion-no-media").status_code == 200

    response = _share(test_client, "cam-1", "smart-1", parent_local_event_id="motion-no-media")
    assert response.status_code == 409
    assert "parent_media_pending" in response.json()["detail"]


def test_non_smart_motion_event_cannot_use_the_shared_route(client):
    test_client, _ = client
    _register_base_motion_with_media(test_client, "cam-1", "motion-other")
    assert _sync_event(test_client, "cam-1", "motion-1", event_type="motion").status_code == 200

    response = _share(test_client, "cam-1", "motion-1", parent_local_event_id="motion-other")
    assert response.status_code == 403
    assert "smart_motion" in response.json()["detail"]


def test_parent_type_other_than_motion_is_rejected(client):
    """The child's own parent_detection_event_id happens to point at a
    real, existing row -- but that row's own stored type isn't
    'motion' (a data-integrity/defensive check; ingestion-time
    resolution already only ever resolves against event_type='motion',
    so this exercises the route's own defense-in-depth re-check)."""
    test_client, database = client
    assert _sync_event(test_client, "cam-1", "person-1", event_type="person").status_code == 200
    assert _sync_event(test_client, "cam-1", "smart-1", event_type="smart_motion", parent_local_event_id="person-1").status_code == 200

    # Ingestion-time resolution requires event_type='motion', so this
    # child stays unresolved -- confirm the share attempt is rejected
    # as pending, never as if a non-motion parent had been accepted.
    response = _share(test_client, "cam-1", "smart-1", parent_local_event_id="person-1")
    assert response.status_code == 409


# --------------------------------------------------------- existing paths, unchanged


def test_ordinary_motion_self_key_path_is_completely_unaffected(client):
    test_client, database = client
    assert _sync_event(test_client, "cam-1", "evt-1").status_code == 200

    first = test_client.post("/api/appliance/analytics/cam-1/events/evt-1/media", headers=_headers(), json=_media_payload("evt-1", camera_id="cam-1"))
    second = test_client.post("/api/appliance/analytics/cam-1/events/evt-1/media", headers=_headers(), json=_media_payload("evt-1", camera_id="cam-1"))
    assert first.status_code == 200 and first.json()["status"] == "accepted"
    assert second.status_code == 200 and second.json()["status"] == "duplicate"

    rejected = test_client.post(
        "/api/appliance/analytics/cam-1/events/evt-1/media",
        headers=_headers(),
        json={**_media_payload("evt-1", camera_id="cam-1"), "s3_key": "recordings/cust-1/site-1/appl-1/cam-1/2026/08/21/archive/unrelated.mp4"},
    )
    assert rejected.status_code == 403
