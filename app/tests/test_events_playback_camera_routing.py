"""Events-to-Playback camera-routing fix (2026-09-02): every event row's
"Playback" link was a bare, param-less "/playback" href
(_customer_event_actions() never passed camera=/t=), so
_render_customer_playback()'s requested_camera_id always resolved to
None and fell back to cameras[0]["id"] -- every event, regardless of
its actual camera, opened Playback on the customer's first camera by
camera_number. Reproduced by Alejandro on real devices across multiple
cameras. Fix passes the event's own real camera_id (and timestamp, so
the nearest recording is preselected too).
"""

import sqlite3
from datetime import datetime, timezone

import main
from database_backend import override_target
from partner_db import initialize_database

_EXPECTED_EPOCH_MS = int(datetime(2026, 9, 1, 20, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)


def test_customer_event_actions_links_to_the_events_own_camera():
    html = main._customer_event_actions("cam-living-room", "2026-09-01T20:15:00")
    assert f'href="/playback?camera=cam-living-room&t={_EXPECTED_EPOCH_MS}&autoplay=event"' in html
    assert 'href="/playback"' not in html


def test_customer_event_actions_without_timestamp_still_scopes_the_camera():
    html = main._customer_event_actions("cam-front-door")
    assert 'href="/playback?camera=cam-front-door"' in html


def test_customer_event_actions_falls_back_to_bare_link_only_without_a_camera_id():
    html = main._customer_event_actions(None)
    assert 'href="/playback"' in html
    assert "Live view" not in html


def test_events_page_links_each_row_to_its_own_camera_not_the_first_camera(tmp_path, monkeypatch):
    db_path = tmp_path / "test_events_routing.db"
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
        conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Test Co','test@example.com','active','2026-01-01')")
        conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main','2026-01-01')")
        conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-TEST','2026-01-01')")
        # camera_number=1 (would be the wrong fallback) vs camera_number=2 (the event's real camera)
        conn.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES('cam-driveway','cust-1','site-1','appl-1',1,'Driveway Left','2026-01-01')")
        conn.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES('cam-livingroom','cust-1','site-1','appl-1',2,'Living Room','2026-01-01')")
        conn.commit()

        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [
            {"id": "cam-driveway", "name": "Driveway Left", "camera_number": 1},
            {"id": "cam-livingroom", "name": "Living Room", "camera_number": 2},
        ])
        monkeypatch.setattr(main, "_customer_detection_events", lambda request: [
            {
                "id": "ev-1", "camera": 2, "camera_id": "cam-livingroom",
                "camera_name": "Living Room", "event_type": "person",
                "timestamp": "2026-09-01T20:15:00", "confidence": 0.9,
                "thumbnail": None, "has_event_clip": True,
            },
        ])

        from types import SimpleNamespace
        request = SimpleNamespace(query_params=SimpleNamespace(get=lambda k, d=None: d))
        html = main._render_customer_events(request)

    # has_event_clip=True on this fixture -- see test_event_direct_playback.py
    # for the event=<id> deep-link behavior itself; this test only
    # re-confirms the camera (cam-livingroom, not the cam-driveway
    # fallback) is still correct in that link.
    assert 'href="/playback?camera=cam-livingroom&event=ev-1&autoplay=event"' in html
    # The old bug's exact symptom: a bare, camera-less link anywhere on the page.
    assert 'href="/playback">Playback</a>' not in html
