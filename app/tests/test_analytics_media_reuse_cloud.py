"""Cloud side of PPE / Facial Recognition / People Counting media reuse
(2026-09-26): a result may show an already-uploaded clip only when the
appliance names, by local id, a parent the cloud independently verifies --
same camera, same appliance, same customer/site, an allowed parent type,
an original (not itself shared) upload, and a clip window that contains
the result's own moment. Then the customer portal serves that clip and
snapshot for the result exactly as it does for any other event.

Real FastAPI TestClient + real SQLite, the same pattern as
test_shared_smart_motion_media_authorization.py (whose Smart Motion
behaviour this change leaves untouched -- that suite still covers it).
"""

import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_analytics_media_reuse_cloud_import.db"):
    import appliance_cloud
    import customer_analytics_workspace
    from partner_db import connection, initialize_database, password_hash

NOW = "2026-09-26T00:00:00"
CLIP = ("2026-09-26T10:00:00", "2026-09-26T10:00:10")  # the parent's 5 s + 5 s window
MOMENT = "2026-09-26T10:00:05"


def _headers(appliance_id="appl-1", credential="credential"):
    return {"X-Appliance-Id": appliance_id, "X-Request-Timestamp": str(int(time.time())),
            "X-Request-Nonce": secrets.token_hex(16), "Authorization": f"Bearer {credential}"}


def _seed(db):
    db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-1", "Partner", "approved", "real", NOW))
    for n in ("1", "2"):
        db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", (f"cust-{n}", "partner-1", f"Customer {n}", f"c{n}@example.test", "active", "real", NOW))
        db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{n}", f"cust-{n}", "Site", NOW))
        db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", (f"appl-{n}", f"cust-{n}", f"site-{n}", f"AIC-TEST-{n}", NOW))
        db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", (f"cred-{n}", f"appl-{n}", password_hash(f"credential-{n}" if n == "2" else "credential"), NOW))
        db.execute("INSERT INTO plans(id,customer_id,recording_mode,retention_days,created_at) VALUES(?,?,?,?,?)", (f"plan-{n}", f"cust-{n}", "motion", 7, NOW))
    for camera, n, number in (("cam-1", "1", 1), ("cam-2", "1", 2), ("cam-3", "2", 1)):
        db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,cloud_recording_mode,status,camera_number,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (camera, f"cust-{n}", f"site-{n}", f"appl-{n}", camera, "motion", "configured", number, NOW))


@pytest.fixture(autouse=True)
def _analytics_sync_enabled(monkeypatch):
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True)
    # The per-appliance request limiter is process-wide; these tests make
    # many requests as appl-1 and must not starve the suites after them.
    appliance_cloud.request_limiter.events.clear()
    yield
    appliance_cloud.request_limiter.events.clear()


@pytest.fixture()
def cloud(tmp_path):
    database = tmp_path / "reuse.db"
    with override_target(sqlite_path=str(database)):
        initialize_database()
        with connection() as db:
            _seed(db)
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *args, **kwargs: "")
        with TestClient(app) as client:
            yield client


def _sync(client, camera, local_id, event_type, parent=None, timestamp=MOMENT, appliance="appl-1", credential="credential", detections=None):
    body = {"local_event_id": local_id, "event_type": event_type, "confidence": 0.9, "object_count": 1,
            "detections": detections if detections is not None else [], "event_timestamp": timestamp}
    if parent:
        body["parent_local_event_id"] = parent
    return client.post(f"/api/appliance/analytics/{camera}/events", headers=_headers(appliance, credential), json=body)


def _owner_with_clip(client, camera, local_id, event_type="person", clip=CLIP, appliance="appl-1", credential="credential", prefix="cust-1/site-1/appl-1"):
    """An event that uploaded its own clip -- the unchanged self-key route."""
    assert _sync(client, camera, local_id, event_type, timestamp=clip[0].replace(":00:00", ":00:05"), appliance=appliance, credential=credential).status_code == 200
    key = f"recordings/{prefix}/{camera}/2026/09/26/events/motion_{local_id}"
    response = client.post(f"/api/appliance/analytics/{camera}/events/{local_id}/media", headers=_headers(appliance, credential),
                           json={"s3_key": key + ".mp4", "thumbnail_s3_key": key + ".jpg", "started_at": clip[0], "ended_at": clip[1],
                                 "duration_seconds": 10.0, "size_bytes": 1000})
    assert response.status_code == 200 and response.json()["status"] == "accepted", response.text
    return key


def _share(client, camera, local_id, parent, appliance="appl-1", credential="credential"):
    return client.post(f"/api/appliance/analytics/{camera}/events/{local_id}/media/shared",
                       headers=_headers(appliance, credential), json={"parent_local_event_id": parent})


def _media(local_id, camera="cam-1"):
    with connection() as db:
        row = db.execute("SELECT dem.* FROM detection_event_media dem JOIN detection_events de ON de.id=dem.detection_event_id "
                         "WHERE de.local_event_id=? AND de.camera_id=?", (local_id, camera)).fetchone()
        return dict(row) if row else None


def _media_count():
    with connection() as db:
        return db.execute("SELECT COUNT(*) FROM detection_event_media").fetchone()[0]


# ------------------------------------------------ each analytic, same camera, same moment

@pytest.mark.parametrize("child_type,detections", [
    ("ppe", [{"hard_hat_present": False, "safety_vest_present": True}]),
    ("facial_recognition", [{"match_state": "recognized", "matched_person_name": "Ana", "engine": "t"}]),
    ("people_counting_in", []),
    ("people_counting_out", []),
])
def test_result_reuses_the_clip_of_the_same_moment_on_the_same_camera(cloud, child_type, detections):
    key = _owner_with_clip(cloud, "cam-1", "owner-1")
    assert _sync(cloud, "cam-1", "child-1", child_type, parent="owner-1", detections=detections).status_code == 200
    response = _share(cloud, "cam-1", "child-1", "owner-1")
    assert response.status_code == 200 and response.json()["status"] == "accepted", response.text
    child, owner = _media("child-1"), _media("owner-1")
    # The owner's own clip and snapshot -- nothing new stored or uploaded.
    assert child["s3_key"] == key + ".mp4" and child["thumbnail_s3_key"] == key + ".jpg"
    assert child["source_media_id"] == owner["id"] and owner["source_media_id"] is None
    assert (child["started_at"], child["ended_at"]) == CLIP
    # Idempotent replay, no second row.
    assert _share(cloud, "cam-1", "child-1", "owner-1").json()["status"] == "duplicate"
    assert _media_count() == 2


def test_facial_and_people_counting_may_reuse_a_clip_of_their_own_kind(cloud):
    _owner_with_clip(cloud, "cam-1", "face-owner", event_type="facial_recognition")
    assert _sync(cloud, "cam-1", "face-2", "facial_recognition", parent="face-owner").status_code == 200
    assert _share(cloud, "cam-1", "face-2", "face-owner").json()["status"] == "accepted"
    _owner_with_clip(cloud, "cam-2", "count-owner", event_type="people_counting_in")
    assert _sync(cloud, "cam-2", "count-2", "people_counting_out", parent="count-owner").status_code == 200
    assert _share(cloud, "cam-2", "count-2", "count-owner").json()["status"] == "accepted"


def test_the_moment_at_the_edges_of_the_clip_window_counts_and_nothing_beyond(cloud):
    _owner_with_clip(cloud, "cam-1", "owner-1")
    for local_id, moment, accepted in (("start", CLIP[0], True), ("end", CLIP[1], True),
                                       ("before", "2026-09-26T09:59:59.999000", False), ("after", "2026-09-26T10:00:10.001000", False)):
        assert _sync(cloud, "cam-1", local_id, "facial_recognition", parent="owner-1", timestamp=moment).status_code == 200
        response = _share(cloud, "cam-1", local_id, "owner-1")
        assert (response.status_code == 200) is accepted, (local_id, response.text)
        assert (_media(local_id) is not None) is accepted


# ------------------------------------------------ what must never attach

def test_ambiguous_nearby_clips_only_the_one_that_recorded_the_moment_attaches(cloud):
    """Two clips on the same camera 20 s apart: a result at 10:00:25 is
    inside the second clip only. Naming the first one is refused even
    though it is 'close'."""
    _owner_with_clip(cloud, "cam-1", "first")
    _owner_with_clip(cloud, "cam-1", "second", clip=("2026-09-26T10:00:20", "2026-09-26T10:00:30"), prefix="cust-1/site-1/appl-1")
    late = "2026-09-26T10:00:25"
    assert _sync(cloud, "cam-1", "wrong", "ppe", parent="first", timestamp=late).status_code == 200
    response = _share(cloud, "cam-1", "wrong", "first")
    assert response.status_code == 403 and "outside" in response.json()["detail"]
    assert _media("wrong") is None
    assert _sync(cloud, "cam-1", "right", "ppe", parent="second", timestamp=late).status_code == 200
    assert _share(cloud, "cam-1", "right", "second").json()["status"] == "accepted"
    assert _media("right")["s3_key"].endswith("motion_second.mp4")


def test_no_cross_camera_association(cloud):
    _owner_with_clip(cloud, "cam-1", "owner-1")
    # A result on cam-2 naming cam-1's clip owner never resolves a parent.
    assert _sync(cloud, "cam-2", "child-1", "ppe", parent="owner-1").status_code == 200
    with connection() as db:
        assert db.execute("SELECT parent_detection_event_id FROM detection_events WHERE local_event_id='child-1'").fetchone()[0] is None
    response = _share(cloud, "cam-2", "child-1", "owner-1")
    assert response.status_code == 409 and "parent_media_pending" in response.json()["detail"]
    assert _media("child-1", camera="cam-2") is None


def test_tenant_isolation(cloud):
    _owner_with_clip(cloud, "cam-1", "owner-1")
    # Another customer's appliance naming this customer's clip owner, on its own camera: never resolves.
    assert _sync(cloud, "cam-3", "theirs", "facial_recognition", parent="owner-1", appliance="appl-2", credential="credential-2").status_code == 200
    assert _share(cloud, "cam-3", "theirs", "owner-1", appliance="appl-2", credential="credential-2").status_code == 409
    # ...nor can it register anything on this customer's camera.
    assert _share(cloud, "cam-1", "owner-1", "owner-1", appliance="appl-2", credential="credential-2").status_code in (403, 404)
    assert _media("theirs", camera="cam-3") is None and _media_count() == 1


def test_no_matching_media_is_pending_never_invented(cloud):
    # The owner synced but never uploaded a clip.
    assert _sync(cloud, "cam-1", "owner-1", "person").status_code == 200
    assert _sync(cloud, "cam-1", "child-1", "ppe", parent="owner-1").status_code == 200
    response = _share(cloud, "cam-1", "child-1", "owner-1")
    assert response.status_code == 409 and "parent_media_pending" in response.json()["detail"]
    # No parent named at all: no correlation, nothing to share.
    assert _sync(cloud, "cam-1", "child-2", "ppe").status_code == 200
    assert _share(cloud, "cam-1", "child-2", "owner-1").status_code == 409
    assert _media_count() == 0


def test_disallowed_parent_types_never_resolve(cloud):
    _owner_with_clip(cloud, "cam-1", "motion-1", event_type="motion")
    _owner_with_clip(cloud, "cam-2", "ppe-1", event_type="ppe")
    # PPE may only use the YOLO scan's clip, not a Motion clip; nobody may use a PPE clip.
    for camera, child, child_type, parent in (("cam-1", "c1", "ppe", "motion-1"), ("cam-2", "c2", "facial_recognition", "ppe-1"),
                                              ("cam-2", "c3", "people_counting_in", "ppe-1")):
        assert _sync(cloud, camera, child, child_type, parent=parent).status_code == 200
        assert _share(cloud, camera, child, parent).status_code == 409, child
    # Other event types still can't use the shared route at all.
    assert _sync(cloud, "cam-1", "plate-1", "plate", parent="motion-1").status_code == 200
    assert _share(cloud, "cam-1", "plate-1", "motion-1").status_code == 403


def test_a_shared_row_is_never_shared_again(cloud):
    """Retention cascades one level (root -> rows naming it), so a clip is
    only ever shared from its original upload."""
    _owner_with_clip(cloud, "cam-1", "person-1")
    assert _sync(cloud, "cam-1", "face-1", "facial_recognition", parent="person-1").status_code == 200
    assert _share(cloud, "cam-1", "face-1", "person-1").json()["status"] == "accepted"
    assert _sync(cloud, "cam-1", "face-2", "facial_recognition", parent="face-1").status_code == 200
    response = _share(cloud, "cam-1", "face-2", "face-1")
    assert response.status_code == 403 and "itself shared" in response.json()["detail"]


def test_a_result_synced_before_its_owner_links_once_the_owner_arrives(cloud):
    """The appliance replays the result's own sync before sharing (as
    register_shared_event_media() does); the correlation is completed once
    and then frozen."""
    assert _sync(cloud, "cam-1", "child-1", "ppe", parent="owner-1").status_code == 200  # owner not synced yet
    _owner_with_clip(cloud, "cam-1", "owner-1")
    assert _sync(cloud, "cam-1", "child-1", "ppe", parent="owner-1").json()["status"] == "duplicate"
    assert _share(cloud, "cam-1", "child-1", "owner-1").json()["status"] == "accepted"
    _owner_with_clip(cloud, "cam-1", "owner-2")
    assert _sync(cloud, "cam-1", "child-1", "ppe", parent="owner-2").status_code == 409  # frozen


# ------------------------------------------------ portal: Analytics card, snapshot, inline playback

@pytest.fixture()
def portal(cloud, monkeypatch):
    import main
    import partner_portal
    with connection() as db:
        for user, customer in (("o1", "cust-1"), ("o2", "cust-2")):
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (user, "partner-1", f"{user}@example.test", user, "customer_owner", "x", 1, customer, NOW, "active", "all"))
    monkeypatch.setattr(main, "_presigned_recording_url", lambda key: f"https://s3.example/{key}")
    monkeypatch.setattr(main, "_presigned_recording_url_and_ttl", lambda key: (f"https://s3.example/{key}", 300))

    def session(email, customer):
        client = TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False)
        client.cookies.set(partner_portal.SESSION_COOKIE, partner_portal._token(email, "customer_owner", None, customer, None))
        return client
    return session


def _cloud_id(local_id):
    with connection() as db:
        return db.execute("SELECT id FROM detection_events WHERE local_event_id=?", (local_id,)).fetchone()[0]


@pytest.mark.parametrize("child_type,workspace", [("ppe", "ppe"), ("facial_recognition", "facial_recognition"), ("people_counting_in", "people_counting")])
def test_portal_serves_the_reused_snapshot_and_clip_to_the_owner_only(cloud, portal, child_type, workspace):
    key = _owner_with_clip(cloud, "cam-1", "owner-1")
    assert _sync(cloud, "cam-1", "child-1", child_type, parent="owner-1").status_code == 200
    assert _share(cloud, "cam-1", "child-1", "owner-1").json()["status"] == "accepted"
    child = _cloud_id("child-1")

    # The Analytics workspace card: a snapshot and a clip, no longer "No preview available".
    data = customer_analytics_workspace.query_events(customer_id="cust-1", camera_ids=["cam-1"], key=workspace,
                                                     start_ms=0, end_ms=4102444800000)
    item = next(e for e in data["events"] if e["event_id"] == child)
    assert item["has_thumbnail"] and item["has_clip"]

    with portal("o1@example.test", "cust-1") as owner:
        snapshot = owner.get(f"/api/customer/events/cam-1/{child}/thumbnail")
        assert snapshot.status_code == 302 and snapshot.headers["location"] == f"https://s3.example/{key}.jpg"
        clip = owner.get(f"/api/customer/events/cam-1/{child}/media/url")  # what the inline player loads
        assert clip.status_code == 200 and clip.json()["url"] == f"https://s3.example/{key}.mp4"
        # Scoped by camera: the same event id under another camera finds nothing.
        assert owner.get(f"/api/customer/events/cam-2/{child}/thumbnail").status_code == 404

    with portal("o2@example.test", "cust-2") as other:
        assert other.get(f"/api/customer/events/cam-1/{child}/thumbnail").status_code == 403
        assert other.get(f"/api/customer/events/cam-1/{child}/media/url").status_code == 403
    other_data = customer_analytics_workspace.query_events(customer_id="cust-2", camera_ids=["cam-1"], key=workspace,
                                                           start_ms=0, end_ms=4102444800000)
    assert other_data["events"] == []
