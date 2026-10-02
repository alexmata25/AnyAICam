"""Customer Dashboard numbers are the customer's own (2026-09-25).
/api/dashboard/intelligence reads appliance-local legacy files with no
tenant scoping, so on the cloud portal every customer saw the same
'0 events' and stray 'Motion detected on Camera 1' alerts linking to the
legacy /camera/1. Portal customers now use
/api/customer/dashboard/intelligence: their permitted cameras' detection
events for the viewer's local day and their own notifications."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database

DAY_START = 1790312400000  # 2026-09-25 00:00 America/Chicago (05:00 UTC)
DAY_END = DAY_START + 86400000


@pytest.fixture()
def client(tmp_path):
    db_path = tmp_path / "dash.db"
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO partners(id,name,created_at) VALUES('p1','P','x')")
        for customer in ("cust-1", "cust-2"):
            conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
                         (customer, "p1", customer, f"{customer}@e.test", "active", "x"))
            conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"s-{customer}", customer, "Home", "x"))
            conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (f"u-{customer}", "p1", f"owner@{customer}.test", "O", "customer_owner", "x", 1, customer, "x", "active", "all"))
        for cid, customer, name, number in (("cam-a", "cust-1", "Porch", 1), ("cam-b", "cust-1", "Yard", 2), ("cam-x", "cust-2", "Other", 1)):
            conn.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES(?,?,?,?,?,?,?)",
                         (cid, customer, f"s-{customer}", name, "configured", number, "x"))
        events = [("e1", "cust-1", "cam-a", "person", "2026-09-25T06:10:00"),  # 01:10 local
                  ("e2", "cust-1", "cam-a", "car", "2026-09-25T06:40:00"),
                  ("e3", "cust-1", "cam-b", "truck", "2026-09-25T20:00:00"),  # 15:00 local
                  ("e4", "cust-1", "cam-a", "person", "2026-09-25T04:59:00"),  # previous local day
                  ("e5", "cust-2", "cam-x", "person", "2026-09-25T10:00:00")]
        for eid, customer, cam, kind, ts in events:
            conn.execute("INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,confidence,"
                         "object_count,detections_json,event_timestamp,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (eid, customer, f"s-{customer}", "app", cam, eid, kind, 0.9, 1, None, ts, ts))
        conn.execute("INSERT INTO notifications(id,user_id,customer_id,site_id,camera_id,event_id,recording_id,event_type,severity,title,message,timestamp,thumbnail,created_at) "
                     "VALUES('n1','u-cust-1','cust-1','s-cust-1','cam-a','e1',NULL,'ppe','info','Ppe','Ppe detected','2026-09-25T06:10:00',NULL,'x')")
        conn.execute("INSERT INTO notifications(id,user_id,customer_id,site_id,camera_id,event_id,recording_id,event_type,severity,title,message,timestamp,thumbnail,created_at) "
                     "VALUES('n2','u-cust-2','cust-2','s-cust-2','cam-x','e5',NULL,'person','info','Person','Person detected','2026-09-25T10:00:00',NULL,'x')")
        conn.commit()
        conn.close()
        with TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client


def _cookie(customer="cust-1"):
    return {partner_portal.SESSION_COOKIE: partner_portal._token(f"owner@{customer}.test", "customer_owner", None, customer, None)}


def _get(client, customer="cust-1", **params):
    return client.get("/api/customer/dashboard/intelligence", params={"start_ms": DAY_START, "end_ms": DAY_END, **params},
                      cookies=_cookie(customer))


def test_counts_are_this_customers_local_day(client):
    data = _get(client).json()
    assert data["events_today"] == 3  # e4 is the previous local day; e5 is another customer
    assert data["analytics"] == {"person": 1, "vehicle": 2, "plate": 0, "intrusion": 0}
    assert data["hourly_activity"][1] == 2 and data["hourly_activity"][15] == 1 and sum(data["hourly_activity"]) == 3
    assert (data["most_active_camera"], data["most_active_camera_label"], data["most_active_camera_events"]) == ("cam-a", "Porch", 2)


def test_alerts_are_the_customers_own_notifications_with_real_links(client):
    data = _get(client).json()
    assert data["unread_alert_count"] == 1
    alert = data["alerts"][0]
    assert alert["type"] == "PPE" and alert["message"] == "PPE detected · Porch"
    assert alert["href"].startswith("/playback?camera=cam-a&event=e1")
    assert alert["timestamp"].endswith("+00:00")  # unambiguous UTC for the browser
    assert "/camera/" not in str(data)


def test_other_tenant_sees_only_their_own(client):
    data = _get(client, customer="cust-2").json()
    assert data["events_today"] == 1 and data["most_active_camera_label"] == "Other"
    assert [a["type"] for a in data["alerts"]] == ["Person"]


def test_bad_ranges_and_non_customers_are_refused(client):
    assert _get(client, end_ms=DAY_START).status_code == 400
    assert _get(client, end_ms=DAY_START + 5 * 86400000).status_code == 400
    response = client.get("/api/customer/dashboard/intelligence", params={"start_ms": DAY_START, "end_ms": DAY_END})
    assert response.status_code in (401, 403, 303)


def test_customer_dashboard_uses_the_scoped_route(client):
    html = client.get("/dashboard", cookies=_cookie()).text
    assert "window.__anyaicamCustomerDashboard=true;" in html
    assert "/api/customer/dashboard/intelligence?start_ms=" in html
    assert ">Porch<" in html or "Porch" in html  # camera names, not "Camera 1"
