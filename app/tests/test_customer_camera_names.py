"""Customer-editable friendly camera names: reuses the real cameras.name
column, so these tests exercise the real UPDATE against a throwaway
sqlite DB (via override_target(), same isolation every other
app/tests/test_customer_*.py file already uses) plus the real HTTP
route through FastAPI's TestClient with a real, validly-signed session
cookie (partner_portal._token(), the exact helper test_talk_audio_relay.py
already established for this same purpose) -- not a mocked identity,
not a hand-copied SQL statement standing in for the real endpoint.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import live_view_page
import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_camera_names.db"


def _seed_tenant(conn):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Test Co','test@example.com','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-TEST','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) "
        "VALUES('cam-1','cust-1','site-1','appl-1',1,'Camera 1','2026-01-01')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,created_at) "
        "VALUES('user-1','owner-cust-1@example.test','customer_owner','cust-1','x','2026-01-01')"
    )
    conn.commit()


def _owner_cookie(customer_id="cust-1"):
    return partner_portal._token(f"owner-{customer_id}@example.test", "customer_owner", None, customer_id, None)


def _viewer_cookie(customer_id="cust-1"):
    return partner_portal._token(f"viewer-{customer_id}@example.test", "customer_viewer", None, customer_id, None)


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        with sqlite3.connect(db_path) as conn:
            _seed_tenant(conn)
        with TestClient(main.app) as test_client:
            yield test_client


def test_rename_persists_and_is_readable_after(client, db_path):
    response = client.put(
        "/api/customer/cameras/1/name",
        json={"name": "Front Door"},
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie()},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Front Door"
    assert body["display_name"] == "Front Door"

    # Simulates "persists after refresh/login": a fresh read of the
    # real row, not the same request's own return value.
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT name, camera_number FROM cameras WHERE id='cam-1'").fetchone()
    assert row["name"] == "Front Door"
    assert row["camera_number"] == 1  # camera_number is never touched by a rename


def test_empty_name_clears_back_to_fallback(client, db_path):
    client.put("/api/customer/cameras/1/name", json={"name": "Front Door"},
               cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    response = client.put("/api/customer/cameras/1/name", json={"name": "  "},
                          cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert response.json()["name"] == ""
    assert response.json()["display_name"] == "Camera 1"


def test_customer_viewer_cannot_rename(client):
    response = client.put(
        "/api/customer/cameras/1/name",
        json={"name": "Nope"},
        cookies={partner_portal.SESSION_COOKIE: _viewer_cookie()},
    )
    assert response.status_code == 403


def test_cannot_rename_a_camera_belonging_to_another_customer(client, db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-2','partner-1','Other Co','o@example.test','active','2026-01-01')"
        )
        conn.execute(
            "INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-2','cust-2','Main','2026-01-01')"
        )
        conn.execute(
            "INSERT INTO cameras(id,customer_id,site_id,camera_number,name,created_at) VALUES('cam-2','cust-2','site-2',1,'Camera 1','2026-01-01')"
        )
        conn.execute(
            "INSERT INTO partner_users(id,email,role,customer_id,password_hash,created_at) "
            "VALUES('user-2','owner-cust-2@example.test','customer_owner','cust-2','x','2026-01-01')"
        )
        conn.commit()

    response = client.put(
        "/api/customer/cameras/1/name",
        json={"name": "Stolen"},
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-2")},
    )
    # cust-2's own camera 1 (cam-2) is legitimately renameable...
    assert response.status_code == 200
    # ...but cust-1's camera 1 (cam-1) must be untouched by cust-2's request.
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cam1 = conn.execute("SELECT name FROM cameras WHERE id='cam-1'").fetchone()
    assert cam1["name"] == "Camera 1"


def test_name_over_60_characters_is_rejected(client):
    response = client.put(
        "/api/customer/cameras/1/name",
        json={"name": "x" * 61},
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie()},
    )
    assert response.status_code == 422


def test_rename_does_not_touch_stream_or_provisioning_fields(client, db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        before = dict(conn.execute("SELECT * FROM cameras WHERE id='cam-1'").fetchone())

    client.put("/api/customer/cameras/1/name", json={"name": "Driveway Right"},
               cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        after = dict(conn.execute("SELECT * FROM cameras WHERE id='cam-1'").fetchone())

    for key in before:
        if key == "name":
            continue
        assert before[key] == after[key], f"rename must not change cameras.{key}"
    assert after["name"] == "Driveway Right"


# --------------------------------------------------- display consistency

def test_live_view_grid_shows_renamed_camera(client):
    client.put("/api/customer/cameras/1/name", json={"name": "Front Door"},
               cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "Front Door" in response.text


def test_playback_camera_tile_falls_back_to_camera_number_not_raw_id(client, db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE cameras SET name='' WHERE id='cam-1'")
        conn.commit()
    response = client.get("/playback", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "Camera 1" in response.text
    # data-camera-id="cam-1" is expected (JS needs the real id); the
    # raw id must never leak into the *visible* button label though.
    assert ">cam-1<" not in response.text


def test_analytics_events_api_returns_camera_name(client, db_path):
    client.put("/api/customer/cameras/1/name", json={"name": "Front Door"},
               cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,"
            "event_type,confidence,object_count,detections_json,event_timestamp,created_at) "
            "VALUES('evt-1','cust-1','site-1','appl-1','cam-1','local-1','motion',0.9,1,NULL,"
            "'2026-08-23T12:00:00','2026-08-23T12:00:01')"
        )
        conn.commit()
    response = client.get("/api/analytics/events", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    events = response.json()["events"]
    assert len(events) == 1
    assert events[0]["camera_name"] == "Front Door"


# --------------------------------------------------- pure fallback helper

def test_camera_display_label_main_prefers_name_then_number_then_id():
    assert main._camera_display_label({"name": "Front Door", "camera_number": 1, "id": "cam-1"}) == "Front Door"
    assert main._camera_display_label({"name": "", "camera_number": 3, "id": "cam-3"}) == "Camera 3"
    assert main._camera_display_label({"name": None, "camera_number": None, "id": "cam-9"}) == "cam-9"


def test_camera_display_label_live_view_page_matches_main_behavior():
    assert live_view_page._camera_display_label({"name": "Driveway", "camera_number": 2, "id": "cam-2"}) == "Driveway"
    assert live_view_page._camera_display_label({"name": "", "camera_number": 4, "id": "cam-4"}) == "Camera 4"
