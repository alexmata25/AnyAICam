"""P0 #5 remediation, Phase 3 (2026-09-05, Codex review): the desktop
Events page gets the same Processing... -> ready behavior the mobile
Playback cards already had (P0 #5) -- a genuinely fresh event (no
thumbnail yet, still within the pipeline's plausible processing
window) renders "Processing…" instead of "No clip available", and its
Action cell offers Live view but withholds a real Playback link until
either media is ready or the pending window has elapsed ("enable
Playback only when ready").

_is_customer_event_pending() is the server-side mirror of the
client-side isMobileEventPending()/isEventPending() check, used so the
very first server-rendered paint already shows the correct state
instead of a "No clip available" flash that the client's own poll loop
would otherwise correct a few seconds later. These tests cover that
server-side initial-render contract; the client-side reconciliation
loop (reconcileDesktopEvent(), scheduleEventPoll()) that keeps the
table live afterward is exercised by the desktop smoke/behavioral
checks in the P0 #5 staging report, since it -- like the mobile
version -- depends on real async timing a plain HTML-content assertion
cannot exercise.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import partner_portal
from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_p05_desktop_events_parity.db"


@pytest.fixture()
def http_client(db_path):
    from fastapi.testclient import TestClient

    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client


def _seed_tenant(conn, customer_id, partner_id="partner-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, f"Customer {customer_id}", f"{customer_id}@example.test", "active", "2026-01-01"),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{customer_id}", customer_id, "Main Site", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
        (f"app-{customer_id}", customer_id, f"site-{customer_id}", f"cloud-{customer_id}", "2026-01-01"),
    )


def _seed_camera(conn, camera_id, *, customer_id, camera_number, name):
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES(?,?,?,?,?,?,?)",
        (camera_id, customer_id, f"site-{customer_id}", name, "configured", camera_number, "2026-01-01"),
    )


def _seed_event(conn, event_id, *, customer_id, camera_id, timestamp, event_type="person"):
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,"
        "event_type,confidence,object_count,detections_json,event_timestamp,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, customer_id, f"site-{customer_id}", f"app-{customer_id}", camera_id, f"local-{event_id}",
         event_type, 0.9, 1, None, timestamp, timestamp),
    )


def _seed_media(conn, event_id, *, customer_id, camera_id, timestamp):
    conn.execute(
        "INSERT INTO detection_event_media(id,detection_event_id,customer_id,camera_id,s3_key,"
        "thumbnail_s3_key,started_at,ended_at,duration_seconds,size_bytes,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (f"media-{event_id}", event_id, customer_id, camera_id, f"clips/{event_id}.mp4",
         f"clips/{event_id}.jpg", timestamp, timestamp, 3.0, 0, timestamp),
    )


def _owner_cookie(customer_id):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


def _table_body(html):
    """Just the <tbody>...</tbody> markup -- the page's own <script>
    block (Phase 3's client-side reconciliation JS) and <style> block
    necessarily contain some of the same class-name/text literals
    (e.g. .event-action-pending{...} in CSS) the server-rendered row
    itself may or may not use for a given event -- asserting against
    the whole page would false-positive on that source text rather
    than the actual rendered row."""
    start = html.index("<tbody>")
    end = html.index("</tbody>") + len("</tbody>")
    return html[start:end]


def _utc_now_naive_iso(offset_seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).replace(tzinfo=None).isoformat()


# --------------------------------------------------------- _is_customer_event_pending() unit branches

def test_pending_helper_true_for_fresh_clipless_event():
    event = {"has_event_clip": False, "thumbnail": None, "timestamp": _utc_now_naive_iso(-5)}
    assert main._is_customer_event_pending(event) is True


def test_pending_helper_false_once_media_is_ready():
    event = {"has_event_clip": True, "thumbnail": "/x", "timestamp": _utc_now_naive_iso(-5)}
    assert main._is_customer_event_pending(event) is False


def test_pending_helper_false_once_the_window_has_elapsed():
    event = {"has_event_clip": False, "thumbnail": None, "timestamp": _utc_now_naive_iso(-200)}
    assert main._is_customer_event_pending(event) is False


def test_pending_helper_false_for_malformed_timestamp():
    event = {"has_event_clip": False, "thumbnail": None, "timestamp": "not-a-real-timestamp"}
    assert main._is_customer_event_pending(event) is False


# --------------------------------------------------------- GET /events initial render

def test_fresh_pending_event_shows_processing_not_no_clip_available(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-a")
    _seed_camera(conn, "cam-a1", customer_id="cust-a", camera_number=1, name="Front Door")
    _seed_event(conn, "evt-fresh", customer_id="cust-a", camera_id="cam-a1", timestamp=_utc_now_naive_iso(-5))
    conn.commit()
    conn.close()

    response = http_client.get("/events", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")})
    assert response.status_code == 200
    assert "Processing…" in response.text
    assert "No clip available" not in response.text
    row_html = _table_body(response.text)
    # Round 2 (2026-09-05, Codex second review): media_state is tracked
    # explicitly on the row from first render, not re-derived from
    # timestamp age at settlement time later -- see
    # settleExpiredDesktopEvents()'s own docstring in main.py for why.
    assert 'data-media-state="processing"' in row_html


def test_pending_event_offers_live_view_but_not_a_real_playback_link(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-a")
    _seed_camera(conn, "cam-a1", customer_id="cust-a", camera_number=1, name="Front Door")
    _seed_event(conn, "evt-fresh", customer_id="cust-a", camera_id="cam-a1", timestamp=_utc_now_naive_iso(-5))
    conn.commit()
    conn.close()

    response = http_client.get("/events", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")})
    row_html = _table_body(response.text)
    assert "/customer/cameras/cam-a1/live" in row_html
    assert 'event-action-pending' in row_html
    assert "/playback?camera=cam-a1&t=" not in row_html


def test_ready_event_gets_a_real_enabled_playback_link(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-a")
    _seed_camera(conn, "cam-a1", customer_id="cust-a", camera_number=1, name="Front Door")
    _seed_event(conn, "evt-ready", customer_id="cust-a", camera_id="cam-a1", timestamp=_utc_now_naive_iso(-5))
    _seed_media(conn, "evt-ready", customer_id="cust-a", camera_id="cam-a1", timestamp=_utc_now_naive_iso(-2))
    conn.commit()
    conn.close()

    response = http_client.get("/events", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")})
    row_html = _table_body(response.text)
    assert "/playback?camera=cam-a1&event=evt-ready&autoplay=event" in row_html
    assert 'event-action-pending' not in row_html
    assert 'data-media-state="ready"' in row_html


def test_old_clipless_event_still_shows_the_em_dash_unchanged(http_client, db_path):
    """A genuinely old event that will never get a clip (most detection
    types, by design) must render exactly as it did before this
    milestone -- the desktop Events table's own long-standing "no
    thumbnail" fallback is a plain em dash (see
    test_events_thumbnail_fix.py), never mobile Playback's "No clip
    available" wording. Processing… is only for events still inside
    the pending window."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-a")
    _seed_camera(conn, "cam-a1", customer_id="cust-a", camera_number=1, name="Front Door")
    _seed_event(conn, "evt-old", customer_id="cust-a", camera_id="cam-a1", timestamp="2026-01-01T00:00:00.000000")
    conn.commit()
    conn.close()

    response = http_client.get("/events", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")})
    row_html = _table_body(response.text)
    assert '<td class="event-thumbnail-cell">—</td>' in row_html
    assert "Processing…" not in row_html
    assert 'event-thumb-pending' not in row_html
    assert 'data-media-state="unavailable"' in row_html
    assert "Not ready yet" in row_html
    assert 'href="/playback' not in row_html


def test_rows_carry_event_id_and_timestamp_for_client_side_reconciliation(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-a")
    _seed_camera(conn, "cam-a1", customer_id="cust-a", camera_number=1, name="Front Door")
    _seed_event(conn, "evt-1", customer_id="cust-a", camera_id="cam-a1", timestamp=_utc_now_naive_iso(-5))
    conn.commit()
    conn.close()

    response = http_client.get("/events", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")})
    assert 'data-event-id="evt-1"' in response.text
    assert 'class="event-thumbnail-cell"' in response.text
    assert 'class="event-action-cell"' in response.text


def test_both_event_count_pills_carry_the_class_and_data_count_client_side_reconciliation_needs(http_client, db_path):
    """Round 2 (2026-09-05, Codex second review): both server-rendered
    'N event(s)' pill displays (topbar + panel-head) must expose a
    class/data-count hook so the client's updateVisibleEventCount()
    can keep them in sync once reconciliation actually adds a new row
    -- these were static counts with no such hook in round 1."""
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-a")
    _seed_camera(conn, "cam-a1", customer_id="cust-a", camera_number=1, name="Front Door")
    _seed_event(conn, "evt-1", customer_id="cust-a", camera_id="cam-a1", timestamp=_utc_now_naive_iso(-5))
    _seed_event(conn, "evt-2", customer_id="cust-a", camera_id="cam-a1", timestamp=_utc_now_naive_iso(-10))
    conn.commit()
    conn.close()

    response = http_client.get("/events", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")})
    assert response.text.count('class="pill event-count-pill" data-count="2"') == 1
    assert response.text.count('class="health-detail event-count-pill" data-count="2"') == 1
