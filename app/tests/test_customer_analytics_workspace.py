"""Customer Analytics workspace (2026-09-25): /analytics for portal
customers (the Dashboard's 'Open analytics', People and Vehicles links
used to land on "Your current role does not include view_analytics"),
backed by /api/customer/analytics/{key}/events over existing
detection_events data only."""
import re
import shutil
import sqlite3
import subprocess

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database

DAY_MS = 86400000
START, END = 1790294400000, 1790294400000 + DAY_MS  # 2026-09-25 00:00 UTC .. +1 day


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "analytics_workspace.db"


def _event(conn, event_id, customer, camera, kind, ts, confidence=0.8, detections=None, clip=False, thumb=False):
    conn.execute("INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,confidence,"
                 "object_count,detections_json,event_timestamp,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (event_id, customer, f"site-{customer}", "app", camera, event_id, kind, confidence, 1, detections, ts, ts))
    if clip:
        conn.execute("INSERT INTO detection_event_media(id,detection_event_id,customer_id,camera_id,s3_key,thumbnail_s3_key,started_at,ended_at,created_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", (f"m-{event_id}", event_id, customer, camera, "e.mp4", "e.jpg" if thumb else None, ts, ts, ts))


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
        for camera, customer, number in (("cam-a", "cust-1", 1), ("cam-b", "cust-1", 2), ("cam-x", "cust-2", 1)):
            conn.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES(?,?,?,?,?,?,?)",
                         (camera, customer, f"site-{customer}", camera.upper(), "configured", number, "2026-01-01"))
        for uid, email, role, customer, mode in (("u-own", "owner@c1.test", "customer_owner", "cust-1", "all"),
                                                 ("u-view", "viewer@c1.test", "customer_viewer", "cust-1", "selected"),
                                                 ("u-own2", "owner@c2.test", "customer_owner", "cust-2", "all")):
            conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (uid, "p1", email, email, role, "x", 1, customer, "2026-01-01", "active", mode))
        conn.execute("INSERT INTO customer_camera_permissions(user_id,camera_id,can_live,can_playback) VALUES('u-view','cam-a',1,1)")
        conn.execute("INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) VALUES('cam-a','smart_motion','active','x','x')")
        _event(conn, "sm-1", "cust-1", "cam-a", "person", "2026-09-25T10:00:00", 0.54, clip=True, thumb=True)
        _event(conn, "sm-2", "cust-1", "cam-b", "car", "2026-09-25T11:00:00", 0.9)
        _event(conn, "sm-3", "cust-1", "cam-a", "motion", "2026-09-25T12:00:00", 25.4)  # raw motion score, not a probability
        _event(conn, "sm-old", "cust-1", "cam-a", "person", "2026-09-20T12:00:00", 0.7)
        _event(conn, "sm-other", "cust-2", "cam-x", "person", "2026-09-25T10:30:00", 0.99)
        _event(conn, "pc-1", "cust-1", "cam-a", "people_counting_in", "2026-09-25T09:10:00", 0.0)
        _event(conn, "pc-2", "cust-1", "cam-a", "people_counting_in", "2026-09-25T09:20:00", 0.0)
        _event(conn, "pc-3", "cust-1", "cam-b", "people_counting_out", "2026-09-25T10:05:00", 0.0)
        _event(conn, "ppe-1", "cust-1", "cam-a", "ppe", "2026-09-25T08:00:00", 0.0, '[{"hard_hat_present": false, "safety_vest_present": true}]')
        _event(conn, "ppe-2", "cust-1", "cam-a", "ppe", "2026-09-25T08:05:00", 0.0, '[{"hard_hat_present": true, "safety_vest_present": true}]')
        _event(conn, "fr-1", "cust-1", "cam-a", "facial_recognition", "2026-09-25T07:00:00", 0.91,
               '[{"match_state": "known", "matched_person_name": "Ana", "matched_watchlist_name": "Family"}]')
        _event(conn, "fr-2", "cust-1", "cam-a", "facial_recognition", "2026-09-25T07:05:00", 0.41, '[{"match_state": "unknown"}]')
        conn.commit()
        conn.close()
        with TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client


def _cookie(email="owner@c1.test", role="customer_owner", customer="cust-1"):
    return {partner_portal.SESSION_COOKIE: partner_portal._token(email, role, None, customer, None)}


def _get(client, key, cookies=None, **params):
    params = {"start_ms": START, "end_ms": END, **params}
    return client.get(f"/api/customer/analytics/{key}/events", params=params, cookies=cookies or _cookie())


def test_smart_motion_rows_summary_and_media_refs(client):
    body = _get(client, "smart_motion").json()
    assert [e["event_id"] for e in body["events"]] == ["sm-3", "sm-2", "sm-1"]  # newest first, range-limited
    first_clip = body["events"][2]
    assert (first_clip["has_clip"], first_clip["has_thumbnail"], first_clip["confidence"]) == (True, True, 0.54)
    assert body["summary"]["total"] == 3 and body["summary"]["by_type"] == {"person": 1, "car": 1, "motion": 1}
    assert body["enabled_camera_ids"] == ["cam-a"]


def test_other_tenants_events_never_appear_and_their_cameras_are_404(client):
    ids = [e["event_id"] for e in _get(client, "smart_motion").json()["events"]]
    assert "sm-other" not in ids
    assert _get(client, "smart_motion", camera_id="cam-x").status_code == 404
    assert _get(client, "smart_motion", camera_id="cam-a,cam-x").status_code == 404
    other = _get(client, "smart_motion", cookies=_cookie("owner@c2.test", customer="cust-2")).json()
    assert [e["event_id"] for e in other["events"]] == ["sm-other"]


def test_viewer_only_sees_permitted_cameras(client):
    viewer = _cookie("viewer@c1.test", "customer_viewer")
    ids = [e["event_id"] for e in _get(client, "smart_motion", cookies=viewer).json()["events"]]
    assert ids == ["sm-3", "sm-1"]
    assert _get(client, "smart_motion", cookies=viewer, camera_id="cam-b").status_code == 404


def test_result_filters_use_stored_values(client):
    assert [e["event_id"] for e in _get(client, "smart_motion", result="vehicle").json()["events"]] == ["sm-2"]
    assert [e["event_id"] for e in _get(client, "people_counting", result="out").json()["events"]] == ["pc-3"]
    violations = _get(client, "ppe", result="violation").json()["events"]
    assert [e["event_id"] for e in violations] == ["ppe-1"]
    assert violations[0]["details"] == {"status": "violation", "hard_hat": False, "vest": True}
    assert violations[0]["confidence"] is None  # PPE's stored 0.0 is not a real confidence
    known = _get(client, "facial_recognition", result="known").json()["events"]
    assert [e["event_id"] for e in known] == ["fr-1"] and known[0]["details"]["person"] == "Ana"
    assert _get(client, "smart_motion", result="bogus").status_code == 400


def test_people_counting_totals_per_camera_and_hourly_trend(client):
    summary = _get(client, "people_counting").json()["summary"]
    assert summary["by_type"] == {"people_counting_in": 2, "people_counting_out": 1}
    assert summary["per_camera"] == {"cam-a": {"in": 2, "out": 0}, "cam-b": {"in": 0, "out": 1}}
    assert [(h["in"], h["out"]) for h in summary["hourly"]] == [(2, 0), (0, 1)]
    assert all(e["confidence"] is None for e in _get(client, "people_counting").json()["events"])


def test_ppe_and_face_summaries_count_results(client):
    assert _get(client, "ppe").json()["summary"]["by_result"] == {"violation": 1, "compliant": 1}
    assert _get(client, "facial_recognition").json()["summary"]["by_result"] == {"known": 1, "unknown": 1}


def test_pagination_and_range_validation(client, monkeypatch):
    import customer_analytics_workspace as ws
    monkeypatch.setattr(ws, "PAGE_SIZE", 2)
    first = ws.query_events(customer_id="cust-1", camera_ids=["cam-a", "cam-b"], key="smart_motion", start_ms=START, end_ms=END, limit=2)
    assert [e["event_id"] for e in first["events"]] == ["sm-3", "sm-2"] and first["next_before"] == "2026-09-25T11:00:00"
    rest = ws.query_events(customer_id="cust-1", camera_ids=["cam-a", "cam-b"], key="smart_motion", start_ms=START, end_ms=END,
                           before=first["next_before"], limit=2)
    assert [e["event_id"] for e in rest["events"]] == ["sm-1"] and rest["next_before"] is None
    assert _get(client, "smart_motion", end_ms=START).status_code == 400
    assert _get(client, "smart_motion", end_ms=START + 200 * DAY_MS).status_code == 400
    assert _get(client, "nonsense").status_code == 404


def test_unauthenticated_api_is_refused(client):
    assert client.get("/api/customer/analytics/smart_motion/events", params={"start_ms": START, "end_ms": END}).status_code in (401, 403, 303)


def test_customer_page_renders_workspace_with_nav_and_camera_preselect(client):
    response = client.get("/analytics?type=ppe&camera=cam-b", cookies=_cookie())
    assert response.status_code == 200
    html = response.text
    assert "view_analytics" not in html and 'id="analytics-tabs"' in html
    assert 'data-tab="ppe" aria-selected="true"' in html
    assert '<option value="cam-b" selected>' in html and 'value="cam-x"' not in html
    assert 'href="/analytics"' in html  # sidebar entry for customers
    # A camera the caller cannot see is ignored, never preselected.
    assert '<option value="cam-x" selected>' not in client.get("/analytics?camera=cam-x", cookies=_cookie()).text


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_workspace_script_is_valid_javascript(client, tmp_path):
    html = client.get("/analytics", cookies=_cookie()).text
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    script = next(s for s in scripts if "analytics-tabs" in s)
    path = tmp_path / "analytics.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_motion_scores_and_placeholders_are_never_shown_as_confidence(client):
    events = {e["event_id"]: e for e in _get(client, "smart_motion").json()["events"]}
    assert events["sm-3"]["confidence"] is None  # stored 25.4 would render as "2540%"
    assert events["sm-2"]["confidence"] == 0.9


def test_hidden_filters_stay_hidden(client):
    html = client.get("/analytics", cookies=_cookie()).text
    assert ".analytics-filter[hidden]{display:none}" in html
    assert 'id="analytics-from-wrap" hidden' in html and 'id="analytics-to-wrap" hidden' in html


def test_face_scores_are_labelled_as_match_only_for_recognized_people(client):
    html = client.get("/analytics", cookies=_cookie()).text
    assert "d.state==='known'&&e.confidence!=null?`${Math.round(e.confidence*100)}% match`:null" in html
