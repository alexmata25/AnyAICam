"""Customer exclusion ("ignore detections in this area") zones (2026-09-26):
drawn and stored like intrusion zones (customer_analytics_rules.py, rule_type
"exclusion"), mirrored to the edge like every rule (edge_camera_sync.py), and
enforced at the source there (detection_exclusion.py): detect_objects_frame()
drops detections centred inside, and pixel motion ignores changes inside --
only on a camera that has such a zone."""

import json
import sqlite3
import types

import numpy as np
import pytest

import customer_analytics_rule_worker
import customer_analytics_rules
import detection_exclusion
import edge_camera_sync
import main
import partner_portal
from database_backend import override_target
from partner_db import connection, initialize_database

LEFT_HALF = [{"x": 0.0, "y": 0.0}, {"x": 0.5, "y": 0.0}, {"x": 0.5, "y": 1.0}, {"x": 0.0, "y": 1.0}]


def _seed(conn):
    conn.execute("INSERT INTO partners(id,name,created_at) VALUES('p1','P','x')")
    for n in ("1", "2"):
        conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)", (f"cust-{n}", "p1", n, f"c{n}@example.test", "active", "x"))
        conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{n}", f"cust-{n}", "Main", "x"))
        conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", (f"app-{n}", f"cust-{n}", f"site-{n}", f"cloud-{n}", "x"))
    for camera, n, number in (("cam-1", "1", 1), ("cam-2", "1", 2), ("cam-3", "2", 1)):
        conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,camera_number,created_at) VALUES(?,?,?,?,?,?,?,?)",
                     (camera, f"cust-{n}", f"site-{n}", f"app-{n}", camera, "configured", number, "x"))
    conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
                 "VALUES('o1','p1','o1@example.test','O','customer_owner','x',1,'cust-1','x','active','all')")
    conn.execute("INSERT INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) VALUES('v1','v1@example.test','customer_viewer','cust-1','x','custom','x')")
    conn.execute("INSERT INTO customer_camera_permissions(user_id,camera_id,can_live,can_playback,can_settings) VALUES('v1','cam-1',1,0,0)")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "exclusion.db"
    with override_target(sqlite_path=path):
        initialize_database()
        conn = sqlite3.connect(path)
        _seed(conn)
        conn.commit()
        conn.close()
        detection_exclusion.reset_cache()
        identities = {1: {"camera_id": "cam-1"}, 2: {"camera_id": "cam-2"}}
        monkeypatch.setattr(main.recording_uploader, "_camera_identity", lambda number: identities.get(number))
        yield path
        detection_exclusion.reset_cache()


def _client(email, role, customer):
    from fastapi.testclient import TestClient
    client = TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False)
    client.cookies.set(partner_portal.SESSION_COOKIE, partner_portal._token(email, role, None, customer, None))
    return client


def _add_zone(camera="cam-1", geometry=LEFT_HALF, enabled=1, rule_id="zone-1", rule_type="exclusion", customer="cust-1"):
    with connection() as conn:
        conn.execute("INSERT INTO customer_analytics_rules(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,direction,geometry_json,enabled,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (rule_id, customer, "site-1", "app-1", camera, rule_type, "Tree", None, json.dumps(geometry), enabled, "x", "x"))
    detection_exclusion.reset_cache()


def _box(cx, cy, w=20, h=20, width=200, height=100, name="person"):
    """A detection centred at (cx, cy) as fractions of a 200x100 frame."""
    return {"class_name": name, "confidence": 0.9, "x": int(cx * width - w / 2), "y": int(cy * height - h / 2), "width": w, "height": h}


FRAME = np.zeros((100, 200, 3), dtype=np.uint8)


# ------------------------------------------------ drawing and storing (customer portal)

def test_customer_draws_an_exclusion_zone_with_the_same_editor_and_store(db):
    with _client("o1@example.test", "customer_owner", "cust-1") as owner:
        created = owner.post("/api/customer/cameras/cam-1/analytics-rules",
                             json={"rule_type": "exclusion", "name": "Swaying tree", "geometry": LEFT_HALF})
        assert created.status_code == 200, created.text
        assert created.json()["rule_type"] == "exclusion" and created.json()["direction"] is None
        listed = owner.get("/api/customer/cameras/cam-1/analytics-rules").json()["rules"]
        assert [r["rule_type"] for r in listed] == ["exclusion"]
        page = owner.get("/customer/cameras/cam-1/analytics-rules").text
        for label in ("Detect inside zone", "Line crossing", "Ignore detections in zone"):
            assert label in page
        # Same validation as any zone.
        assert owner.post("/api/customer/cameras/cam-1/analytics-rules",
                          json={"rule_type": "exclusion", "name": "x", "geometry": LEFT_HALF[:2]}).status_code == 400
        assert owner.post("/api/customer/cameras/cam-1/analytics-rules",
                          json={"rule_type": "exclusion", "name": "x", "geometry": LEFT_HALF, "direction": "both"}).status_code == 400
        # Another customer's camera is not found.
        assert owner.post("/api/customer/cameras/cam-3/analytics-rules",
                          json={"rule_type": "exclusion", "name": "x", "geometry": LEFT_HALF}).status_code == 404


def test_a_viewer_without_camera_settings_cannot_add_one(db):
    with _client("v1@example.test", "customer_viewer", "cust-1") as viewer:
        response = viewer.post("/api/customer/cameras/cam-1/analytics-rules",
                               json={"rule_type": "exclusion", "name": "x", "geometry": LEFT_HALF})
        assert response.status_code == 403


def test_exclusion_is_a_zone_type():
    assert "exclusion" in customer_analytics_rules.RULE_TYPES and "exclusion" in customer_analytics_rules.ZONE_RULE_TYPES
    assert "exclusion" not in customer_analytics_rules.LINE_RULE_TYPES


# ------------------------------------------------ enforcement at the source (edge)

def test_detections_centred_inside_the_zone_are_dropped_the_rest_kept(db):
    _add_zone()
    inside, outside, edge_straddler = _box(0.2, 0.5), _box(0.8, 0.5), _box(0.52, 0.5, w=60)
    kept = detection_exclusion.filter_detections(1, [inside, outside, edge_straddler], FRAME)
    assert kept == [outside, edge_straddler]  # judged by the centre, like an intrusion zone


def test_nothing_is_suppressed_without_a_configured_zone(db):
    detections = [_box(0.2, 0.5)]
    assert detection_exclusion.filter_detections(1, detections, FRAME) is detections
    _add_zone(camera="cam-2")  # another camera's zone
    assert detection_exclusion.filter_detections(1, detections, FRAME) == detections
    _add_zone(camera="cam-1", rule_id="off", enabled=0)  # disabled
    assert detection_exclusion.filter_detections(1, detections, FRAME) == detections
    _add_zone(camera="cam-1", rule_id="intrusion", rule_type="intrusion")  # a detection zone, not exclusion
    assert detection_exclusion.filter_detections(1, detections, FRAME) == detections
    _add_zone(camera="cam-1", rule_id="bad", geometry=[{"x": "?"}])  # malformed excludes nothing
    assert detection_exclusion.filter_detections(1, detections, FRAME) == detections
    assert detection_exclusion.filter_detections(99, detections, FRAME) == detections  # unknown camera


def test_a_removed_zone_stops_applying(db):
    _add_zone()
    assert detection_exclusion.filter_detections(1, [_box(0.2, 0.5)], FRAME) == []
    with connection() as conn:
        conn.execute("DELETE FROM customer_analytics_rules WHERE id='zone-1'")
    detection_exclusion.reset_cache()  # the real cache expires within CACHE_SECONDS
    assert len(detection_exclusion.filter_detections(1, [_box(0.2, 0.5)], FRAME)) == 1


def test_detect_objects_frame_applies_the_zone_for_every_downstream_analytic(db, monkeypatch, tmp_path):
    """The single YOLO call behind person/vehicle/Smart Motion, PPE, Facial
    Recognition, LPR, People Counting and Intrusion/Line Crossing."""
    _add_zone()
    for number in (1, 2):
        (tmp_path / f"camera{number}.m3u8").write_text("#EXTM3U")
    monkeypatch.setattr(main, "HLS_FOLDER", tmp_path)
    monkeypatch.setattr(main, "YOLO", object())

    class Capture:
        def __init__(self, *a): pass
        def set(self, *a): pass
        def read(self): return True, FRAME.copy()
        def release(self): pass

    monkeypatch.setattr(main.cv2, "VideoCapture", Capture)

    def box(x1, y1, x2, y2, cls=0):
        tensor = lambda values: [types.SimpleNamespace(item=lambda v=values: v, tolist=lambda v=values: v)]
        return types.SimpleNamespace(cls=tensor(cls), conf=tensor(0.9), xyxy=tensor([x1, y1, x2, y2]))

    result = types.SimpleNamespace(names={0: "person"}, boxes=[box(30, 40, 50, 60), box(150, 40, 170, 60)])
    monkeypatch.setattr(main, "get_yolo_model", lambda: types.SimpleNamespace(predict=lambda **k: [result]))
    detected = main.detect_objects_frame(1)
    assert detected["ok"] and [d["x"] for d in detected["detections"]] == [150]
    assert [d["x"] for d in main.detect_objects_frame(2)["detections"]] == [30, 150]  # no zone on camera 2


def test_pixel_motion_inside_the_zone_is_ignored(db):
    zones = [types.SimpleNamespace(x=0.0, y=0.0, width=1.0, height=1.0)]  # whole-frame motion zone
    previous = bytes(14400)

    def frame_with_change(column_range):
        current = np.zeros((90, 160), dtype=np.uint8)
        current[:, column_range] = 200
        return current.tobytes()

    left, right = frame_with_change(slice(10, 20)), frame_with_change(slice(140, 150))
    main._motion_zone_mask_cache.clear()
    before = main._compare_motion_frames(1, left, previous, zones)
    assert before[2] > 0  # changed pixels on the left, no zone yet
    _add_zone()
    ignored = main._compare_motion_frames(1, left, previous, zones)
    counted = main._compare_motion_frames(1, right, previous, zones)
    assert ignored[2] == 0 and counted[2] > 0
    assert ignored[1] == counted[1] < before[1]  # only the non-excluded half is compared
    mask = detection_exclusion.motion_exclusion_mask(1).reshape(90, 160)
    assert mask[:, :80].all() and not mask[:, 80:].any()
    assert detection_exclusion.motion_exclusion_mask(2) is None


def test_the_rule_worker_never_evaluates_an_exclusion_zone_as_an_alert(db):
    _add_zone()
    assert customer_analytics_rule_worker.load_rules_for_camera("cam-1") == []


def test_edge_sync_mirrors_and_removes_exclusion_zones(db):
    with connection() as conn:
        rule = {"id": "cloud-zone", "customer_id": "cust-1", "site_id": "site-1", "camera_id": "cam-1",
                "rule_type": "exclusion", "name": "Road", "direction": None, "geometry": LEFT_HALF, "updated_at": "x"}
        edge_camera_sync._reconcile_analytics_rules(conn, "app-1", [rule], "now")
    assert detection_exclusion.zones_for_camera(1)
    with connection() as conn:
        edge_camera_sync._reconcile_analytics_rules(conn, "app-1", [], "now")
    detection_exclusion.reset_cache()
    assert detection_exclusion.zones_for_camera(1) == ()
