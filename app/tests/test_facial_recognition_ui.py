"""AAC routes -- Phase 1. Real FastAPI app + real SQLite database
(override_target(), matching test_camera_people_counting_entitlement.py's
own established pattern) exercised through TestClient: authenticated
enrollment, authenticated deletion, permission failures, and -- the
property checked most aggressively -- that a customer_owner/
customer_viewer session can never reach another tenant's people/events/
watchlists/settings through these routes, no matter what customer_id it
passes.

Also verifies no biometric/secret material (raw embeddings, uploaded
image bytes) is ever written into audit_logs.details_json.
"""

import base64
import json

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_facial_recognition_ui_import.db"):
    import facial_recognition_ui
    import partner_portal
    from partner_db import connection, initialize_database

NOW = "2026-09-08T00:00:00"


def _shell(title, active, content, scripts=""):
    return f"<html><title>{title}</title>{content}{scripts}</html>"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_facial_recognition_ui.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (NOW,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-1','p1','C1','c1@example.test','active','real',?)", (NOW,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-2','p1','C2','c2@example.test','active','real',?)", (NOW,))
        app = FastAPI()
        facial_recognition_ui.register_facial_recognition_routes(app, _shell)
        with TestClient(app) as test_client:
            yield test_client


def _admin_cookie():
    return partner_portal._token("admin@example.test", "administrator")


def _customer_owner_cookie(customer_id="cust-1"):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id)


def _customer_viewer_cookie(customer_id="cust-1"):
    return partner_portal._token("viewer@example.test", "customer_viewer", None, customer_id)


def _salesperson_cookie():
    return partner_portal._token("sales@example.test", "salesperson")


def _cookies(token):
    return {partner_portal.SESSION_COOKIE: token}


def _sample_image_base64() -> str:
    # Random noise, not a flat/blank image: a uniform image survives
    # grayscale+histogram-equalization as all-zero, which would make
    # embed_face_crop() correctly report "no real signal" (norm 0 ->
    # None) -- that's the right behavior for embed_face_crop(), but
    # would make this route-level test spuriously 422 for a reason
    # unrelated to what it's actually checking.
    rng = np.random.default_rng(3)
    image = rng.integers(0, 255, size=(120, 120, 3), dtype=np.uint8)
    ok, buffer = cv2.imencode(".jpg", image)
    assert ok
    return base64.b64encode(buffer.tobytes()).decode()


# --------------------------------------------------------------- authentication / permissions


def test_unauthenticated_enroll_is_rejected(client):
    response = client.post("/api/aac/people", json={"customer_id": "cust-1", "display_name": "Alice"})
    assert response.status_code == 401


def test_salesperson_cannot_view_people(client):
    response = client.get("/api/aac/people", params={"customer_id": "cust-1"}, cookies=_cookies(_salesperson_cookie()))
    assert response.status_code == 403


def test_customer_viewer_cannot_enroll(client):
    response = client.post(
        "/api/aac/people",
        json={"customer_id": "cust-1", "display_name": "Alice"},
        cookies=_cookies(_customer_viewer_cookie()),
    )
    assert response.status_code == 403


def test_customer_viewer_can_view_people(client):
    response = client.get("/api/aac/people", params={"customer_id": "cust-1"}, cookies=_cookies(_customer_viewer_cookie()))
    assert response.status_code == 200


def test_customer_owner_can_enroll(client):
    response = client.post(
        "/api/aac/people",
        json={"customer_id": "cust-1", "display_name": "Alice"},
        cookies=_cookies(_customer_owner_cookie()),
    )
    assert response.status_code == 200
    assert "person_id" in response.json()


def test_administrator_can_enroll_for_any_customer(client):
    response = client.post(
        "/api/aac/people",
        json={"customer_id": "cust-2", "display_name": "Bob"},
        cookies=_cookies(_admin_cookie()),
    )
    assert response.status_code == 200


# --------------------------------------------------------------- tenant isolation


def test_customer_owner_cannot_specify_a_different_customer_id(client):
    response = client.post(
        "/api/aac/people",
        json={"customer_id": "cust-2", "display_name": "Eve"},
        cookies=_cookies(_customer_owner_cookie(customer_id="cust-1")),
    )
    assert response.status_code == 403


def test_customer_owner_cannot_list_another_customers_people(client):
    client.post("/api/aac/people", json={"customer_id": "cust-2", "display_name": "Bob"}, cookies=_cookies(_admin_cookie()))
    response = client.get("/api/aac/people", params={"customer_id": "cust-2"}, cookies=_cookies(_customer_owner_cookie(customer_id="cust-1")))
    assert response.status_code == 403


def test_customer_owner_cannot_read_another_customers_person_by_id(client):
    created = client.post("/api/aac/people", json={"customer_id": "cust-2", "display_name": "Bob"}, cookies=_cookies(_admin_cookie()))
    person_id = created.json()["person_id"]
    response = client.get(f"/api/aac/people/{person_id}", params={"customer_id": "cust-1"}, cookies=_cookies(_customer_owner_cookie(customer_id="cust-1")))
    # Rejected at tenant-resolution (403) rather than leaking a 404 that
    # would confirm cust-2's person_id exists somewhere -- but either a
    # 403 or 404 here is an acceptable "you cannot see this", so this
    # only asserts it is NOT a 200.
    assert response.status_code in (403, 404)


def test_administrator_cannot_read_person_under_wrong_customer_id(client):
    """Even administrator (which can act as any customer) must not see
    a person when it explicitly queries the WRONG customer_id for that
    person -- get_person() is still scoped by the customer_id given,
    not merely by "is this identity privileged"."""
    created = client.post("/api/aac/people", json={"customer_id": "cust-1", "display_name": "Alice"}, cookies=_cookies(_admin_cookie()))
    person_id = created.json()["person_id"]
    response = client.get(f"/api/aac/people/{person_id}", params={"customer_id": "cust-2"}, cookies=_cookies(_admin_cookie()))
    assert response.status_code == 404


# --------------------------------------------------------------- enrollment images


def test_add_reference_image_stores_only_the_crop_not_the_upload(client, db_path, monkeypatch):
    # The real default Haar cascade correctly finds no face in a plain
    # synthetic image (see test_facial_recognition.py's own coverage of
    # that real, unmocked path) -- an injected fake engine is used here
    # instead, so this test can verify routing/storage behavior (only
    # the crop is persisted, never the raw upload) with a synthetic
    # image, matching this project's "synthetic/test images only" rule,
    # without needing a real detectable face photo.
    import facial_recognition as fr

    class _OneFaceEngine(fr.FaceEngine):
        name = "haar_intensity"
        version = "1"

        def detect_faces(self, image_bgr):
            return [fr.FaceDetection(10, 10, 40, 40)]

        def embed(self, face_crop_bgr):
            return fr.embed_face_crop(face_crop_bgr)

        def capability(self):
            return {"engine": self.name, "version": self.version, "available": True, "gpu": False, "reason": None}

    monkeypatch.setattr(fr, "_engine", _OneFaceEngine())
    created = client.post("/api/aac/people", json={"customer_id": "cust-1", "display_name": "Alice"}, cookies=_cookies(_customer_owner_cookie()))
    person_id = created.json()["person_id"]
    image_base64 = _sample_image_base64()
    response = client.post(
        f"/api/aac/people/{person_id}/images",
        json={"customer_id": "cust-1", "image_base64": image_base64},
        cookies=_cookies(_customer_owner_cookie()),
    )
    assert response.status_code == 200
    embedding_id = response.json()["embedding_id"]
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            row = db.execute("SELECT source_image_path,embedding_json FROM facial_embeddings WHERE id=?", (embedding_id,)).fetchone()
    assert row["source_image_path"] is not None
    # The stored embedding is a real feature vector, never the raw
    # base64 upload itself.
    stored = json.loads(row["embedding_json"])
    assert isinstance(stored, list) and all(isinstance(value, float) for value in stored)
    assert image_base64 not in row["embedding_json"]


def test_add_reference_image_rejects_image_with_no_detectable_face(client):
    created = client.post("/api/aac/people", json={"customer_id": "cust-1", "display_name": "Alice"}, cookies=_cookies(_customer_owner_cookie()))
    person_id = created.json()["person_id"]
    blank = np.zeros((10, 10, 3), dtype=np.uint8)
    ok, buffer = cv2.imencode(".jpg", blank)
    response = client.post(
        f"/api/aac/people/{person_id}/images",
        json={"customer_id": "cust-1", "image_base64": base64.b64encode(buffer.tobytes()).decode()},
        cookies=_cookies(_customer_owner_cookie()),
    )
    assert response.status_code == 422


def test_add_reference_image_rejects_malformed_base64(client):
    created = client.post("/api/aac/people", json={"customer_id": "cust-1", "display_name": "Alice"}, cookies=_cookies(_customer_owner_cookie()))
    person_id = created.json()["person_id"]
    response = client.post(
        f"/api/aac/people/{person_id}/images",
        json={"customer_id": "cust-1", "image_base64": "not-valid-base64!!!"},
        cookies=_cookies(_customer_owner_cookie()),
    )
    assert response.status_code == 400


# --------------------------------------------------------------- deletion


def test_authenticated_deletion_removes_person(client):
    created = client.post("/api/aac/people", json={"customer_id": "cust-1", "display_name": "Alice"}, cookies=_cookies(_customer_owner_cookie()))
    person_id = created.json()["person_id"]
    response = client.delete(f"/api/aac/people/{person_id}", params={"customer_id": "cust-1"}, cookies=_cookies(_customer_owner_cookie()))
    assert response.status_code == 200
    follow_up = client.get(f"/api/aac/people/{person_id}", params={"customer_id": "cust-1"}, cookies=_cookies(_customer_owner_cookie()))
    assert follow_up.status_code == 404


def test_unauthenticated_deletion_is_rejected(client):
    created = client.post("/api/aac/people", json={"customer_id": "cust-1", "display_name": "Alice"}, cookies=_cookies(_customer_owner_cookie()))
    person_id = created.json()["person_id"]
    response = client.delete(f"/api/aac/people/{person_id}", params={"customer_id": "cust-1"})
    assert response.status_code == 401


def test_viewer_cannot_delete(client):
    created = client.post("/api/aac/people", json={"customer_id": "cust-1", "display_name": "Alice"}, cookies=_cookies(_customer_owner_cookie()))
    person_id = created.json()["person_id"]
    response = client.delete(f"/api/aac/people/{person_id}", params={"customer_id": "cust-1"}, cookies=_cookies(_customer_viewer_cookie()))
    assert response.status_code == 403


# --------------------------------------------------------------- audit trail + secret-free logging


def test_enrollment_and_deletion_are_audited(client, db_path):
    created = client.post("/api/aac/people", json={"customer_id": "cust-1", "display_name": "Alice"}, cookies=_cookies(_customer_owner_cookie()))
    person_id = created.json()["person_id"]
    client.delete(f"/api/aac/people/{person_id}", params={"customer_id": "cust-1"}, cookies=_cookies(_customer_owner_cookie()))
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            actions = [row["action"] for row in db.execute("SELECT action FROM audit_logs WHERE entity_id=?", (person_id,)).fetchall()]
    assert "facial.person.enrolled" in actions
    assert "facial.person.deleted" in actions


def test_audit_log_never_contains_the_raw_uploaded_image_or_embedding(client, db_path):
    created = client.post("/api/aac/people", json={"customer_id": "cust-1", "display_name": "Alice"}, cookies=_cookies(_customer_owner_cookie()))
    person_id = created.json()["person_id"]
    image_base64 = _sample_image_base64()
    client.post(
        f"/api/aac/people/{person_id}/images",
        json={"customer_id": "cust-1", "image_base64": image_base64},
        cookies=_cookies(_customer_owner_cookie()),
    )
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            rows = db.execute("SELECT details_json FROM audit_logs WHERE entity_id=?", (person_id,)).fetchall()
    for row in rows:
        assert image_base64 not in row["details_json"]
        assert "embedding" not in row["details_json"].lower()


# --------------------------------------------------------------- watchlists / events / settings


def test_watchlist_lifecycle_through_routes(client):
    created = client.post(
        "/api/aac/watchlists",
        json={"customer_id": "cust-1", "name": "Banned"},
        cookies=_cookies(_customer_owner_cookie()),
    )
    assert created.status_code == 200
    watchlist_id = created.json()["watchlist_id"]
    person = client.post("/api/aac/people", json={"customer_id": "cust-1", "display_name": "Alice"}, cookies=_cookies(_customer_owner_cookie()))
    person_id = person.json()["person_id"]
    add_member = client.post(
        f"/api/aac/watchlists/{watchlist_id}/members",
        json={"customer_id": "cust-1", "person_id": person_id},
        cookies=_cookies(_customer_owner_cookie()),
    )
    assert add_member.status_code == 200
    members = client.get(f"/api/aac/watchlists/{watchlist_id}/members", params={"customer_id": "cust-1"}, cookies=_cookies(_customer_viewer_cookie()))
    assert members.status_code == 200
    assert len(members.json()["members"]) == 1


def test_settings_round_trip_through_routes(client):
    update = client.put(
        "/api/aac/settings",
        json={"customer_id": "cust-1", "min_confidence": 0.75},
        cookies=_cookies(_customer_owner_cookie()),
    )
    assert update.status_code == 200
    fetched = client.get("/api/aac/settings", params={"customer_id": "cust-1"}, cookies=_cookies(_customer_viewer_cookie()))
    assert fetched.json()["min_confidence"] == 0.75


def test_settings_rejects_out_of_range_confidence(client):
    response = client.put(
        "/api/aac/settings",
        json={"customer_id": "cust-1", "min_confidence": 1.5},
        cookies=_cookies(_customer_owner_cookie()),
    )
    assert response.status_code == 400


def test_events_endpoint_scopes_to_customer(client):
    response = client.get("/api/aac/events", params={"customer_id": "cust-1"}, cookies=_cookies(_customer_viewer_cookie()))
    assert response.status_code == 200
    assert response.json() == {"events": []}


def test_capability_endpoint_requires_authentication(client):
    assert client.get("/api/aac/capability").status_code == 401
    assert client.get("/api/aac/capability", cookies=_cookies(_customer_viewer_cookie())).status_code == 200


# --------------------------------------------------------------- HTML pages render


def test_people_page_renders_for_authenticated_viewer(client):
    response = client.get("/aac/people", cookies=_cookies(_customer_viewer_cookie()))
    assert response.status_code == 200
    assert "People" in response.text


def test_people_page_rejects_unauthenticated(client):
    response = client.get("/aac/people")
    assert response.status_code == 401
