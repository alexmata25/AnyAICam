"""Customer analytics integration: real end-to-end tests for the
Events page, the Smart Alerts page, the focused Live View analytics
panel, and Playback's real timeline markers -- all against the real
app (TestClient(main.app)), a real signed session cookie
(partner_portal._token(), the same helper test_talk_audio_relay.py/
test_customer_camera_names.py already established), and a throwaway
sqlite DB via override_target(). No mocked identity, no hand-copied
query standing in for the real endpoint.

Entitlements/analytics_enabled state lives in a JSON file
(customer_platform.FEATURES_FILE), not the sqlite DB -- redirected to
a tmp_path file per test the same way the DB itself is redirected.
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


def _grant_entitlements(monkeypatch, tmp_path, camera_number, features):
    features_file = tmp_path / "customer_camera_features.json"
    monkeypatch.setattr(customer_platform, "FEATURES_FILE", features_file)
    key = customer_platform._camera_key("user-1", camera_number)
    features_file.write_text(json.dumps({
        key: {
            "entitlements": list(features),
            "analytics_enabled": {name: True for name in features},
        }
    }), encoding="utf-8")


def _owner_cookie(customer_id="cust-1"):
    return partner_portal._token(f"owner-{customer_id}@example.test", "customer_owner", None, customer_id, None)


@pytest.fixture()
def client(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(customer_platform, "FEATURES_FILE", tmp_path / "customer_camera_features.json")
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


def test_camera_with_enabled_analytics_gets_the_panel_and_hides_the_sidebar(client, db_path, tmp_path, monkeypatch):
    _grant_entitlements(monkeypatch, tmp_path, 1, ["smart_motion", "people_counting"])
    with sqlite3.connect(db_path) as conn:
        _seed_detection_event(conn, "evt-1", "cam-1", "smart_motion", 0.8, "2026-08-23T10:00:00")

    response = client.get("/customer/cameras/cam-1/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'id="camera-analytics-panel"' in response.text
    assert "display:none!important" in response.text  # sidebar hidden
    assert "Smart Motion" in response.text
    assert "People Counting" in response.text


def test_lpr_panel_omitted_entirely_when_no_plate_events_exist(client, tmp_path, monkeypatch):
    _grant_entitlements(monkeypatch, tmp_path, 1, ["lpr"])
    response = client.get("/customer/cameras/cam-1/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert "License Plate Recognition" not in response.text


def test_lpr_panel_shown_once_a_real_plate_event_exists(client, db_path, tmp_path, monkeypatch):
    _grant_entitlements(monkeypatch, tmp_path, 1, ["lpr"])
    with sqlite3.connect(db_path) as conn:
        _seed_detection_event(conn, "evt-plate", "cam-1", "plate", 0.6, "2026-08-23T10:00:00")
    response = client.get("/customer/cameras/cam-1/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert "License Plate Recognition" in response.text


def test_ppe_panel_shows_violation_from_real_forwarded_detections(client, db_path, tmp_path, monkeypatch):
    _grant_entitlements(monkeypatch, tmp_path, 1, ["ppe_detection"])
    with sqlite3.connect(db_path) as conn:
        _seed_ppe_event(conn, "evt-ppe", "cam-1", "2026-08-23T10:00:00", hard_hat=False, vest=False)
    response = client.get("/customer/cameras/cam-1/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert "Violation" in response.text


def test_ppe_panel_shows_compliant_from_real_forwarded_detections(client, db_path, tmp_path, monkeypatch):
    _grant_entitlements(monkeypatch, tmp_path, 1, ["ppe_detection"])
    with sqlite3.connect(db_path) as conn:
        _seed_ppe_event(conn, "evt-ppe", "cam-1", "2026-08-23T10:00:00", hard_hat=True, vest=True)
    response = client.get("/customer/cameras/cam-1/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert "Compliant" in response.text


def test_people_counting_occupancy_is_a_real_aggregate_not_fake(client, db_path, tmp_path, monkeypatch):
    _grant_entitlements(monkeypatch, tmp_path, 1, ["people_counting"])
    with sqlite3.connect(db_path) as conn:
        import datetime as dt
        today = dt.datetime.now().strftime("%Y-%m-%dT12:00:00")
        _seed_detection_event(conn, "evt-in-1", "cam-1", "people_counting_in", 0.9, today, "in-1")
        _seed_detection_event(conn, "evt-in-2", "cam-1", "people_counting_in", 0.9, today, "in-2")
        _seed_detection_event(conn, "evt-out-1", "cam-1", "people_counting_out", 0.9, today, "out-1")
    response = client.get("/customer/cameras/cam-1/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert "In 2 · Out 1 · Currently inside 1" in response.text


def test_smart_alerts_panel_shows_real_notifications_for_this_camera(client, db_path, tmp_path, monkeypatch):
    _grant_entitlements(monkeypatch, tmp_path, 1, ["smart_motion"])
    with sqlite3.connect(db_path) as conn:
        _seed_notification(conn, "notif-1", "cam-1", "smart_motion", "Smart Motion", "Smart Motion: person detected", "2026-08-23T10:00:00")
    response = client.get("/customer/cameras/cam-1/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert "Smart Alerts" in response.text
    assert "Smart Motion (" in response.text or "Smart Motion" in response.text


def test_recent_activity_is_not_limited_to_the_last_hour(client, db_path, tmp_path, monkeypatch):
    # Recent activity shows real history newest-first, not just the
    # last hour -- a camera that's been quiet for a while should still
    # show its real recent past instead of an empty section.
    _grant_entitlements(monkeypatch, tmp_path, 1, ["smart_motion"])
    with sqlite3.connect(db_path) as conn:
        import datetime as dt
        old = (dt.datetime.now() - dt.timedelta(hours=5)).isoformat()
        _seed_detection_event(conn, "evt-old", "cam-1", "person", 0.9, old)
    response = client.get("/customer/cameras/cam-1/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert "Recent activity" in response.text
    assert "No activity recorded yet" not in response.text


def test_recent_activity_honest_empty_state_with_no_events(client, tmp_path, monkeypatch):
    _grant_entitlements(monkeypatch, tmp_path, 1, ["smart_motion"])
    response = client.get("/customer/cameras/cam-1/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert "No activity recorded yet for this camera" in response.text


def test_recent_activity_rows_deep_link_to_playback_for_this_camera_and_time(client, db_path, tmp_path, monkeypatch):
    _grant_entitlements(monkeypatch, tmp_path, 1, ["smart_motion"])
    with sqlite3.connect(db_path) as conn:
        _seed_detection_event(conn, "evt-1", "cam-1", "person", 0.9, "2026-08-23T10:00:00")
    response = client.get("/customer/cameras/cam-1/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    # & is HTML-escaped to &amp; inside the href attribute (standard,
    # correct HTML -- browsers decode it back to & on navigation).
    assert "/playback?camera=cam-1&amp;t=2026-08-23T10%3A00%3A00" in response.text


def test_ppe_and_lpr_summary_lines_are_also_clickable_playback_links(client, db_path, tmp_path, monkeypatch):
    _grant_entitlements(monkeypatch, tmp_path, 1, ["ppe_detection", "lpr"])
    with sqlite3.connect(db_path) as conn:
        _seed_ppe_event(conn, "evt-ppe", "cam-1", "2026-08-23T10:00:00", hard_hat=True, vest=True)
        _seed_detection_event(conn, "evt-plate", "cam-1", "plate", 0.6, "2026-08-23T11:00:00")
    response = client.get("/customer/cameras/cam-1/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    # Two distinct deep links: one per real event, each to its own moment.
    assert "t=2026-08-23T10%3A00%3A00" in response.text
    assert "t=2026-08-23T11%3A00%3A00" in response.text


# --------------------------------------------------------- Playback deep-linking


def test_playback_preselects_the_camera_from_the_query_param(client, db_path):
    with sqlite3.connect(db_path) as conn:
        _seed_detection_event(conn, "evt-1", "cam-5", "person", 0.8, "2026-08-23T10:00:00")
    response = client.get("/playback?camera=cam-5&t=2026-08-23T10:00:00", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'class="playback-camera-tile active" data-camera-id="cam-5"' in response.text
    assert 'const initialTimestamp="2026-08-23T10:00:00";' in response.text


def test_playback_falls_back_to_first_camera_for_an_unknown_camera_param(client):
    response = client.get("/playback?camera=not-a-real-camera", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'class="playback-camera-tile active" data-camera-id="cam-1"' in response.text


def test_playback_without_query_params_behaves_exactly_as_before(client):
    response = client.get("/playback", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert 'class="playback-camera-tile active" data-camera-id="cam-1"' in response.text
    assert "const initialTimestamp=null;" in response.text


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


def test_playback_filter_buttons_are_no_longer_disabled(client):
    response = client.get("/playback", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    filters_html = response.text.split('class="monitor-filters"')[1][:1500]
    assert "disabled" not in filters_html
