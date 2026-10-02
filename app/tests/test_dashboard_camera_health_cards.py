"""Dashboard camera section = CAMERA HEALTH + RECENT ACTIVITY (2026-09-28).
The cards used to look like live tiles ("Connecting… Live preview will
appear", LIVE badges) while live video already has its own page. Each card
is now compact: online/offline, recording, analytics in use, last event, a
warning only when needed, a small snapshot clearly labelled as one, and
quick links. No new polling -- the snapshot refresh is slower than before."""
import inspect
import sqlite3

import main
from test_dashboard_permission_mismatch_fix import (  # noqa: F401  (fixtures)
    _owner_cookie, _seed_camera, _seed_permission, _seed_tenant, _seed_viewer, _viewer_cookie, db_path, http_client, partner_portal,
)


def _card(html, n):
    start = html.index(f'id="dashboard-camera-{n}"')
    return html[start:html.index("</article>", start)]


def _seed_front_door(db_path, *, with_event=True):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1, name="Front Door")
    conn.execute("UPDATE cameras SET local_recording_mode='event', smart_motion_enabled=1, lpr_enabled=1, appliance_id='app-cust-1' WHERE id='cam-1'")
    if with_event:
        for i, (etype, ts) in enumerate((("car", "2026-09-28T10:00:00"), ("person", "2026-09-28T11:30:00"))):
            conn.execute(
                "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)", (f"ev-{i}", "cust-1", "site-cust-1", "app-cust-1", "cam-1", f"l-{i}", etype, ts, ts))
    conn.commit()
    conn.close()


def test_card_shows_health_and_activity_not_a_live_tile(http_client, db_path):
    _seed_front_door(db_path)
    html = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}).text
    card = _card(html, 1)
    assert "Front Door" in card
    assert "Recording</dt>" in card and ">Event<" in card
    assert "Smart Motion, LPR" in card
    assert "Last event</dt>" in card and "Person" in card and 'data-at="2026-09-28T11:30:00"' in card  # the newest event
    assert ">Snapshot<" in card  # the image is labelled as a snapshot
    for live_tile_text in ("Live preview will appear", "dashboard-live-badge", ">LIVE<", "Connecting to"):
        assert live_tile_text not in card


def test_quick_actions_link_live_playback_events_and_rules(http_client, db_path):
    _seed_front_door(db_path)
    card = _card(http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}).text, 1)
    for href in ('/customer/cameras/cam-1/live', '/playback?camera=cam-1', '/events?camera=cam-1', '/customer/cameras/cam-1/analytics-rules'):
        assert f'href="{href}"' in card


def test_a_viewer_without_live_access_gets_no_live_action(http_client, db_path):
    _seed_front_door(db_path)
    conn = sqlite3.connect(db_path)
    _seed_viewer(conn, "viewer-1", customer_id="cust-1", email="viewer@example.test")
    _seed_permission(conn, user_id="viewer-1", camera_id="cam-1", can_playback=True, can_live=False)
    conn.commit()
    conn.close()
    card = _card(http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _viewer_cookie("viewer@example.test", "cust-1")}).text, 1)
    assert "/customer/cameras/cam-1/live" not in card and 'href="/playback?camera=cam-1"' in card and "Playback only" in card


def test_a_camera_without_events_says_so(http_client, db_path):
    _seed_front_door(db_path, with_event=False)
    card = _card(http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}).text, 1)
    assert "No events yet" in card


def test_health_facts_fail_soft_for_unknown_cameras(http_client, db_path):
    assert main._dashboard_camera_health_facts([], {3: "missing-camera"}) == {}
    assert main._dashboard_camera_health_facts([], {}) == {}


def test_status_poll_drives_online_recording_and_attention_without_new_polling():
    source = inspect.getsource(main.dashboard) if hasattr(main, "dashboard") else ""
    page = main.__file__
    text = open(page, encoding="utf-8").read()
    assert "DASHBOARD_SNAPSHOT_REFRESH_MS=60000" in text  # was 20 s per camera
    assert "state.textContent=camera.online?'Online':'Offline'" in text
    assert "Recording problem: ${camera.recording}" in text
    assert 'id="dashboard-attention"' in text and "All cameras healthy" in text
    assert "document.querySelectorAll('[data-dashboard-camera]')" in text


def test_events_page_opens_filtered_to_one_camera(http_client, db_path):
    _seed_front_door(db_path)
    conn = sqlite3.connect(db_path)
    _seed_camera(conn, "cam-2", customer_id="cust-1", camera_number=2, name="Driveway")
    conn.commit()
    conn.close()
    cookie = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}
    html = http_client.get("/events?camera=cam-1", cookies=cookie).text
    assert 'checked data-camera="1"' in html and 'checked data-camera="2"' not in html
    html_all = http_client.get("/events?camera=someone-elses-camera", cookies=cookie).text  # unknown ids are ignored
    assert 'checked data-camera="1"' in html_all and 'checked data-camera="2"' in html_all


def test_events_page_only_filters_on_load_when_opened_with_a_camera(http_client, db_path):
    _seed_front_door(db_path)
    cookie = {partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")}
    assert 'data-initial-filter="1"' in http_client.get("/events?camera=cam-1", cookies=cookie).text
    assert 'data-initial-filter="1"' not in http_client.get("/events", cookies=cookie).text


def test_dashboard_section_heading_describes_health_cards_not_live_tiles():
    import main
    source = open(main.__file__, encoding="utf-8").read()
    assert "<h2>Camera health</h2>" in source
    assert "Click any camera to open its full live view." not in source
