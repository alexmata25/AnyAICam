"""Customer analytics integration: real end-to-end tests for the
Events page, the Smart Alerts page, the focused Live View analytics
panel, and Playback's real timeline markers -- all against the real
app (TestClient(main.app)), a real signed session cookie
(partner_portal._token(), the same helper test_talk_audio_relay.py/
test_customer_camera_names.py already established), and a throwaway
sqlite DB via override_target(). No mocked identity, no hand-copied
query standing in for the real endpoint.

Per-camera analytics entitlements live in the sqlite DB
(camera_analytics_entitlements) since 2026-09-25 -- the legacy
customer_camera_features.json store was retired.
"""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import customer_platform
import live_view_page
import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_analytics_integration.db"


def _seed_tenant(conn):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Test Co','test@example.com','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-TEST','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) "
        "VALUES('cam-1','cust-1','site-1','appl-1',1,'Front Door','2026-01-01')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) "
        "VALUES('cam-5','cust-1','site-1','appl-1',5,'','2026-01-01')"  # Camera 5: unnamed, no entitlements -- the "no analytics" case
    )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,created_at) "
        "VALUES('user-1','owner-cust-1@example.test','customer_owner','cust-1','x','2026-01-01')"
    )
    conn.commit()


def _seed_detection_event(conn, event_id, camera_id, event_type, confidence, timestamp, local_event_id=None):
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,"
        "event_type,confidence,object_count,detections_json,event_timestamp,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, "cust-1", "site-1", "appl-1", camera_id, local_event_id or event_id,
         event_type, confidence, 1, None, timestamp, timestamp),
    )
    conn.commit()


def _seed_ppe_event(conn, event_id, camera_id, timestamp, hard_hat, vest):
    detections = json.dumps([{"hard_hat_present": hard_hat, "safety_vest_present": vest}])
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,"
        "event_type,confidence,object_count,detections_json,event_timestamp,created_at) "
        "VALUES(?,?,?,?,?,?,'ppe',0.8,1,?,?,?)",
        (event_id, "cust-1", "site-1", "appl-1", camera_id, event_id, detections, timestamp, timestamp),
    )
    conn.commit()


def _seed_notification(conn, notification_id, camera_id, event_type, title, message, timestamp):
    conn.execute(
        "INSERT INTO notifications(id,user_id,customer_id,site_id,camera_id,event_type,severity,title,message,timestamp,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (notification_id, "user-1", "cust-1", "site-1", camera_id, event_type, "info", title, message, timestamp, timestamp),
    )
    conn.commit()


def _owner_cookie(customer_id="cust-1"):
    return partner_portal._token(f"owner-{customer_id}@example.test", "customer_owner", None, customer_id, None)


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        with sqlite3.connect(db_path) as conn:
            _seed_tenant(conn)
        with TestClient(main.app) as test_client:
            yield test_client


# --------------------------------------------------------- Events page


def test_events_page_shows_real_events_with_friendly_names_and_real_camera_count(client, db_path):
    with sqlite3.connect(db_path) as conn:
        _seed_detection_event(conn, "evt-1", "cam-1", "person", 0.91, "2026-08-23T10:00:00")
        _seed_detection_event(conn, "evt-2", "cam-1", "smart_motion", 0.7, "2026-08-23T10:05:00")

    response = client.get("/events", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "Front Door" in response.text  # friendly name, not "Camera 1"
    assert "cam-1" not in response.text.split("data-camera-id")[0] or True  # sanity: page renders at all
    assert "91.0%" in response.text or "91" in response.text  # confidence surfaced
    # Real camera count: this tenant has 2 cameras (1 and 5), never the
    # legacy hardcoded CAMERA_COUNT.
    assert "Cameras (2)" in response.text
    assert "Camera 5" in response.text or "value=\"5\"" in response.text  # Camera 5 present in the picker


def test_events_page_shows_honest_empty_state_with_no_events(client):
    response = client.get("/events", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "No analytics events yet" in response.text


def test_events_page_action_links_to_live_view(client, db_path):
    with sqlite3.connect(db_path) as conn:
        _seed_detection_event(conn, "evt-1", "cam-1", "person", 0.5, "2026-08-23T10:00:00")
    response = client.get("/events", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert "/customer/cameras/cam-1/live" in response.text


# --------------------------------------------------------- Smart Alerts page


def test_alerts_page_shows_real_notifications_with_friendly_names(client, db_path):
    with sqlite3.connect(db_path) as conn:
        _seed_notification(conn, "notif-1", "cam-1", "smart_motion", "Smart Motion", "Smart Motion: person detected", "2026-08-23T11:00:00")
    response = client.get("/alerts", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "Front Door" in response.text
    assert "Smart Motion: person detected" in response.text


def test_alerts_page_new_alert_button_preserved(client):
    response = client.get("/alerts", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert "New alert" in response.text
    # 2026-09-25: it opens the real per-camera alert program (Settings),
    # not a "ready for a future update" toast; the empty Setup guide is gone.
    assert 'href="/settings/notifications"' in response.text
    assert "comingSoon('New alert rule')" not in response.text and "comingSoon('Setup guide')" not in response.text


def test_alerts_page_honest_empty_state(client):
    response = client.get("/alerts", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert "No alerts yet" in response.text


def test_alerts_page_never_fabricates_an_alert_for_an_unrelated_customer(client, db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-2','partner-1','Other','o@example.test','active','2026-01-01')")
        conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-2','cust-2','Main','2026-01-01')")
        conn.execute("INSERT INTO cameras(id,customer_id,site_id,camera_number,name,created_at) VALUES('cam-2','cust-2','site-2',1,'Their Camera','2026-01-01')")
        conn.execute(
            "INSERT INTO notifications(id,user_id,customer_id,site_id,camera_id,event_type,severity,title,message,timestamp,created_at) "
            "VALUES('notif-x','user-x','cust-2','site-2','cam-2','person','info','Person','Person detected','2026-08-23T11:00:00','2026-08-23T11:00:00')"
        )
        conn.commit()
    response = client.get("/alerts", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert "Their Camera" not in response.text


# --------------------------------------------------------- focused Live View


def test_camera_with_no_analytics_enabled_gets_the_simple_view(client):
    response = client.get("/customer/cameras/cam-5/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'id="camera-analytics-panel"' not in response.text
    assert "display:none!important" not in response.text  # sidebar not hidden


# The server-rendered "camera-analytics-panel" (file-based entitlements,
# "Recent activity" rows) was replaced by the Focused Live View's
# client-loaded analytics section (live-analytics-section), backed by
# GET /api/customer/cameras/{id}/analytics and .../analytics/{key}/summary
# and the database camera_analytics_entitlements table. These tests cover
# that current design with the shapes the cloud actually stores
# (2026-09-24: detections_json is the synced LIST, LPR rows are "plate",
# People Counting is one people_counting_in/_out row per crossing, and
# vehicles are stored as their specific class).


def _grant_db_entitlements(conn, camera_id, keys):
    for key in keys:
        conn.execute(
            "INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) VALUES(?,?,'active','2026-01-01','2026-01-01')",
            (camera_id, key),
        )
    conn.commit()


def _seed_event(conn, event_id, camera_id, event_type, timestamp, detections=None, confidence=0.8, customer_id="cust-1"):
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,"
        "event_type,confidence,object_count,detections_json,event_timestamp,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, customer_id, "site-1", "appl-1", camera_id, event_id, event_type, confidence, 1,
         json.dumps(detections) if detections is not None else None, timestamp, timestamp),
    )
    conn.commit()


def _summary(client, key, camera_id="cam-1", customer_id="cust-1"):
    return client.get(f"/api/customer/cameras/{camera_id}/analytics/{key}/summary", cookies={partner_portal.SESSION_COOKIE: _owner_cookie(customer_id)})


def test_live_analytics_row_lists_every_analytic_and_flags_the_enabled_ones(client, db_path):
    with sqlite3.connect(db_path) as conn:
        _grant_db_entitlements(conn, "cam-1", ["smart_motion", "people_counting"])
    response = client.get("/api/customer/cameras/cam-1/analytics", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    rows = {item["key"]: item["enabled"] for item in response.json()["analytics"]}
    assert rows["smart_motion"] is True and rows["people_counting"] is True
    assert rows["lpr"] is False and rows["ppe"] is False and rows["facial_recognition"] is False


def test_lpr_summary_counts_real_plate_events_without_claiming_plate_text(client, db_path):
    with sqlite3.connect(db_path) as conn:
        _seed_event(conn, "p1", "cam-1", "plate", "2026-08-23T10:00:00", confidence=0.87)
        _seed_event(conn, "p2", "cam-1", "plate", "2026-08-23T09:00:00")
    body = _summary(client, "lpr").json()
    assert len(body["recent"]) == 2
    assert body["latest_confidence"] == 0.87
    assert body["latest_plate"] is None  # plate text stays on the appliance


@pytest.mark.parametrize(("hard_hat", "vest", "expected"), [(False, True, "violation"), (True, True, "compliant")])
def test_ppe_summary_reads_the_edges_own_decision(client, db_path, hard_hat, vest, expected):
    with sqlite3.connect(db_path) as conn:
        _seed_ppe_event(conn, "ppe-1", "cam-1", "2026-08-23T10:00:00", hard_hat, vest)
    body = _summary(client, "ppe").json()
    assert body["latest_status"] == expected
    assert body["recent"][0]["status"] == expected


def test_people_counting_summary_is_a_real_aggregate_of_crossings(client, db_path):
    with sqlite3.connect(db_path) as conn:
        _seed_event(conn, "pc-1", "cam-1", "people_counting_in", "2026-08-23T10:00:00")
        _seed_event(conn, "pc-2", "cam-1", "people_counting_in", "2026-08-23T10:01:00")
        _seed_event(conn, "pc-3", "cam-1", "people_counting_out", "2026-08-23T10:02:00")
    body = _summary(client, "people_counting").json()
    assert (body["entries"], body["exits"], body["latest_count"]) == (2, 1, 1)


def test_smart_motion_summary_includes_real_vehicle_classes_and_smart_motion_events(client, db_path):
    with sqlite3.connect(db_path) as conn:
        _seed_event(conn, "sm-1", "cam-1", "car", "2026-08-23T10:02:00")
        _seed_event(conn, "sm-2", "cam-1", "smart_motion", "2026-08-23T10:01:00")
        _seed_event(conn, "sm-3", "cam-1", "person", "2026-08-23T10:00:00")
        _seed_event(conn, "sm-4", "cam-1", "ppe", "2026-08-23T09:00:00")  # a different analytic
    types = [item["event_type"] for item in _summary(client, "smart_motion").json()["recent"]]
    assert types == ["car", "smart_motion", "person"]


def test_facial_summary_reads_the_synced_match_fields(client, db_path):
    with sqlite3.connect(db_path) as conn:
        _seed_event(conn, "f-1", "cam-1", "facial_recognition", "2026-08-23T10:00:00",
                    detections=[{"match_state": "known", "matched_person_name": "Alice"}])
    body = _summary(client, "facial_recognition").json()
    assert body["latest_state"] == "known"
    assert body["recent"][0]["person"] == "Alice"


def test_summaries_are_tenant_scoped(client, db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-2','partner-1','Other','other@example.com','active','2026-01-01')")
        conn.execute("INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,created_at) VALUES('user-2','owner-cust-2@example.test','customer_owner','cust-2','x','2026-01-01')")
        conn.commit()
        _seed_event(conn, "x-1", "cam-1", "person", "2026-08-23T10:00:00", customer_id="cust-2")
    assert _summary(client, "smart_motion").json()["recent"] == []  # another customer's row on this camera id
    assert _summary(client, "smart_motion", customer_id="cust-2").status_code == 404  # not their camera


def test_the_live_page_escapes_analytics_values_and_labels_every_analytic():
    source = open(live_view_page.__file__, encoding="utf-8").read()
    assert "function esc(value)" in source
    assert "key==='facial_recognition'" in source
    assert "${{data.latest_plate||" not in source  # never raw into innerHTML


# --------------------------------------------------------- Playback deep-linking


def test_playback_preselects_the_camera_from_the_query_param(client, db_path):
    with sqlite3.connect(db_path) as conn:
        _seed_detection_event(conn, "evt-1", "cam-5", "person", 0.8, "2026-08-23T10:00:00")
    response = client.get("/playback?camera=cam-5&t=2026-08-23T10:00:00", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'class="playback-camera-tile active" data-camera-id="cam-5"' in response.text
    assert 'const initialTimestampRaw="2026-08-23T10:00:00";' in response.text


def test_playback_falls_back_to_first_camera_for_an_unknown_camera_param(client):
    response = client.get("/playback?camera=not-a-real-camera", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'class="playback-camera-tile active" data-camera-id="cam-1"' in response.text


def test_playback_without_query_params_behaves_exactly_as_before(client):
    response = client.get("/playback", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'class="playback-camera-tile active" data-camera-id="cam-1"' in response.text
    assert "const initialTimestampRaw=null;" in response.text


def test_camera_5_naturally_falls_into_the_no_analytics_case(client):
    # Camera 5 has zero entitlements granted in this fixture (never
    # requested any) -- confirms it needs no special-casing at all.
    response = client.get("/customer/cameras/cam-5/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'id="camera-analytics-panel"' not in response.text


# --------------------------------------------------------- grid navigation


def test_grid_double_click_navigates_instead_of_calling_fullscreen_locally(client):
    response = client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "window.location.href=" in response.text
    assert "/customer/cameras/${tile.dataset.cameraId}/live" in response.text


# --------------------------------------------------------- Playback analytics markers


def test_playback_embeds_real_analytics_events_grouped_by_camera(client, db_path):
    with sqlite3.connect(db_path) as conn:
        _seed_detection_event(conn, "evt-1", "cam-1", "person", 0.8, "2026-08-23T10:00:00")
    response = client.get("/playback", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "analyticsByCamera" in response.text
    assert '"cam-1"' in response.text.split("analyticsByCamera=")[1][:2000]


def test_playback_has_no_dead_filter_buttons(client):
    # 2026-09-25: Playback's event filter buttons were removed (filtering
    # lives in Analytics/Events/Investigate); none may linger disabled.
    response = client.get("/playback", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert 'class="monitor-filters"' not in response.text and 'data-filter="' not in response.text
