"""AAC edge/cloud sync -- Phase 2. Covers the three gaps identified in
the Phase 1 Codex review:

1. analytics_sync.py's facial_recognition special case in
   _build_payload() (mirrors the existing, already-tested ppe special
   case exactly).
2. appliance_cloud.py's facial_events detail-row creation inside
   analytics_event_available(), and the new
   GET /api/appliance/facial-directory route (embedding distribution).
3. facial_embedding_sync.py's edge-side full-replace logic.

Thumbnail cloud sync is not implemented in this pass -- see the Phase 2
report's own note.
"""

import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import analytics_sync as asy
from database_backend import override_target

with override_target(sqlite_path="/tmp/test_facial_recognition_cloud_sync_import.db"):
    import appliance_cloud
    import facial_embedding_sync
    import facial_people
    import partner_portal
    from partner_db import connection, initialize_database, password_hash

NOW = "2026-09-08T00:00:00"


# --------------------------------------------------------------- analytics_sync.py payload builder


def test_facial_recognition_event_forwards_match_fields_via_detections():
    event = {
        "id": "evt-1",
        "event_type": "facial_recognition",
        "camera": 1,
        "timestamp": NOW,
        "confidence": 0.91,
        "match_state": "known",
        "matched_person_id": "person-1",
        "matched_person_name": "Alice",
        "matched_watchlist_id": None,
        "matched_watchlist_name": None,
        "engine": "onnx_yunet_sface",
        "engine_version": "1",
    }
    payload = asy._build_payload(event)
    assert payload["detections"] == [{
        "match_state": "known",
        "matched_person_id": "person-1",
        "matched_person_name": "Alice",
        "matched_watchlist_id": None,
        "matched_watchlist_name": None,
        "engine": "onnx_yunet_sface",
        "engine_version": "1",
        # Face Access (2026-09-17): always present, None here because
        # the source event has no door_notify_message key at all --
        # see test_analytics_sync.py's own dedicated mode1/2/3 coverage
        # for when this is a real message.
        "door_notify_message": None,
    }]


def test_facial_recognition_event_missing_fields_forwards_none_not_a_crash():
    event = {"id": "evt-1", "event_type": "facial_recognition", "timestamp": NOW}
    payload = asy._build_payload(event)
    assert payload["detections"][0]["match_state"] is None


def test_facial_recognition_does_not_affect_ppe_or_plain_events():
    ppe_event = {"id": "e1", "event_type": "ppe", "timestamp": NOW, "hard_hat_present": True, "safety_vest_present": False}
    assert asy._build_payload(ppe_event)["detections"] == [{"hard_hat_present": True, "safety_vest_present": False}]
    plain_event = {"id": "e2", "event_type": "person", "timestamp": NOW}
    assert asy._build_payload(plain_event)["detections"] is None


# --------------------------------------------------------------- appliance_cloud.py cloud-side routes


def _appliance_auth_headers(appliance_id: str, credential: str) -> dict:
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


def _seed(db, *, appliance_id="appl-1", cloud_id="AIC-TEST0001", credential="test-credential", customer_id="cust-1", camera_id="cam-1"):
    db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (NOW,))
    db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", (customer_id, "p1", "C", f"{customer_id}@example.test", "active", "real", NOW))
    site_id = f"site-{customer_id}"
    db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Site", NOW))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", (appliance_id, customer_id, site_id, cloud_id, NOW))
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", (f"cred-{appliance_id}", appliance_id, password_hash(credential), NOW))
    db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at,camera_number) VALUES(?,?,?,?,?,?,?,?)", (camera_id, customer_id, site_id, appliance_id, "Camera 1", "active", NOW, 1))


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_cloud_sync.db"


@pytest.fixture()
def client(db_path, monkeypatch):
    monkeypatch.setattr(appliance_cloud, "ANALYTICS_SYNC_ENABLED", True)
    monkeypatch.setattr(appliance_cloud, "FACIAL_EMBEDDING_SYNC_ENABLED", True)
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        with connection() as db:
            _seed(db)
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


def test_analytics_event_creates_facial_events_row(client, db_path):
    payload = {
        "local_event_id": "levt-1",
        "event_type": "facial_recognition",
        "confidence": 0.85,
        "object_count": 1,
        "detections": [{
            "match_state": "known", "matched_person_id": "person-1", "matched_person_name": "Alice",
            "matched_watchlist_id": None, "matched_watchlist_name": None,
            "engine": "onnx_yunet_sface", "engine_version": "1",
        }],
        "event_timestamp": NOW,
    }
    response = client.post("/api/appliance/analytics/cam-1/events", json=payload, headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert response.status_code == 200
    event_id = response.json()["event_id"]
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            row = db.execute("SELECT * FROM facial_events WHERE detection_event_id=?", (event_id,)).fetchone()
    assert row is not None
    assert row["match_state"] == "known"
    assert row["matched_person_name"] == "Alice"
    assert row["customer_id"] == "cust-1"


def test_analytics_event_replay_does_not_duplicate_facial_events_row(client, db_path):
    payload = {
        "local_event_id": "levt-1", "event_type": "facial_recognition", "confidence": 0.85,
        "detections": [{"match_state": "unknown"}], "event_timestamp": NOW,
    }
    headers = _appliance_auth_headers("appl-1", "test-credential")
    first = client.post("/api/appliance/analytics/cam-1/events", json=payload, headers=headers)
    assert first.json()["status"] == "accepted"
    second = client.post("/api/appliance/analytics/cam-1/events", json=payload, headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert second.json()["status"] == "duplicate"
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM facial_events").fetchone()["n"]
    assert count == 1


def test_non_facial_event_type_creates_no_facial_events_row(client, db_path):
    payload = {"local_event_id": "levt-ppe", "event_type": "ppe", "confidence": 0.9, "detections": [{"hard_hat_present": True}], "event_timestamp": NOW}
    client.post("/api/appliance/analytics/cam-1/events", json=payload, headers=_appliance_auth_headers("appl-1", "test-credential"))
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM facial_events").fetchone()["n"]
    assert count == 0


def test_facial_directory_route_requires_authentication(client):
    response = client.get("/api/appliance/facial-directory")
    assert response.status_code == 401


def test_facial_directory_route_returns_404_when_disabled(client, monkeypatch):
    monkeypatch.setattr(appliance_cloud, "FACIAL_EMBEDDING_SYNC_ENABLED", False)
    response = client.get("/api/appliance/facial-directory", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert response.status_code == 404


def test_facial_directory_route_returns_enrolled_people_and_embeddings(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
            facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="onnx_yunet_sface", engine_version="1", now=NOW)
            watchlist_id = facial_people.create_watchlist(db, customer_id="cust-1", name="Banned", now=NOW)
            facial_people.add_watchlist_member(db, customer_id="cust-1", watchlist_id=watchlist_id, person_id=person_id, now=NOW)
    response = client.get("/api/appliance/facial-directory", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert response.status_code == 200
    body = response.json()
    assert body["customer_id"] == "cust-1"
    assert [p["display_name"] for p in body["people"]] == ["Alice"]
    assert len(body["embeddings"]) == 1
    assert [w["name"] for w in body["watchlists"]] == ["Banned"]
    assert body["watchlist_members"][0]["person_id"] == person_id


def test_facial_directory_never_returns_another_customers_data(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-2", cloud_id="AIC-TEST0002", credential="other-credential", customer_id="cust-2", camera_id="cam-2")
            facial_people.enroll_person(db, customer_id="cust-2", display_name="Mallory", now=NOW)
            facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    response = client.get("/api/appliance/facial-directory", headers=_appliance_auth_headers("appl-1", "test-credential"))
    names = [p["display_name"] for p in response.json()["people"]]
    assert names == ["Alice"]
    assert "Mallory" not in names


def test_facial_event_thumbnail_upload_and_tenant_isolation(client, db_path, tmp_path, monkeypatch):
    import base64

    import object_storage

    monkeypatch.setattr(object_storage, "get_storage", lambda: object_storage.LocalStorage(root=tmp_path / "storage"))
    payload = {
        "local_event_id": "levt-1", "event_type": "facial_recognition", "confidence": 0.9,
        "detections": [{"match_state": "known"}], "event_timestamp": NOW,
    }
    created = client.post("/api/appliance/analytics/cam-1/events", json=payload, headers=_appliance_auth_headers("appl-1", "test-credential"))
    detection_event_id = created.json()["event_id"]
    image_b64 = base64.b64encode(b"fake-jpeg-bytes").decode()

    response = client.post(
        f"/api/appliance/facial-events/{detection_event_id}/thumbnail",
        json={"image_base64": image_b64},
        headers=_appliance_auth_headers("appl-1", "test-credential"),
    )
    assert response.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            row = db.execute("SELECT face_thumbnail_path FROM facial_events WHERE detection_event_id=?", (detection_event_id,)).fetchone()
    assert row["face_thumbnail_path"] is not None

    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed(db, appliance_id="appl-2", cloud_id="AIC-TEST0002", credential="other-credential", customer_id="cust-2", camera_id="cam-2")
    cross_tenant = client.post(
        f"/api/appliance/facial-events/{detection_event_id}/thumbnail",
        json={"image_base64": image_b64},
        headers=_appliance_auth_headers("appl-2", "other-credential"),
    )
    assert cross_tenant.status_code == 403


def test_facial_event_thumbnail_requires_authentication(client):
    response = client.post("/api/appliance/facial-events/whatever/thumbnail", json={"image_base64": "AA=="})
    assert response.status_code == 401


def test_facial_event_thumbnail_rejects_invalid_base64(client):
    payload = {"local_event_id": "levt-1", "event_type": "facial_recognition", "confidence": 0.9, "detections": [{"match_state": "known"}], "event_timestamp": NOW}
    created = client.post("/api/appliance/analytics/cam-1/events", json=payload, headers=_appliance_auth_headers("appl-1", "test-credential"))
    detection_event_id = created.json()["event_id"]
    response = client.post(
        f"/api/appliance/facial-events/{detection_event_id}/thumbnail",
        json={"image_base64": "not valid base64!!"},
        headers=_appliance_auth_headers("appl-1", "test-credential"),
    )
    assert response.status_code == 400


def test_facial_directory_excludes_disabled_people(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
            facial_people.update_person(db, customer_id="cust-1", person_id=person_id, status="disabled", now=NOW)
    response = client.get("/api/appliance/facial-directory", headers=_appliance_auth_headers("appl-1", "test-credential"))
    assert response.json()["people"] == []


# --------------------------------------------------------------- facial_embedding_sync.py (edge side)


@pytest.fixture()
def edge_db_path(tmp_path):
    return tmp_path / "test_edge_directory.db"


def test_replace_local_directory_inserts_the_full_snapshot(edge_db_path):
    with override_target(sqlite_path=str(edge_db_path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (NOW,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-1','p1','C','c@example.test','active','real',?)", (NOW,))
        directory = {
            "people": [{"id": "person-1", "display_name": "Alice", "status": "active"}],
            "embeddings": [{"id": "emb-1", "person_id": "person-1", "engine": "onnx_yunet_sface", "engine_version": "1", "embedding_json": "[1.0, 0.0]"}],
            "watchlists": [{"id": "wl-1", "name": "Banned", "classification": "alert"}],
            "watchlist_members": [{"watchlist_id": "wl-1", "person_id": "person-1"}],
        }
        summary = facial_embedding_sync._replace_local_directory("cust-1", directory)
        assert summary == {"people": 1, "embeddings": 1, "watchlists": 1, "watchlist_members": 1}
        with connection() as db:
            people = facial_people.list_people(db, customer_id="cust-1")
            assert [p["display_name"] for p in people] == ["Alice"]
            enrolled = facial_people.enrolled_embeddings_for_matching(db, customer_id="cust-1", engine="onnx_yunet_sface", engine_version="1")
            assert len(enrolled) == 1
            assert facial_people.watchlisted_person_ids(db, customer_id="cust-1") == {"person-1"}


def test_replace_local_directory_removes_people_no_longer_in_the_snapshot(edge_db_path):
    """The core correctness property: a person deleted on the cloud
    (hard-deleted there, per Phase 1's facial_people.delete_person())
    must not linger as a stale, still-matchable embedding on the edge
    forever -- the next full-replace sync removes it automatically."""
    with override_target(sqlite_path=str(edge_db_path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (NOW,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-1','p1','C','c@example.test','active','real',?)", (NOW,))
        facial_embedding_sync._replace_local_directory("cust-1", {
            "people": [{"id": "person-1", "display_name": "Alice", "status": "active"}],
            "embeddings": [{"id": "emb-1", "person_id": "person-1", "engine": "onnx_yunet_sface", "engine_version": "1", "embedding_json": "[1.0]"}],
            "watchlists": [], "watchlist_members": [],
        })
        # Second sync: Alice is gone from the cloud snapshot entirely.
        facial_embedding_sync._replace_local_directory("cust-1", {"people": [], "embeddings": [], "watchlists": [], "watchlist_members": []})
        with connection() as db:
            assert facial_people.list_people(db, customer_id="cust-1") == []
            assert facial_people.enrolled_embeddings_for_matching(db, customer_id="cust-1", engine="onnx_yunet_sface", engine_version="1") == []


def test_replace_local_directory_never_touches_a_different_customers_rows(edge_db_path):
    with override_target(sqlite_path=str(edge_db_path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (NOW,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-1','p1','C1','c1@example.test','active','real',?)", (NOW,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-2','p1','C2','c2@example.test','active','real',?)", (NOW,))
        with connection() as db:
            other_person = facial_people.enroll_person(db, customer_id="cust-2", display_name="Untouched", now=NOW)
        facial_embedding_sync._replace_local_directory("cust-1", {"people": [{"id": "person-1", "display_name": "Alice", "status": "active"}], "embeddings": [], "watchlists": [], "watchlist_members": []})
        with connection() as db:
            assert facial_people.get_person(db, customer_id="cust-2", person_id=other_person) is not None


def test_replace_local_directory_never_syncs_the_source_enrollment_image(edge_db_path):
    """Only the embedding vector crosses this sync -- see
    facial_embedding_sync.py's own module docstring: the original/
    aligned enrollment image never leaves the cloud."""
    with override_target(sqlite_path=str(edge_db_path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (NOW,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-1','p1','C','c@example.test','active','real',?)", (NOW,))
        facial_embedding_sync._replace_local_directory("cust-1", {
            "people": [{"id": "person-1", "display_name": "Alice", "status": "active"}],
            "embeddings": [{"id": "emb-1", "person_id": "person-1", "engine": "e", "engine_version": "1", "embedding_json": "[1.0]", "source_image_path": "/cloud/only/path.jpg"}],
            "watchlists": [], "watchlist_members": [],
        })
        with connection() as db:
            images = facial_people.list_reference_images(db, customer_id="cust-1", person_id="person-1")
    assert images[0]["source_image_path"] is None


def test_sync_facial_directory_returns_no_identity_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(facial_embedding_sync, "CREDENTIAL_FILE", tmp_path / "missing.json")
    assert facial_embedding_sync.sync_facial_directory() == {"status": "no_identity"}


def test_sync_facial_directory_handles_unreachable_cloud_without_deleting_local_data(tmp_path, monkeypatch, edge_db_path):
    import json as json_module

    credential_file = tmp_path / "credential.json"
    credential_file.write_text(json_module.dumps({"appliance_id": "appl-1", "credential": "secret"}))
    monkeypatch.setattr(facial_embedding_sync, "CREDENTIAL_FILE", credential_file)
    monkeypatch.setattr(facial_embedding_sync, "CLOUD_URL", "https://unreachable.invalid.example")
    monkeypatch.setattr(facial_embedding_sync, "_control_plane_get", lambda path: None)
    with override_target(sqlite_path=str(edge_db_path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (NOW,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-1','p1','C','c@example.test','active','real',?)", (NOW,))
        with connection() as db:
            facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
        result = facial_embedding_sync.sync_facial_directory()
        assert result == {"status": "fetch_failed"}
        with connection() as db:
            # A failed fetch must never delete the already-synced local
            # copy -- fail-safe, matching this module's own docstring.
            assert [p["display_name"] for p in facial_people.list_people(db, customer_id="cust-1")] == ["Alice"]
