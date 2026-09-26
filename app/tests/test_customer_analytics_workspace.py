"""Customer Analytics workspaces (2026-09-25): one Analytics entry (desktop
flyout / phone submenu) listing only the analytics the customer has, a
dedicated page per analytic at /analytics/<slug>, an overview at
/analytics, and /api/customer/analytics/{key}/events over existing
detection_events data -- tenant-scoped, limited to the caller's permitted
cameras and to the cameras entitled to that analytic."""
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import customer_analytics_workspace as ws
import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database

DAY_MS = 86400000
START, END = 1790294400000, 1790294400000 + DAY_MS  # 2026-09-25 00:00 UTC .. +1 day
STATIC = Path(__file__).resolve().parents[1] / "static"


def _event(conn, event_id, customer, camera, kind, ts, confidence=0.8, detections=None, clip=False, thumb=False):
    conn.execute("INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,confidence,"
                 "object_count,detections_json,event_timestamp,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (event_id, customer, f"site-{customer}", "app", camera, event_id, kind, confidence, 1, detections, ts, ts))
    if clip:
        conn.execute("INSERT INTO detection_event_media(id,detection_event_id,customer_id,camera_id,s3_key,thumbnail_s3_key,started_at,ended_at,created_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", (f"m-{event_id}", event_id, customer, camera, "e.mp4", "e.jpg" if thumb else None, ts, ts, ts))


def _entitle(conn, camera, *keys):
    for key in keys:
        conn.execute("INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) VALUES(?,?,'active','x','x')",
                     (camera, key))


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "analytics_workspace.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO partners(id,name,created_at) VALUES('p1','P','2026-01-01')")
        for customer in ("cust-1", "cust-2"):
            conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
                         (customer, "p1", customer, f"{customer}@example.test", "active", "2026-01-01"))
            conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{customer}", customer, "Main", "2026-01-01"))
        for camera, customer, number in (("cam-a", "cust-1", 1), ("cam-b", "cust-1", 2), ("cam-c", "cust-1", 3), ("cam-x", "cust-2", 1)):
            conn.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES(?,?,?,?,?,?,?)",
                         (camera, customer, f"site-{customer}", camera.upper(), "configured", number, "2026-01-01"))
        for uid, email, role, customer, mode in (("u-own", "owner@c1.test", "customer_owner", "cust-1", "all"),
                                                 ("u-view", "viewer@c1.test", "customer_viewer", "cust-1", "selected"),
                                                 ("u-own2", "owner@c2.test", "customer_owner", "cust-2", "all")):
            conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (uid, "p1", email, email, role, "x", 1, customer, "2026-01-01", "active", mode))
        conn.execute("INSERT INTO customer_camera_permissions(user_id,camera_id,can_live,can_playback) VALUES('u-view','cam-a',1,1)")
        _entitle(conn, "cam-a", "smart_motion", "people_counting", "ppe", "lpr")
        _entitle(conn, "cam-b", "smart_motion", "people_counting")
        _entitle(conn, "cam-x", "smart_motion", "facial_recognition")
        # cam-c has events but no entitlement at all.
        _event(conn, "sm-1", "cust-1", "cam-a", "person", "2026-09-25T10:00:00", 0.54, clip=True, thumb=True)
        _event(conn, "sm-2", "cust-1", "cam-b", "car", "2026-09-25T11:00:00", 0.9)
        _event(conn, "sm-3", "cust-1", "cam-a", "motion", "2026-09-25T12:00:00", 25.4)  # raw motion score, not a probability
        _event(conn, "sm-old", "cust-1", "cam-a", "person", "2026-09-20T12:00:00", 0.7)
        _event(conn, "sm-c", "cust-1", "cam-c", "person", "2026-09-25T09:00:00", 0.8)  # unentitled camera
        _event(conn, "sm-other", "cust-2", "cam-x", "person", "2026-09-25T10:30:00", 0.99)
        _event(conn, "pc-1", "cust-1", "cam-a", "people_counting_in", "2026-09-25T09:10:00", 0.0)
        _event(conn, "pc-2", "cust-1", "cam-a", "people_counting_in", "2026-09-25T09:20:00", 0.0)
        _event(conn, "pc-3", "cust-1", "cam-b", "people_counting_out", "2026-09-25T10:05:00", 0.0)
        _event(conn, "ppe-1", "cust-1", "cam-a", "ppe", "2026-09-25T08:00:00", 0.0, '[{"hard_hat_present": false, "safety_vest_present": true}]')
        _event(conn, "ppe-2", "cust-1", "cam-a", "ppe", "2026-09-25T08:05:00", 0.0, '[{"hard_hat_present": true, "safety_vest_present": true}]')
        _event(conn, "ppe-3", "cust-1", "cam-a", "ppe", "2026-09-25T08:10:00", 0.0, '[{"hard_hat_present": true, "safety_vest_present": false}]')
        _event(conn, "lpr-1", "cust-1", "cam-a", "plate", "2026-09-25T07:00:00", 0.93, '[{"plate": "ABC123"}]', clip=True, thumb=True)
        _event(conn, "lpr-2", "cust-1", "cam-a", "plate", "2026-09-25T07:05:00", 0.88, '[{"plate": "XYZ9"}]')
        _event(conn, "fr-1", "cust-2", "cam-x", "facial_recognition", "2026-09-25T07:00:00", 0.91,
               '[{"match_state": "known", "matched_person_name": "Ana", "matched_watchlist_name": "Family"}]')
        _event(conn, "fr-2", "cust-2", "cam-x", "facial_recognition", "2026-09-25T07:05:00", 0.41, '[{"match_state": "unknown"}]')
        _event(conn, "fr-c", "cust-1", "cam-a", "facial_recognition", "2026-09-25T07:05:00", 0.9, '[{"match_state": "known"}]')  # not entitled
        _event(conn, "lc-x", "cust-2", "cam-x", "line_crossing", "2026-09-25T06:00:00", 0.7,
               '[{"rule_name": "Line Crossing (Front gate)", "direction": "in"}]')
        conn.commit()
        conn.close()
        with TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client


def _cookie(email="owner@c1.test", role="customer_owner", customer="cust-1"):
    return {partner_portal.SESSION_COOKIE: partner_portal._token(email, role, None, customer, None)}


def _other():
    return _cookie("owner@c2.test", customer="cust-2")


def _get(client, key, cookies=None, **params):
    params = {"start_ms": START, "end_ms": END, **params}
    return client.get(f"/api/customer/analytics/{key}/events", params=params, cookies=cookies or _cookie())


def _ids(response):
    return [e["event_id"] for e in response.json()["events"]]


# ---------------------------------------------------------------- data API

def test_smart_motion_rows_summary_and_media_refs(client):
    body = _get(client, "smart_motion").json()
    assert [e["event_id"] for e in body["events"]] == ["sm-3", "sm-2", "sm-1"]  # newest first, range-limited
    clip = body["events"][2]
    assert (clip["has_clip"], clip["has_thumbnail"], clip["confidence"]) == (True, True, 0.54)
    assert body["events"][0]["confidence"] is None  # 25.4 is a motion score, never "2540%"
    assert body["events"][1]["confidence"] == 0.9
    assert body["summary"]["total"] == 3 and body["summary"]["by_type"] == {"person": 1, "car": 1, "motion": 1}
    assert sorted(body["enabled_camera_ids"]) == ["cam-a", "cam-b"]


def test_unentitled_cameras_are_never_shown(client):
    assert "sm-c" not in _ids(_get(client, "smart_motion"))
    assert _ids(_get(client, "smart_motion", camera_id="cam-c")) == []  # permitted but not entitled
    assert _ids(_get(client, "facial_recognition")) == []  # cust-1 has no Facial Recognition


def test_other_tenants_events_never_appear_and_their_cameras_are_404(client):
    assert _get(client, "smart_motion", camera_id="cam-x").status_code == 404
    assert _get(client, "smart_motion", camera_id="cam-a,cam-x").status_code == 404
    assert _ids(_get(client, "smart_motion", cookies=_other())) == ["sm-other"]


def test_viewer_only_sees_permitted_cameras(client):
    viewer = _cookie("viewer@c1.test", "customer_viewer")
    assert _ids(_get(client, "smart_motion", cookies=viewer)) == ["sm-3", "sm-1"]
    assert _get(client, "smart_motion", cookies=viewer, camera_id="cam-b").status_code == 404


def test_type_specific_filters_and_search(client):
    assert _ids(_get(client, "smart_motion", result="vehicle")) == ["sm-2"]
    assert _ids(_get(client, "people_counting", result="out")) == ["pc-3"]
    assert _ids(_get(client, "ppe", result="violation")) == ["ppe-3", "ppe-1"]
    assert _ids(_get(client, "ppe", result="missing_hard_hat")) == ["ppe-1"]
    assert _ids(_get(client, "ppe", result="missing_vest")) == ["ppe-3"]
    assert _ids(_get(client, "lpr", q="abc")) == ["lpr-1"]
    assert _ids(_get(client, "lpr", q="%")) == []  # LIKE wildcards are escaped
    assert _ids(_get(client, "facial_recognition", cookies=_other(), result="known")) == ["fr-1"]
    assert _ids(_get(client, "facial_recognition", cookies=_other(), q="ana")) == ["fr-1"]
    assert _get(client, "smart_motion", result="bogus").status_code == 400
    assert _get(client, "nonsense").status_code == 404


def test_each_analytic_reports_its_own_stored_fields(client):
    ppe = {e["event_id"]: e for e in _get(client, "ppe").json()["events"]}
    assert ppe["ppe-1"]["details"] == {"status": "violation", "hard_hat": False, "vest": True}
    assert ppe["ppe-1"]["confidence"] is None  # PPE's stored 0.0 is not a real confidence
    assert _get(client, "ppe").json()["summary"]["by_result"] == {"violation": 2, "compliant": 1, "missing_hard_hat": 1, "missing_vest": 1}
    lpr = {e["event_id"]: e for e in _get(client, "lpr").json()["events"]}
    assert lpr["lpr-1"]["details"] == {"plate": "ABC123"} and lpr["lpr-1"]["has_clip"]
    faces = _get(client, "facial_recognition", cookies=_other()).json()
    assert faces["summary"]["by_result"] == {"known": 1, "unknown": 1}
    assert {e["event_id"]: e["details"]["person"] for e in faces["events"]} == {"fr-1": "Ana", "fr-2": None}
    lc = _get(client, "line_crossing", cookies=_other()).json()
    assert lc["events"][0]["details"] == {"rule": "Front gate", "direction": "in"}  # "Line Crossing (...)" prefix dropped
    assert lc["summary"]["by_result"] == {"Front gate": 1}


def test_people_counting_totals_per_camera_and_hourly_trend(client):
    body = _get(client, "people_counting").json()
    summary = body["summary"]
    assert summary["by_type"] == {"people_counting_in": 2, "people_counting_out": 1}
    assert summary["per_camera"] == {"cam-a": {"in": 2, "out": 0}, "cam-b": {"in": 0, "out": 1}}
    assert [(h["in"], h["out"]) for h in summary["hourly"]] == [(2, 0), (0, 1)]
    assert all(e["confidence"] is None for e in body["events"])


def test_pagination_and_range_validation(client):
    first = ws.query_events(customer_id="cust-1", camera_ids=["cam-a", "cam-b"], key="smart_motion", start_ms=START, end_ms=END, limit=2)
    assert [e["event_id"] for e in first["events"]] == ["sm-3", "sm-2"] and first["next_before"] == "2026-09-25T11:00:00"
    rest = ws.query_events(customer_id="cust-1", camera_ids=["cam-a", "cam-b"], key="smart_motion", start_ms=START, end_ms=END,
                           before=first["next_before"], limit=2)
    assert [e["event_id"] for e in rest["events"]] == ["sm-1"] and rest["next_before"] is None
    assert _get(client, "smart_motion", end_ms=START).status_code == 400
    assert _get(client, "smart_motion", end_ms=START + 200 * DAY_MS).status_code == 400


def test_unauthenticated_api_is_refused(client):
    assert client.get("/api/customer/analytics/smart_motion/events", params={"start_ms": START, "end_ms": END}).status_code in (401, 403, 303)


# ---------------------------------------------------------------- menu + pages

def test_menu_lists_only_what_the_customer_has(client):
    assert [w["key"] for w in ws.available_workspaces("cust-1", ["cam-a", "cam-b", "cam-c"])] == [
        "smart_motion", "people_counting", "lpr", "ppe"]  # no faces; no rules or events -> no line crossing / intrusion
    assert [w["key"] for w in ws.available_workspaces("cust-2", ["cam-x"])] == [
        "smart_motion", "facial_recognition", "line_crossing"]  # it has line-crossing events
    assert [w["key"] for w in ws.available_workspaces("cust-1", ["cam-b"])] == ["smart_motion", "people_counting"]
    assert ws.available_workspaces("cust-1", []) == [] and ws.available_workspaces(None, ["cam-a"]) == []


def test_desktop_flyout_and_mobile_submenu_on_customer_pages(client):
    html = client.get("/playback", cookies=_cookie()).text
    flyout = re.search(r'<div class="nav-flyout" role="menu" aria-label="Analytics" hidden>(.*?)</div>', html, re.S).group(1)
    assert re.findall(r'>([^<]+)</a>', flyout) == ["All analytics", "Smart Motion", "People Counting", "License Plates", "PPE", "Smart Rules"]
    assert "Voice" not in flyout  # AAC Voice Call keeps its own place
    assert html.count("data-nav-flyout-toggle>") == 1  # one Analytics entry, not one per analytic
    assert '<button type="button" class="mobile-analytics-toggle' in html
    sheet = re.search(r'<div class="mobile-analytics-sheet" id="mobile-analytics-sheet"[^>]*>(.*?)</div>', html, re.S).group(1)
    assert 'href="/analytics/ppe"' in sheet and "facial-recognition" not in sheet
    viewer = client.get("/playback", cookies=_cookie("viewer@c1.test", "customer_viewer")).text
    assert 'href="/analytics/people-counting"' in viewer and 'href="/analytics/lpr"' in viewer


def test_menu_marks_the_current_workspace(client):
    html = client.get("/analytics/ppe", cookies=_cookie()).text
    assert '<a role="menuitem" href="/analytics/ppe" aria-current="page">PPE</a>' in html


def test_each_available_workspace_renders_its_own_page(client):
    for slug, label in (("smart-motion", "Smart Motion"), ("people-counting", "People Counting"), ("lpr", "License Plates"), ("ppe", "PPE")):
        html = client.get(f"/analytics/{slug}", cookies=_cookie()).text
        assert f"<h1>{label}</h1>" in html, slug
        assert "/static/analytics_workspace.js" in html and "/static/inline_media.js" in html and "window.__AW=" in html
    ppe = client.get("/analytics/ppe", cookies=_cookie()).text
    assert 'value="missing_hard_hat"' in ppe and 'id="aw-search"' not in ppe
    lpr = client.get("/analytics/lpr", cookies=_cookie()).text
    assert 'id="aw-search"' in lpr and "Plate contains" in lpr
    motion = client.get("/analytics/smart-motion?camera=cam-b", cookies=_cookie()).text
    assert '<option value="cam-b" selected>' in motion and 'value="cam-c"' not in motion and 'value="cam-x"' not in motion
    assert '<option value="cam-x" selected>' not in client.get("/analytics/smart-motion?camera=cam-x", cookies=_cookie()).text


def test_hidden_filters_stay_hidden(client):
    html = client.get("/analytics/smart-motion", cookies=_cookie()).text
    assert ".aw-filter[hidden]{display:none}" in html
    assert 'id="aw-from-wrap" hidden' in html and 'id="aw-to-wrap" hidden' in html


def test_unavailable_workspace_explains_itself(client):
    html = client.get("/analytics/facial-recognition", cookies=_cookie()).text
    assert "Not enabled for your cameras" in html and 'href="/subscription-portal"' in html and "window.__AW=" not in html
    assert client.get("/analytics/nonsense", cookies=_cookie()).status_code == 404


def test_overview_and_legacy_links(client):
    html = client.get("/analytics", cookies=_cookie()).text
    assert "view_analytics" not in html
    assert html.count('class="aw-tile"') == 5 and 'href="/analytics/ppe"' in html  # 4 analytics + Smart Rules
    response = client.get("/analytics?type=ppe&camera=cam-a", cookies=_cookie())
    assert response.status_code == 303 and response.headers["location"] == "/analytics/ppe?camera=cam-a"


# ---------------------------------------------------------------- Smart Rules

def _rule(db_path, rule_id, camera, rule_type, customer="cust-1", enabled=1):
    geometry = '[{"x":0.1,"y":0.1},{"x":0.9,"y":0.1}]' if rule_type in ("line_crossing", "people_counting") else         '[{"x":0.1,"y":0.1},{"x":0.5,"y":0.1},{"x":0.5,"y":0.5}]'
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO customer_analytics_rules(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,direction,geometry_json,enabled,created_at,updated_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (rule_id, customer, f"site-{customer}", "app", camera, rule_type, rule_id,
                                                   "both" if rule_type in ("line_crossing", "people_counting") else None, geometry, enabled, "x", "x"))
    conn.commit()
    conn.close()


def _rules_tiles(html):
    return {m.group(1): m.group(2) for m in re.finditer(r'<a class="aw-tile" href="/customer/cameras/([^/]+)/analytics-rules"[^>]*>(.*?)</a>', html, re.S)}


def test_smart_rules_is_in_the_analytics_menu_for_every_customer(client):
    html = client.get("/playback", cookies=_cookie()).text
    for container in (r'<div class="nav-flyout" role="menu" aria-label="Analytics" hidden>(.*?)</div>',
                      r'<div class="mobile-analytics-sheet" id="mobile-analytics-sheet"[^>]*>(.*?)</div>'):
        assert '<a role="menuitem" href="/analytics/smart-rules">Smart Rules</a>' in re.search(container, html, re.S).group(1)
    # Rules need no plan (a zone to ignore works on any camera): even with no analytics at all it is there.
    desktop, mobile, sheet = ws.nav_menu_html([], "/playback")
    assert 'href="/analytics/smart-rules"' in desktop and 'href="/analytics/smart-rules"' in sheet


def test_smart_rules_page_picks_a_camera_and_opens_its_existing_editor(client, db_path):
    _rule(db_path, "z1", "cam-a", "intrusion")
    _rule(db_path, "z2", "cam-a", "exclusion")
    _rule(db_path, "z3", "cam-a", "exclusion")
    _rule(db_path, "l1", "cam-b", "line_crossing")
    _rule(db_path, "p1", "cam-b", "people_counting")
    _rule(db_path, "off", "cam-c", "intrusion", enabled=0)
    _rule(db_path, "theirs", "cam-x", "exclusion", customer="cust-2")
    response = client.get("/analytics/smart-rules", cookies=_cookie())
    assert response.status_code == 200
    html = response.text
    assert "<h1>Smart Rules</h1>" in html and 'href="/analytics"' in html
    tiles = _rules_tiles(html)
    assert set(tiles) == {"cam-a", "cam-b", "cam-c"}  # never another customer's camera
    assert "1 detection zone, 2 zones to ignore" in tiles["cam-a"] and "Draw rules" in tiles["cam-a"]
    assert "1 line crossing, 1 counting line" in tiles["cam-b"]
    assert "No rules yet" in tiles["cam-c"]  # a disabled rule isn't counted
    assert '<a role="menuitem" href="/analytics/smart-rules" aria-current="page">Smart Rules</a>' in html
    # The editor it opens is the existing one, with a way back.
    editor = client.get("/customer/cameras/cam-a/analytics-rules", cookies=_cookie()).text
    assert "Ignore detections in zone" in editor and 'href="/analytics/smart-rules">Smart Rules</a>' in editor
    assert '<a role="menuitem" href="/analytics/smart-rules" aria-current="page">Smart Rules</a>' in editor


def test_smart_rules_respects_viewer_permissions_and_tenants(client, db_path):
    viewer = client.get("/analytics/smart-rules", cookies=_cookie("viewer@c1.test", "customer_viewer")).text
    tiles = _rules_tiles(viewer)
    assert set(tiles) == {"cam-a"} and "View rules" in tiles["cam-a"]  # no Camera Settings access -> view only
    other = client.get("/analytics/smart-rules", cookies=_other()).text
    assert set(_rules_tiles(other)) == {"cam-x"}
    assert client.get("/analytics/smart-rules").status_code in (302, 303, 401, 403)


def test_overview_offers_smart_rules(client):
    html = client.get("/analytics", cookies=_cookie()).text
    assert '<a class="aw-tile" href="/analytics/smart-rules"><strong>Smart Rules</strong>' in html


def test_face_scores_are_labelled_as_match_only_for_recognized_people():
    source = (STATIC / "analytics_workspace.js").read_text(encoding="utf-8")
    assert "known&&e.confidence!=null?`${pct(e.confidence)}% match`:null" in source


def test_no_microphone_in_analytics_media():
    for name in ("analytics_workspace.js", "inline_media.js"):
        source = (STATIC / name).read_text(encoding="utf-8")
        assert "talk-mic" not in source and "getUserMedia" not in source and "🎤" not in source


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_static_scripts_are_valid_javascript():
    for name in ("analytics_workspace.js", "inline_media.js"):
        result = subprocess.run(["node", "--check", str(STATIC / name)], capture_output=True, text=True)
        assert result.returncode == 0, (name, result.stderr)


def test_static_assets_are_versioned_per_build(client):
    """Cloudflare caches /static for 4 h; each page must request this
    build's JS/CSS (?v=<build>) so a deploy never runs against stale files."""
    tag = f"?v={ws.ASSET_VERSION}"
    workspace = client.get("/analytics/smart-motion", cookies=_cookie()).text
    for asset in ("analytics_workspace.js", "inline_media.js", "inline_media.css", "event_media.js"):
        assert f"/static/{asset}{tag}" in workspace, asset
    playback = client.get("/playback", cookies=_cookie()).text
    for asset in ("inline_media.js", "inline_media.css", "event_media.js"):
        assert f"/static/{asset}{tag}" in playback, asset
    assert f"/static/inline_media.css{tag}" in client.get("/analytics", cookies=_cookie()).text
    assert ws.versioned('src="/static/a.js"') == f'src="/static/a.js{tag}"'
