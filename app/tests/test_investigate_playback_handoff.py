"""Investigate -> Playback handoff fix (2026-09-05): investigation_page()
built its own separate, outdated inline Playback URL
(?camera=<id>&t=<raw ISO timestamp>, no event=, no autoplay=event)
instead of reusing _customer_event_actions()'s already-fixed link
logic (Events page, Alerts). That meant Investigate alone had
regressed to the original 2026-09-02 "playable events depend on a
nearby continuous recording" bug -- an event whose moment falls in a
gap between catalog recordings silently "does not play" from
Investigate even when its own real clip exists -- and never
autoplayed even when a nearby recording happened to exist, since
autoplay=event was never set at all.

Fix: the URL-building logic is extracted into one shared helper,
_customer_event_playback_href(camera_id, timestamp, event_id,
has_event_clip), and both _customer_event_actions() and
investigation_page() now call it -- there is exactly one place this
logic lives. See that function's own docstring for the full contract:
event= takes priority whenever has_event_clip and a real event_id are
both present; otherwise a real timestamp is converted to an
unambiguous epoch-ms integer via _naive_utc_timestamp_to_epoch_ms()
(never a raw ISO string) with autoplay=event; a missing camera_id
always returns the bare '/playback' path, never a broken query
string.

Tests below cover: the shared helper's own branches in isolation;
_customer_event_actions() (Events/Alerts) delegating to it with
byte-identical external output to before this refactor; the real
Investigate page (GET /investigate) embedding the correct link shape
per event; the real Playback page (GET /playback) correctly resolving
camera/event/autoplay from both link shapes it now receives, and
still rendering normally with no deep-link params at all (manual
Playback); and a source-level regression check that no duplicate
inline '/playback?camera=...&t=' construction was left behind.
"""

import re
import sqlite3

import pytest

import partner_portal
from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_investigate_playback_handoff.db"


@pytest.fixture()
def http_client(db_path):
    from fastapi.testclient import TestClient

    with override_target(sqlite_path=db_path):
        initialize_database()
        # base_url must be one of cloud_settings.effective_trusted_hosts --
        # see test_dashboard_camera_tenant_scoping.py's own fixture for why
        # this is required to avoid TrustedHostMiddleware's "Invalid host
        # header" 400 on every request in this suite.
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


def _seed_camera(conn, camera_id, *, customer_id, camera_number, name, status="configured"):
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES(?,?,?,?,?,?,?)",
        (camera_id, customer_id, f"site-{customer_id}", name, status, camera_number, "2026-01-01"),
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


# ----------------------------------------------------- _customer_event_playback_href() unit branches

def test_playback_href_uses_event_id_when_clip_is_ready():
    href = main._customer_event_playback_href("cam-1", "2026-09-05T00:00:00.000000", "evt-1", True)
    assert href == "/playback?camera=cam-1&event=evt-1&autoplay=event"


def test_playback_href_preserves_canonical_event_id_while_media_is_pending():
    href = main._customer_event_playback_href("cam-1", "2026-09-05T00:00:00.000000", "evt-1", False)
    assert href == "/playback?camera=cam-1&event=evt-1&autoplay=event"
    assert "&t=" not in href, "a canonical event must never fall back to unrelated recording footage"


def test_playback_href_falls_back_to_epoch_ms_when_clip_flagged_but_no_event_id():
    href = main._customer_event_playback_href("cam-1", "2026-09-05T00:00:00.000000", None, True)
    assert "event=" not in href
    assert re.fullmatch(r"/playback\?camera=cam-1&t=\d+&autoplay=event", href)


def test_playback_href_omits_autoplay_with_no_timestamp_and_no_clip():
    assert main._customer_event_playback_href("cam-1") == "/playback?camera=cam-1"


@pytest.mark.parametrize("camera_id", [None, ""])
def test_playback_href_never_builds_broken_link_without_camera_id(camera_id):
    assert main._customer_event_playback_href(camera_id, "2026-09-05T00:00:00", "evt-1", True) == "/playback"


# ----------------------------------------------------- _customer_event_actions() (Events/Alerts) unchanged

def test_customer_event_actions_with_clip_matches_pre_refactor_shape():
    html = main._customer_event_actions("cam-1", "2026-09-05T00:00:00.000000", "evt-1", True)
    assert html == (
        '<a class="download" href="/customer/cameras/cam-1/live">Live view</a> '
        '<a class="download" href="/playback?camera=cam-1&event=evt-1&autoplay=event">Playback</a>'
    )


def test_customer_event_actions_without_clip_matches_pre_refactor_shape():
    html = main._customer_event_actions("cam-1", "2026-09-05T00:00:00.000000", None, False)
    match = re.search(r'href="(/playback\?camera=cam-1&t=\d+&autoplay=event)"', html)
    assert match, html
    assert '<a class="download" href="/customer/cameras/cam-1/live">Live view</a>' in html


def test_customer_event_actions_without_camera_id_matches_pre_refactor_shape():
    assert main._customer_event_actions(None) == '<a class="download" href="/playback">Playback</a>'


# ----------------------------------------------------- GET /investigate embeds the correct link per event

def test_investigate_page_embeds_event_link_for_event_with_clip(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-a")
    _seed_camera(conn, "cam-a1", customer_id="cust-a", camera_number=1, name="Front Door")
    _seed_event(conn, "evt-with-clip", customer_id="cust-a", camera_id="cam-a1", timestamp="2026-09-05T10:00:00.000000")
    _seed_media(conn, "evt-with-clip", customer_id="cust-a", camera_id="cam-a1", timestamp="2026-09-05T10:00:03.000000")
    conn.commit()
    conn.close()

    response = http_client.get("/investigate", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")})
    assert response.status_code == 200
    match = re.search(r"const investigationEvents=(\[.*?\]);", response.text, re.S)
    assert match, "investigationEvents JSON not found in /investigate response"
    import json
    events = json.loads(match.group(1))
    event = next(e for e in events if e["id"] == "evt-with-clip")
    assert event["recording"] == "/playback?camera=cam-a1&event=evt-with-clip&autoplay=event"


def test_investigate_page_preserves_canonical_event_link_while_media_is_pending(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-a")
    _seed_camera(conn, "cam-a1", customer_id="cust-a", camera_number=1, name="Front Door")
    _seed_event(conn, "evt-no-clip", customer_id="cust-a", camera_id="cam-a1", timestamp="2026-09-05T11:00:00.000000")
    conn.commit()
    conn.close()

    response = http_client.get("/investigate", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")})
    assert response.status_code == 200
    match = re.search(r"const investigationEvents=(\[.*?\]);", response.text, re.S)
    import json
    events = json.loads(match.group(1))
    event = next(e for e in events if e["id"] == "evt-no-clip")
    assert event["recording"] == "/playback?camera=cam-a1&event=evt-no-clip&autoplay=event"
    assert "&t=" not in event["recording"]


# ----------------------------------------------------- GET /playback resolves both deep-link shapes

def test_playback_page_resolves_event_deep_link(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-a")
    _seed_camera(conn, "cam-a1", customer_id="cust-a", camera_number=1, name="Front Door")
    _seed_camera(conn, "cam-a2", customer_id="cust-a", camera_number=2, name="Living Room")
    _seed_event(conn, "evt-with-clip", customer_id="cust-a", camera_id="cam-a2", timestamp="2026-09-05T10:00:00.000000")
    _seed_media(conn, "evt-with-clip", customer_id="cust-a", camera_id="cam-a2", timestamp="2026-09-05T10:00:03.000000")
    conn.commit()
    conn.close()

    response = http_client.get(
        "/playback?camera=cam-a2&event=evt-with-clip&autoplay=event",
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")},
    )
    assert response.status_code == 200
    assert 'const initialEventId="evt-with-clip";' in response.text
    assert "const autoplayFromEvent=true;" in response.text
    assert 'playback-camera-tile active" data-camera-id="cam-a2"' in response.text


def test_playback_page_resolves_timestamp_deep_link(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-a")
    _seed_camera(conn, "cam-a1", customer_id="cust-a", camera_number=1, name="Front Door")
    conn.commit()
    conn.close()

    response = http_client.get(
        "/playback?camera=cam-a1&t=1788566400000&autoplay=event",
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")},
    )
    assert response.status_code == 200
    assert "const initialTimestampRaw=\"1788566400000\";" in response.text
    assert "const autoplayFromEvent=true;" in response.text
    assert 'playback-camera-tile active" data-camera-id="cam-a1"' in response.text


def test_playback_page_manual_navigation_unaffected(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-a")
    _seed_camera(conn, "cam-a1", customer_id="cust-a", camera_number=1, name="Front Door")
    conn.commit()
    conn.close()

    response = http_client.get("/playback", cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")})
    assert response.status_code == 200
    assert "playback-camera-tile" in response.text
    assert "const autoplayFromEvent=false;" in response.text


# ----------------------------------------------------- no duplicate/conflicting link-building logic

def test_no_duplicate_inline_playback_link_construction_remains():
    import inspect
    source = inspect.getsource(main)
    assert source.count("def _customer_event_playback_href(") == 1
    # The only literal '/playback?camera={...}...&t=' text pattern in the
    # whole module should be inside _customer_event_playback_href() itself.
    inline_occurrences = re.findall(r'/playback\?camera=\{[^}]*\}[^"\']*&t=', source)
    assert len(inline_occurrences) == 0, (
        "found a leftover inline '/playback?camera=...&t=' construction outside "
        "the shared helper -- investigation_page() (or any other caller) must "
        "call _customer_event_playback_href() instead of rebuilding this URL itself"
    )
