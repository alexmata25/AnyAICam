"""Camera detail Analytics summaries carry what the customer view needs
(2026-09-25): each recent result's stored event id, an epoch-ms time (so
the browser shows viewer-local time, never a raw naive-UTC ISO string),
and whether the event already has a clip / thumbnail (the same
detection_event_media join Events and the Dashboard use). Nothing new is
stored; tenant scoping is unchanged. Also: assigning an analytic with no
purchased licenses explains itself instead of "licensed for 0 camera(s)"."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

import customer_analytics_panel as cap
import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database

T = "2026-09-25T18:08:49.232478"
T_MS = 1790359729232


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "analytics_refs.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        for customer in ("cust-1", "cust-2"):
            conn.execute("INSERT INTO partners(id,name,created_at) VALUES(?,?,?) ON CONFLICT DO NOTHING", ("p1", "P", "2026-01-01"))
            conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
                         (customer, "p1", customer, f"{customer}@example.test", "active", "2026-01-01"))
            conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{customer}", customer, "Main", "2026-01-01"))
            conn.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES(?,?,?,?,?,?,?)",
                         (f"cam-{customer}", customer, f"site-{customer}", "Front", "configured", 1, "2026-01-01"))
            conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (f"u-{customer}", "p1", f"owner@{customer}.test", "Owner", "customer_owner", "x", 1,
                                                          customer, "2026-01-01", "active", "all"))
        rows = [("evt-clip", "cust-1", "person", 0.54, T), ("evt-bare", "cust-1", "car", None, "2026-09-25T18:01:10"),
                ("evt-other", "cust-2", "person", 0.9, T)]
        for event_id, customer, kind, confidence, ts in rows:
            conn.execute("INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,confidence,"
                         "object_count,detections_json,event_timestamp,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (event_id, customer, f"site-{customer}", "app", f"cam-{customer}", event_id, kind, confidence, 1, None, ts, ts))
        conn.execute("INSERT INTO detection_event_media(id,detection_event_id,customer_id,camera_id,s3_key,thumbnail_s3_key,started_at,ended_at,created_at) "
                     "VALUES('m1','evt-clip','cust-1','cam-cust-1','events/a.mp4','events/a.jpg',?,?,?)", (T, T, T))
        conn.execute("INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) "
                     "VALUES('cam-cust-1','smart_motion','active','2026-01-01','2026-01-01')")
        conn.commit()
        conn.close()
        with TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client


def _cookie(customer_id="cust-1"):
    return {partner_portal.SESSION_COOKIE: partner_portal._token(f"owner@{customer_id}.test", "customer_owner", None, customer_id, None)}


def test_smart_motion_summary_carries_event_refs(client):
    response = client.get("/api/customer/cameras/cam-cust-1/analytics/smart_motion/summary", cookies=_cookie())
    assert response.status_code == 200
    body = response.json()
    assert body["latest_timestamp_ms"] == T_MS
    first, second = body["recent"]
    assert first == {"event_type": "person", "timestamp": T, "confidence": 0.54, "thumbnail": None,
                     "event_id": "evt-clip", "timestamp_ms": T_MS, "has_clip": True, "has_thumbnail": True}
    assert (second["event_id"], second["has_clip"], second["has_thumbnail"], second["confidence"]) == ("evt-bare", False, False, None)


def test_summary_stays_tenant_scoped(client):
    response = client.get("/api/customer/cameras/cam-cust-1/analytics/smart_motion/summary", cookies=_cookie("cust-2"))
    assert response.status_code == 404  # another tenant's camera does not exist for them
    own = client.get("/api/customer/cameras/cam-cust-2/analytics/smart_motion/summary", cookies=_cookie("cust-2")).json()
    assert [item["event_id"] for item in own["recent"]] == ["evt-other"]


def test_epoch_ms_treats_naive_timestamps_as_utc():
    assert cap.epoch_ms(T) == T_MS
    assert cap.epoch_ms(T + "Z") == T_MS
    assert cap.epoch_ms("2026-09-25T13:08:49.232478-05:00") == T_MS
    assert cap.epoch_ms("not a time") is None and cap.epoch_ms(None) is None


def test_every_summarizer_exposes_event_refs():
    row = {"id": "e1", "event_type": "plate", "event_timestamp": T, "confidence": 0.8, "object_count": 1,
           "detections_json": None, "has_clip": 1, "has_thumbnail": 0}
    for key in cap.SUMMARIZERS:
        item = cap.summarize(key, [dict(row)])["recent"][0]
        assert (item["event_id"], item["timestamp_ms"], item["has_clip"], item["has_thumbnail"]) == ("e1", T_MS, True, False), key


def test_adding_an_unpurchased_analytic_explains_itself(client):
    response = client.post("/api/customer/cameras/cam-cust-1/analytics/lpr", cookies=_cookie(),
                           headers={"x-csrf-token": "t"})
    if response.status_code == 403 and "CSRF" in response.text:
        pytest.skip("CSRF enforced in this configuration; covered by the direct call below")
    assert response.status_code == 409
    assert "not part of your plan" in response.json()["detail"]


def test_zero_license_message_directly(db_path, client):
    with override_target(sqlite_path=db_path):
        from partner_db import connection
        with connection() as db, pytest.raises(cap.LicenseLimitExceeded, match="not part of your plan for this site yet"):
            cap.assign_entitlement(db, "cam-cust-1", "lpr", now="2026-09-25T00:00:00")


def test_real_confidence_rejects_scores_and_placeholders():
    assert cap.real_confidence("person", 0.54) == 0.54
    assert cap.real_confidence("motion", 0.5) is None
    assert cap.real_confidence("car", 25.4) is None
    assert cap.real_confidence("ppe", 0.0) is None
    assert cap.summarize("smart_motion", [{"id": "m", "event_type": "motion", "confidence": 18.5, "event_timestamp": T}])["recent"][0]["confidence"] is None
