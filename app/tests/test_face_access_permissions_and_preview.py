"""Face Recognition / Face Access fix-before-launch items (2026-10-02, Codex
audit; owner policies approved 2026-10-02), tested through the real API.

- Backup Access least privilege: a household member with only Backup
  Access reaches the people list and a person's profile (name, status) for
  Backup Access -- never FR events, match detail, face thumbnails,
  watchlists, settings, reference images or Face Access details.
- Entitlement: every customer Face Access / biometric API refuses an
  account without the add-on, and the database is left unchanged.
- Hybrid face preview: no cloud face crops; match detail says plainly that
  the preview is only on the local appliance, and never sends a file path.
"""
import base64
import json
import sqlite3

import pytest

import facial_events
import facial_people
from database_backend import override_target
from partner_db import connection
from test_facial_recognition_ui import (  # noqa: F401 -- fixtures and helpers
    NOW, _cookies, _customer_owner_cookie, _customer_viewer_cookie, _grant_face_access, _sample_image_base64, client, db_path,
)


def _person(db_path, customer_id="cust-1", name="Alice"):
    with override_target(sqlite_path=str(db_path)), connection() as db:
        person = facial_people.enroll_person(db, customer_id=customer_id, display_name=name, now=NOW, external_reference="LEASE-7",
                                             notes="private note")
        facial_people.add_reference_image(db, customer_id=customer_id, person_id=person, embedding=(1.0, 0.0), engine="onnx_yunet_arcface",
                                          engine_version="1", now=NOW)
        watchlist = facial_people.create_watchlist(db, customer_id=customer_id, name="Staff", now=NOW)
        facial_people.add_watchlist_member(db, customer_id=customer_id, watchlist_id=watchlist, person_id=person, now=NOW)
    return person, watchlist


def _member(db_path, *, people=0, face_access=0, backup_access=0, email="viewer@example.test"):
    with override_target(sqlite_path=str(db_path)), connection() as db:
        db.execute("INSERT OR REPLACE INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status) "
                   "VALUES('u-member','p1',?,'Member','customer_viewer','x',1,'cust-1',?,'active')", (email, NOW))
        db.execute("INSERT OR REPLACE INTO customer_user_permissions(user_id,customer_id,can_people,can_face_access,can_backup_access,updated_at) "
                   "VALUES('u-member','cust-1',?,?,?,?)", (people, face_access, backup_access, NOW))


def _event(db_path, *, thumbnail_path=None, customer_id="cust-1"):
    with override_target(sqlite_path=str(db_path)), connection() as db:
        db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1',?,'Home',?)", (customer_id, NOW))
        db.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1',?,'site-1','AIC-1',?)", (customer_id, NOW))
        db.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,name,status,camera_number,created_at) "
                   "VALUES('cam-1',?,'site-1','appl-1','Door','active',1,?)", (customer_id, NOW))
        event = facial_events.create_match_event(db, customer_id=customer_id, site_id="site-1", camera_id="cam-1", appliance_id="appl-1",
                                                 match_state="unknown", matched_person=None, matched_watchlist=None, confidence=0.4,
                                                 engine="onnx_yunet_arcface", engine_version="1",
                                                 face_bbox={"x": 0, "y": 0, "width": 10, "height": 10},
                                                 face_thumbnail_path=thumbnail_path, now=NOW)
    return event["id"]


VIEWER = _cookies(_customer_viewer_cookie())
OWNER = _cookies(_customer_owner_cookie())


# ================================================================ Backup Access least privilege

def test_backup_access_only_member_sees_only_names_for_backup_access(client, db_path):
    _grant_face_access()
    person, watchlist = _person(db_path)
    event_id = _event(db_path)
    _member(db_path, backup_access=1)
    listed = client.get("/api/aac/people", cookies=VIEWER)
    assert listed.status_code == 200 and listed.json()["people"] == [{"id": person, "display_name": "Alice", "status": "active"}]
    profile = client.get(f"/api/aac/people/{person}", cookies=VIEWER).json()
    assert profile == {"id": person, "display_name": "Alice", "status": "active"}  # no images, watchlists, reference, notes
    for url in ("/api/aac/events", f"/api/aac/events/{event_id}", f"/api/aac/events/{event_id}/thumbnail", "/api/aac/watchlists",
                f"/api/aac/watchlists/{watchlist}/members", "/api/aac/settings", f"/api/aac/people/{person}/access"):
        response = client.get(url, cookies=VIEWER)
        assert response.status_code == 403, (url, response.status_code)
    for page in ("/aac/events", f"/aac/events/{event_id}", "/aac/watchlists", "/aac/settings"):
        assert client.get(page, cookies=VIEWER).status_code == 403, page
    page = client.get(f"/aac/people/enroll?person={person}", cookies=VIEWER).text
    assert 'id="e-photo-panel" hidden' in page and 'id="a-panel" hidden' in page and "const CAN_VIEW_DETAILS=false" in page


def test_people_or_face_access_members_keep_full_people_access(client, db_path):
    _grant_face_access()
    person, _ = _person(db_path)
    for grant in ({"people": 1}, {"face_access": 1}):
        _member(db_path, **grant)
        profile = client.get(f"/api/aac/people/{person}", cookies=VIEWER).json()
        assert profile["external_reference"] == "LEASE-7" and len(profile["reference_images"]) == 1 and profile["watchlists"], grant
        assert client.get("/api/aac/events", cookies=VIEWER).status_code == 200, grant


def test_a_member_with_no_people_grant_gets_nothing(client, db_path):
    _grant_face_access()
    person, _ = _person(db_path)
    _member(db_path)
    assert client.get("/api/aac/people", cookies=VIEWER).status_code == 403
    assert client.get(f"/api/aac/people/{person}", cookies=VIEWER).status_code == 403


# ================================================================ entitlement gate

def _counts(db_path):
    with override_target(sqlite_path=str(db_path)), connection() as db:
        return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("facial_people", "facial_embeddings", "facial_watchlists", "facial_watchlist_members", "facial_settings",
                              "facial_rules", "audit_logs")}


def _settings_row(db_path):
    with override_target(sqlite_path=str(db_path)), connection() as db:
        found = db.execute("SELECT * FROM facial_settings WHERE customer_id='cust-1'").fetchone()
    return dict(found) if found else None


def test_without_the_add_on_every_face_access_api_is_refused_and_nothing_changes(client, db_path):
    person, watchlist = _person(db_path)  # data left from an earlier subscription
    event_id = _event(db_path)
    with override_target(sqlite_path=str(db_path)), connection() as db:
        embedding = db.execute("SELECT id FROM facial_embeddings LIMIT 1").fetchone()[0]
    before, settings_before = _counts(db_path), _settings_row(db_path)
    image = {"image_base64": _sample_image_base64()}
    requests = [
        ("post", "/api/aac/people", {"display_name": "Mallory"}),
        ("patch", f"/api/aac/people/{person}", {"display_name": "Renamed"}),
        ("post", f"/api/aac/people/{person}/images", image),
        ("delete", f"/api/aac/people/{person}/images/{embedding}", None),
        ("post", "/api/aac/watchlists", {"name": "VIP"}),
        ("delete", f"/api/aac/watchlists/{watchlist}", None),
        ("post", f"/api/aac/watchlists/{watchlist}/members", {"person_id": person}),
        ("delete", f"/api/aac/watchlists/{watchlist}/members/{person}", None),
        ("put", "/api/aac/settings", {"min_confidence": 0.9}),
        ("put", f"/api/aac/people/{person}/access", {"access_enabled": True}),
        ("post", "/api/aac/face-preview", image),
        ("get", "/api/aac/people", None), ("get", f"/api/aac/people/{person}", None), ("get", f"/api/aac/people/{person}/access", None),
        ("get", "/api/aac/watchlists", None), ("get", f"/api/aac/watchlists/{watchlist}/members", None),
        ("get", "/api/aac/events", None), ("get", f"/api/aac/events/{event_id}", None), ("get", f"/api/aac/events/{event_id}/thumbnail", None),
        ("get", "/api/aac/settings", None),
    ]
    for method, url, body in requests:
        call = getattr(client, method)
        response = call(url, cookies=OWNER, json=body) if body is not None else call(url, cookies=OWNER)
        assert response.status_code == 403 and "not active" in response.json()["detail"], (method, url, response.status_code)
    assert _counts(db_path) == before and _settings_row(db_path) == settings_before
    with override_target(sqlite_path=str(db_path)), connection() as db:
        assert db.execute("SELECT display_name FROM facial_people WHERE id=?", (person,)).fetchone()[0] == "Alice"


def test_a_lapsed_customer_can_still_erase_a_person(client, db_path):
    """Deleting a person (and with it their face templates) is erasure, not
    use of the licensed feature: it stays available without the add-on."""
    person, _ = _person(db_path)
    assert client.delete(f"/api/aac/people/{person}", cookies=OWNER).status_code == 200
    with override_target(sqlite_path=str(db_path)), connection() as db:
        assert db.execute("SELECT COUNT(*) FROM facial_embeddings WHERE person_id=?", (person,)).fetchone()[0] == 0


def test_the_add_on_restores_access_without_cross_tenant_reach(client, db_path):
    _grant_face_access()
    _person(db_path, customer_id="cust-2", name="Other tenant")
    assert client.post("/api/aac/people", cookies=OWNER, json={"display_name": "Bob"}).status_code == 200
    names = [p["display_name"] for p in client.get("/api/aac/people", cookies=OWNER).json()["people"]]
    assert names == ["Bob"]
    foreign = client.get("/api/aac/people?customer_id=cust-2", cookies=OWNER)  # naming another tenant
    assert foreign.status_code in (403, 404) or [p["display_name"] for p in foreign.json()["people"]] == ["Bob"]
    assert "Other tenant" not in foreign.text


# ================================================================ Hybrid face preview

def test_cloud_match_detail_says_the_preview_is_local_only(client, db_path, monkeypatch):
    import facial_recognition_ui
    _grant_face_access()
    monkeypatch.setattr(facial_recognition_ui, "_runtime_role", lambda: "cloud")
    event_id = _event(db_path, thumbnail_path=None)
    detail = client.get(f"/api/aac/events/{event_id}", cookies=OWNER).json()
    assert detail["face_preview_available"] is False
    assert detail["face_preview_note"] == "Face preview is available only on the local appliance."
    assert "face_thumbnail_path" not in detail
    page = client.get(f"/aac/events/{event_id}", cookies=OWNER).text
    assert "e.face_preview_available" in page and "d-preview-note" in page and "onerror=" not in page


def test_local_match_detail_shows_the_local_crop(client, db_path, tmp_path, monkeypatch):
    import facial_recognition_ui
    _grant_face_access()
    monkeypatch.setattr(facial_recognition_ui, "_runtime_role", lambda: "edge")
    crop = tmp_path / "crop.jpg"
    crop.write_bytes(b"\xff\xd8jpeg")
    event_id = _event(db_path, thumbnail_path=str(crop))
    detail = client.get(f"/api/aac/events/{event_id}", cookies=OWNER).json()
    assert detail["face_preview_available"] is True and "face_thumbnail_path" not in detail
    assert client.get(f"/api/aac/events/{event_id}/thumbnail", cookies=OWNER).content == b"\xff\xd8jpeg"
    other = _cookies(_customer_owner_cookie("cust-2"))
    assert client.get(f"/api/aac/events/{event_id}", cookies=other).status_code in (403, 404)  # tenant isolation intact
    no_crop = _event(db_path, thumbnail_path=None)
    assert client.get(f"/api/aac/events/{no_crop}", cookies=OWNER).json()["face_preview_note"] == "No face preview was saved for this match."


def test_no_cloud_face_crop_upload_path_exists():
    from pathlib import Path
    source = (Path(facial_events.__file__).parent / "appliance_cloud.py").read_text(encoding="utf-8")
    route = source[source.index("/api/appliance/facial-events/{detection_event_id}/thumbnail"):]
    route = route[:route.index("@app.post", 10)]
    assert "status_code=410" in route and "get_storage" not in route and ".put(" not in route
