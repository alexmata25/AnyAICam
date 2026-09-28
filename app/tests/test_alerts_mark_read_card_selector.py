"""Regression coverage for the Smart Alerts "Mark read" button (2026-09-23):

The per-card "Mark read" button carries its own data-notification-id
attribute (so the click handler can read the id without a DOM lookup),
but the click handler's own ancestor lookup used a bare
`button.closest('[data-notification-id]')` -- and Element.closest()
checks the starting element itself before walking up. Since the button
itself already matches that selector, `closest()` returned the button,
never its ancestor `<article>` card. The backend POST /api/customer/
notifications/{id}/read still succeeded (confirmed live: 200 OK,
read_at set) because the id was read correctly either way -- but
markCardRead() then read/wrote data-read, .alert-unread, and the
"remove the button" step against the wrong element (a plain <button>
has none of those), so the card's own read state, styling, and button
never visibly changed. A customer clicking "Mark read" saw nothing
happen and had no way to tell the click had actually worked
server-side.

This file proves the fix at the only level this bug lived at -- the
embedded JS itself, via the real rendered /alerts page -- plus an
end-to-end check that the already-correct backend round trip
(POST .../read -> read_at set -> a fresh render shows the card as
read) was never the broken half.

Same fixture idiom as test_customer_investigate_and_alerts.py /
test_live_view_page_face_access_ui.py: real HTTP through the real app
(TestClient(main.app)), a throwaway sqlite DB via override_target().
"""

import sqlite3
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database


CAMERA_CASES = [("cam-alert-a", 11), ("cam-alert-b", 33)]


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_alerts_mark_read_card_selector.db"


def _seed(conn, camera_id, camera_number, notif_id):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) "
        "VALUES('cust-1','partner-1','Test Co','test@example.com','active','2026-01-01')"
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-1','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (camera_id, "cust-1", "site-1", "appl-1", camera_number, "Test Camera", "2026-01-01"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
        "VALUES('user-owner','partner-1','owner-a@example.test','Owner','customer_owner','x',1,'cust-1','2026-01-01')"
    )
    conn.execute(
        "INSERT INTO notifications(id,user_id,customer_id,site_id,camera_id,event_id,recording_id,"
        "event_type,severity,title,message,timestamp,thumbnail,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (notif_id, "user-owner", "cust-1", "site-1", camera_id, None, None,
         "motion", "info", "Motion detected", None, "2026-09-22T20:00:00", None, "2026-09-22T20:00:00"),
    )
    conn.commit()


def _owner_cookie():
    return partner_portal._token("owner-a@example.test", "customer_owner", None, "cust-1", None)


@contextmanager
def _seeded_client(db_path, camera_id, camera_number, notif_id):
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        from partner_db import connection
        with connection() as conn:
            _seed(conn, camera_id, camera_number, notif_id)
        from cloud_config import settings
        trusted = settings.effective_trusted_hosts or []
        allowed_host = "testserver" if ("*" in trusted or "testserver" in trusted or not trusted) else trusted[0]
        with TestClient(main.app, base_url=f"http://{allowed_host}") as test_client:
            yield test_client


def _add_notification(client_db_path, notif_id, *, user_id="user-owner", customer_id="cust-1", camera_id="cam-alert-x", event_type="motion", timestamp="2026-09-22T20:00:00"):
    with override_target(sqlite_path=str(client_db_path)):
        from partner_db import connection
        with connection() as conn:
            conn.execute(
                "INSERT INTO notifications(id,user_id,customer_id,site_id,camera_id,event_type,severity,title,timestamp,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (notif_id, user_id, customer_id, "site-1", camera_id, event_type, "info", "Alert", timestamp, timestamp),
            )


def _state(client_db_path, notif_id):
    with sqlite3.connect(client_db_path) as conn:
        return conn.execute("SELECT acknowledged_at, dismissed_at, bookmarked_at FROM notifications WHERE id=?", (notif_id,)).fetchone()


def test_smart_alerts_is_an_attention_inbox_not_a_read_unread_list(db_path):
    """Smart Alerts (2026-09-28): Acknowledge / Dismiss / Save replace the
    old Mark read workflow; the page links out to Events for history."""
    with _seeded_client(db_path, "cam-alert-x", 7, "notif-x") as client:
        response = client.get("/alerts", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    text = response.text
    assert "Mark read" not in text and "mark-alert-read" not in text
    assert 'data-sa-action="acknowledge"' in text and 'data-sa-action="dismiss"' in text and 'data-sa-action="save"' in text
    for label in ("All", "People", "Vehicles", "Security", "System"):
        assert f'>{label} <span class="sa-filter-count">' in text
    assert 'href="/events"' in text


@pytest.mark.parametrize("camera_id,camera_number", CAMERA_CASES)
def test_acknowledge_dismiss_and_save_round_trip(db_path, camera_id, camera_number):
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie()}
    with _seeded_client(db_path, camera_id, camera_number, "notif-ack") as client:
        _add_notification(db_path, "notif-dis", camera_id=camera_id, event_type="person", timestamp="2026-09-22T21:00:00")
        _add_notification(db_path, "notif-save", camera_id=camera_id, event_type="car", timestamp="2026-09-22T22:00:00")
        before = client.get("/alerts", cookies=cookies).text
        assert all(n in before for n in ("notif-ack", "notif-dis", "notif-save"))

        assert client.post("/api/customer/notifications/acknowledge", json={"ids": ["notif-ack"]}, cookies=cookies).json()["updated"] == 1
        assert client.post("/api/customer/notifications/dismiss", json={"ids": ["notif-dis"]}, cookies=cookies).json()["updated"] == 1
        assert client.post("/api/customer/notifications/bookmark", json={"ids": ["notif-save"], "saved": True}, cookies=cookies).json()["updated"] == 1

        active = client.get("/alerts", cookies=cookies).text
        handled = client.get("/alerts?view=handled", cookies=cookies).text
        saved = client.get("/alerts?view=saved", cookies=cookies).text
    assert "notif-ack" not in active and "notif-dis" not in active and "notif-save" in active
    assert "notif-ack" in handled and "notif-dis" not in handled
    assert "notif-save" in saved and "Saved ★" in saved
    assert _state(db_path, "notif-ack")[0] and _state(db_path, "notif-dis")[1] and _state(db_path, "notif-save")[2]


def test_an_intrusion_alarm_cannot_be_dismissed_only_acknowledged(db_path):
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie()}
    with _seeded_client(db_path, "cam-alert-x", 7, "notif-x") as client:
        _add_notification(db_path, "alarm-1", event_type="intrusion_alarm", timestamp="2026-09-22T23:00:00")
        page = client.get("/alerts", cookies=cookies).text
        client.post("/api/customer/notifications/dismiss", json={"ids": ["alarm-1"]}, cookies=cookies)
        assert _state(db_path, "alarm-1")[1] is None
        still = client.get("/alerts", cookies=cookies).text
        client.post("/api/customer/notifications/acknowledge", json={"ids": ["alarm-1"]}, cookies=cookies)
        after = client.get("/alerts", cookies=cookies).text
    assert 'class="sa-card sa-alarm"' in page and "INTRUSION ALARM" in page and 'href="tel:911"' in page
    assert page.index("alarm-1") < page.index("notif-x")  # pinned above ordinary alerts
    assert "alarm-1" in still and "alarm-1" not in after


def test_actions_only_touch_the_callers_own_rows(db_path):
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie()}
    with _seeded_client(db_path, "cam-alert-x", 7, "notif-x") as client:
        with override_target(sqlite_path=str(db_path)):
            from partner_db import connection
            with connection() as conn:
                conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
                             "VALUES('user-viewer','partner-1','viewer@example.test','Viewer','customer_viewer','x',1,'cust-1','2026-01-01')")
                conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-2','partner-1','Other','o@example.test','active','2026-01-01')")
                conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
                             "VALUES('user-other','partner-1','other@example.test','Other','customer_owner','x',1,'cust-2','2026-01-01')")
        _add_notification(db_path, "someone-else", user_id="user-viewer")
        _add_notification(db_path, "other-customer", user_id="user-other", customer_id="cust-2")
        result = client.post("/api/customer/notifications/acknowledge", json={"ids": ["someone-else", "other-customer"]}, cookies=cookies)
        bad = client.post("/api/customer/notifications/acknowledge", json={"ids": []}, cookies=cookies)
        anonymous = client.post("/api/customer/notifications/acknowledge", json={"ids": ["notif-x"]})
    assert result.json()["updated"] == 0
    assert _state(db_path, "someone-else")[0] is None and _state(db_path, "other-customer")[0] is None
    assert bad.status_code == 400 and anonymous.status_code in (401, 403, 303, 307)
