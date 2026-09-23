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


def test_alerts_page_js_resolves_the_ancestor_card_not_the_button_itself(db_path):
    """The bug's own root cause, asserted directly: the click handler
    must scope its ancestor lookup to the article card, not the bare
    attribute the button also happens to carry."""
    with _seeded_client(db_path, "cam-alert-x", 7, "notif-x") as client:
        response = client.get("/alerts", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    assert "button.closest('article[data-notification-id]')" in response.text
    # The broken selector must be completely gone, not just supplemented.
    assert "button.closest('[data-notification-id]')" not in response.text


@pytest.mark.parametrize("camera_id,camera_number", CAMERA_CASES)
def test_mark_read_round_trip_persists_and_renders_as_read(db_path, camera_id, camera_number):
    """The backend half of "Mark read" was always correct -- proves that
    end-to-end: POST the same route the fixed JS calls, then confirm a
    fresh render of the page shows the card as read (no button, no
    alert-unread class, data-read="1"), for cameras outside the 1-5
    Ryzen pilot range."""
    notif_id = f"notif-{camera_id}"
    cookies = {partner_portal.SESSION_COOKIE: _owner_cookie()}

    with _seeded_client(db_path, camera_id, camera_number, notif_id) as client:
        before = client.get("/alerts", cookies=cookies)
        assert f'data-notification-id="{notif_id}"' in before.text
        assert f'<button class="ghost-button mark-alert-read" type="button" data-notification-id="{notif_id}">Mark read</button>' in before.text
        assert f'data-read="0"' in before.text

        read_response = client.post(f"/api/customer/notifications/{notif_id}/read", cookies=cookies)
        assert read_response.status_code == 200
        assert read_response.json()["id"] == notif_id

        after = client.get("/alerts", cookies=cookies)
        assert f'data-notification-id="{notif_id}"' in after.text
        assert f'data-notification-id="{notif_id}">Mark read</button>' not in after.text
        assert f'data-read="1"' in after.text
        assert "alert-unread" not in after.text.split(f'data-notification-id="{notif_id}"')[1].split("</article>")[0]
