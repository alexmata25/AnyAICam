"""License Plates as a structured table (2026-09-30).

Each plate read is one row: time, camera, plate text, cropped plate image,
vehicle make, model, colour/type and its clip. Before this:
- the appliance never sent plate fields to the cloud at all (plate events
  synced with detections=None), so the customer page could not show the
  plate text ("Plate text is kept on the appliance");
- plate events were timed when the scan finished, so their clip could miss
  the vehicle; they are now timed from the frames (first read);
- nothing described the vehicle. Type comes from the detector; colour only
  when one clearly dominates; make/model stay Unknown (no classifier).
Older reads without these fields still render, as "Unknown"/"Not recorded"."""
import base64
import json
import sqlite3
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

import analytics_sync
import detection_timing as dt
import lpr
import lpr_vehicle
from test_customer_analytics_workspace import _cookie, _event, _get, _ids, _other, client, db_path  # noqa: F401

JPEG = bytes((0xFF, 0xD8, 0xFF, 0xE0)) + b"plate-jpeg-bytes"
JPEG_B64 = base64.b64encode(JPEG).decode()


def _record(**overrides):
    record = {"plate": "KXT4821", "plate_confidence": 0.94, "vehicle_type": "car", "vehicle_color": "White",
              "vehicle_color_confidence": 0.82, "vehicle_make": None, "vehicle_model": None,
              "vehicle_make_model_confidence": None, "plate_crop_jpeg": JPEG_B64}
    record.update(overrides)
    return json.dumps([record])


# ------------------------------------------------------------ vehicle details (edge)

def _solid(bgr, size=(120, 200)):
    image = np.zeros((*size, 3), dtype=np.uint8)
    image[:] = bgr
    return image


def test_a_clearly_red_vehicle_reads_red():
    color, share = lpr_vehicle.estimate_vehicle_color(_solid((30, 30, 200)))
    assert color == "Red" and share >= lpr_vehicle.VEHICLE_ATTRIBUTE_MIN_CONFIDENCE


def test_ir_night_frames_never_get_a_colour():
    gray = _solid((128, 128, 128))
    assert lpr_vehicle.estimate_vehicle_color(gray) == (None, 0.0)


def test_no_dominant_colour_is_unknown_not_a_guess():
    mixed = _solid((30, 30, 200))
    mixed[:, 100:] = (200, 60, 30)  # half red, half blue
    color, share = lpr_vehicle.estimate_vehicle_color(mixed)
    assert color is None and share < lpr_vehicle.VEHICLE_ATTRIBUTE_MIN_CONFIDENCE


def test_tiny_or_missing_crops_are_unknown():
    assert lpr_vehicle.estimate_vehicle_color(_solid((30, 30, 200), size=(10, 10)))[0] is None
    assert lpr_vehicle.estimate_vehicle_color(None)[0] is None


def test_vehicle_type_is_the_detector_class_of_the_plate_box():
    detections = [{"x": 1, "y": 2, "width": 30, "height": 20, "class_name": "person"},
                  {"x": 10, "y": 20, "width": 300, "height": 200, "class_name": "truck"}]
    assert lpr_vehicle.vehicle_type_for((10, 20, 300, 200), detections) == "truck"
    assert lpr_vehicle.vehicle_type_for((1, 2, 30, 20), detections) is None  # not a vehicle
    assert lpr_vehicle.vehicle_type_for(None, detections) is None  # full-frame read: no vehicle box


# ------------------------------------------------------------ timing (edge)

def test_a_confirmed_plate_reports_when_it_was_first_read():
    lpr.reset_pipeline_state()
    read = {"plate_number": "KXT4821", "confidence": 95.0}
    confirmed, t = None, 100.0
    while confirmed is None and t < 100.0 + 4 * lpr.LPR_CONFIRM_READS:
        confirmed = lpr.confirm_plate(5, read, now=t)
        t += 4.0
    assert confirmed is not None and lpr.LPR_CONFIRM_READS > 1
    confirmed_at = 100.0 + 4.0 * (lpr.LPR_CONFIRM_READS - 1)
    assert confirmed["first_read_age_seconds"] == pytest.approx(confirmed_at - 100.0)
    lpr.reset_pipeline_state()


def test_plate_event_starts_before_the_first_read():
    frame = datetime(2026, 9, 30, 16, 0, 12)
    start, first_read, moment = dt.plate_event_span(frame, frame - timedelta(seconds=5), now=frame + timedelta(seconds=8),
                                                    first_read_age_seconds=10, scan_interval_seconds=5)
    assert moment == frame and first_read == frame - timedelta(seconds=10)
    assert start == first_read - timedelta(seconds=5)
    capped = dt.plate_event_span(frame, None, now=frame, first_read_age_seconds=500, scan_interval_seconds=5)[0]
    assert capped == frame - timedelta(seconds=dt.MAX_EVENT_LEAD_SECONDS)


# ------------------------------------------------------------ run_lpr_scan (edge)

def test_plate_event_is_timed_from_its_reads_and_describes_its_vehicle(monkeypatch, tmp_path):
    import main
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    frame[100:300, 100:400] = (30, 30, 200)  # a red car
    box = (100, 100, 300, 200)
    detected_at = datetime.now() - timedelta(seconds=7)
    plate = {"plate_number": "KXT4821", "confidence": 94.0, "region": (5, 5, 40, 12), "crop": frame[100:300, 100:400],
             "vehicle_box": box, "first_read_age_seconds": 6.0}
    monkeypatch.setattr(main.lpr, "LPR_ENABLED", True)
    monkeypatch.setattr(main.lpr, "is_camera_enabled", lambda camera: True)
    monkeypatch.setattr(main.lpr, "LPR_FULL_RES_FRAMES", False)
    monkeypatch.setattr(main.lpr, "scan_frame", lambda *a, **k: [dict(plate)])
    monkeypatch.setattr(main.recording_uploader, "_camera_identity", lambda camera: {"lpr_enabled": True})
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path)
    saved, linked, scheduled = [], [], []
    monkeypatch.setattr(main, "append_analytics_event", lambda event: saved.append(event))
    monkeypatch.setattr(main, "linked_recording_for", lambda camera, at, *a, **k: linked.append(at))
    monkeypatch.setattr(main, "_schedule_owned_analytics_clip", lambda *a: scheduled.append(a))
    monkeypatch.setattr(main.event_media_sharing, "should_build_own_clip", lambda camera: True)
    main.event_media_sharing.owners.reset()
    result = {"frame": frame, "frame_captured_at": detected_at, "previous_frame_captured_at": detected_at - timedelta(seconds=5),
              "detections": [{"x": 100, "y": 100, "width": 300, "height": 200, "class_name": "car", "confidence": 0.9}]}
    events = main.run_lpr_scan(9, result)
    main.event_media_sharing.owners.reset()
    event = events[0]
    first_read = detected_at - timedelta(seconds=6)
    assert event["timestamp"] == first_read.isoformat()
    assert linked == [detected_at]
    assert (event["vehicle_type"], event["vehicle_color"]) == ("car", "Red")
    assert event["vehicle_make"] is None and event["vehicle_model"] is None
    event_id, camera, moment, _thumb, start = scheduled[0]
    assert (event_id, camera, moment) == (event["id"], 9, detected_at)
    assert start <= first_read - timedelta(seconds=5)  # the clip begins before the vehicle's first read
    assert event["plate_crop"] and (tmp_path / event["plate_crop"].split("/")[-2] / event["plate_crop"].split("/")[-1]).is_file()


# ------------------------------------------------------------ sync payload (edge -> cloud)

def test_plate_events_now_send_their_plate_and_vehicle(monkeypatch, tmp_path):
    monkeypatch.setattr(analytics_sync, "RECORDINGS_FOLDER", tmp_path)
    crop = tmp_path / "media" / "ai" / "2026-09-30" / "camera9_16-00-06_plate_abc.jpg"
    crop.parent.mkdir(parents=True)
    crop.write_bytes(JPEG)
    payload = analytics_sync._build_payload({
        "id": "abc", "event_type": "plate", "timestamp": "2026-09-30T16:00:06", "confidence": 0.94, "plate_number": "KXT4821",
        "plate_crop": "/recordings/media/ai/2026-09-30/camera9_16-00-06_plate_abc.jpg", "vehicle_type": "car",
        "vehicle_color": "Red", "vehicle_color_confidence": 0.91, "vehicle_make": None, "vehicle_model": None,
        "vehicle_make_model_confidence": None, "thumbnail": "/recordings/media/ai/x.jpg", "linked_recording": "/x"})
    record = payload["detections"][0]
    assert record["plate"] == "KXT4821" and record["vehicle_type"] == "car" and record["vehicle_color"] == "Red"
    assert base64.b64decode(record["plate_crop_jpeg"]) == JPEG
    assert "thumbnail" not in json.dumps(payload) and "linked_recording" not in json.dumps(payload)


def test_older_local_plate_events_and_bad_paths_degrade_to_none(monkeypatch, tmp_path):
    monkeypatch.setattr(analytics_sync, "RECORDINGS_FOLDER", tmp_path)
    record = analytics_sync._build_payload({"id": "old", "event_type": "plate", "timestamp": "2026-09-01T10:00:00",
                                            "plate_number": "OLD1", "plate_crop": "/recordings/../../etc/passwd"})["detections"][0]
    assert record["plate"] == "OLD1"
    assert record["plate_crop_jpeg"] is None and record["vehicle_color"] is None and record["vehicle_type"] is None


# ------------------------------------------------------------ cloud table

def _seed(db_path, *rows):
    conn = sqlite3.connect(db_path)
    for row in rows:
        _event(conn, *row)
    conn.commit()
    conn.close()


def test_each_row_carries_the_table_fields_with_unknowns_as_none(client, db_path):  # noqa: F811
    _seed(db_path, ("lpr-full", "cust-1", "cam-a", "plate", "2026-09-25T06:00:00", 0.94, _record(), True, True))
    row = {e["event_id"]: e for e in _get(client, "lpr").json()["events"]}["lpr-full"]
    assert row["details"] == {"plate": "KXT4821", "has_plate_image": True, "vehicle_type": "Car", "vehicle_color": "White",
                              "vehicle_make": None, "vehicle_model": None}
    assert row["has_clip"] is True and row["camera_id"] == "cam-a"


def test_low_confidence_vehicle_details_are_unknown(client, db_path):  # noqa: F811
    _seed(db_path,
          ("lpr-low", "cust-1", "cam-a", "plate", "2026-09-25T06:10:00", 0.9,
           _record(vehicle_color="Blue", vehicle_color_confidence=0.41, vehicle_make="Toyota", vehicle_model="Camry",
                   vehicle_make_model_confidence=0.3)),
          ("lpr-sure", "cust-1", "cam-a", "plate", "2026-09-25T06:20:00", 0.9,
           _record(vehicle_make="Toyota", vehicle_model="Camry", vehicle_make_model_confidence=0.88)))
    rows = {e["event_id"]: e["details"] for e in _get(client, "lpr").json()["events"]}
    assert (rows["lpr-low"]["vehicle_color"], rows["lpr-low"]["vehicle_make"], rows["lpr-low"]["vehicle_model"]) == (None, None, None)
    assert (rows["lpr-sure"]["vehicle_make"], rows["lpr-sure"]["vehicle_model"]) == ("Toyota", "Camry")


def test_historical_reads_still_render(client):  # noqa: F811
    rows = {e["event_id"]: e["details"] for e in _get(client, "lpr").json()["events"]}
    assert rows["lpr-1"] == {"plate": "ABC123", "has_plate_image": False, "vehicle_type": None, "vehicle_color": None,
                             "vehicle_make": None, "vehicle_model": None}


def test_plate_search_matches_the_plate_not_the_image_data(client, db_path):  # noqa: F811
    _seed(db_path, ("lpr-img", "cust-1", "cam-a", "plate", "2026-09-25T06:30:00", 0.9, _record(plate="QRS1")))
    needle = JPEG_B64[2:8]
    assert needle.upper() not in "QRS1ABC123XYZ9"
    response = _get(client, "lpr", q=needle).json()
    assert response["events"] == [] and response["summary"]["total"] == 0
    assert _ids(_get(client, "lpr", q="qrs")) == ["lpr-img"]


def test_plate_image_is_served_only_to_its_own_customer(client, db_path):  # noqa: F811
    _seed(db_path,
          ("lpr-pic", "cust-1", "cam-a", "plate", "2026-09-25T06:40:00", 0.9, _record()),
          ("lpr-unent", "cust-1", "cam-c", "plate", "2026-09-25T06:40:00", 0.9, _record()),
          ("lpr-bad", "cust-1", "cam-a", "plate", "2026-09-25T06:41:00", 0.9, _record(plate_crop_jpeg="bm90IGEganBlZw==")))
    url = "/api/customer/analytics/lpr/{}/plate-image"
    ok = client.get(url.format("lpr-pic"), cookies=_cookie())
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/jpeg" and ok.content == JPEG
    assert client.get(url.format("lpr-pic"), cookies=_other()).status_code == 404          # another tenant
    assert client.get(url.format("lpr-unent"), cookies=_cookie()).status_code == 404       # camera without LPR
    assert client.get(url.format("lpr-1"), cookies=_cookie()).status_code == 404           # older read: no image
    assert client.get(url.format("lpr-bad"), cookies=_cookie()).status_code == 404         # not a JPEG
    assert client.get(url.format("sm-1"), cookies=_cookie()).status_code == 404            # not a plate event
    assert client.get(url.format("lpr-pic")).status_code in (401, 403)                     # signed out


def test_edge_payload_round_trips_into_a_table_row(client, db_path, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(analytics_sync, "RECORDINGS_FOLDER", tmp_path)
    payload = analytics_sync._build_payload({"id": "rt", "event_type": "plate", "timestamp": "2026-09-25T06:50:00",
                                             "confidence": 0.9, "plate_number": "RT42", "vehicle_type": "bus"})
    _seed(db_path, ("lpr-rt", "cust-1", "cam-a", "plate", payload["event_timestamp"], 0.9, json.dumps(payload["detections"])))
    row = {e["event_id"]: e for e in _get(client, "lpr").json()["events"]}["lpr-rt"]
    assert row["details"] == {"plate": "RT42", "has_plate_image": False, "vehicle_type": "Bus", "vehicle_color": None,
                              "vehicle_make": None, "vehicle_model": None}


def test_the_page_renders_a_table_with_every_column(client):  # noqa: F811
    html = client.get("/analytics/lpr", cookies=_cookie()).text
    assert ".aw-lpr-row" in html and "Every plate read, with the vehicle it was read on and its clip." in html
    script = (Path(__file__).resolve().parents[1] / "static" / "analytics_workspace.js").read_text(encoding="utf-8")
    assert "['Time','Camera','Plate','Plate image','Make','Model','Color / type','Clip']" in script
    assert 'Unknown' in script and 'Not recorded' in script
    assert "Plate text is kept on the appliance" not in script  # plate text is synced now
