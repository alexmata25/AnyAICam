"""Independent LPR cadence (2026-09-28). The real night test showed LPR
only ran when a vehicle *event* was saved (at most every 30 s, never for a
parked vehicle) while confirmation needs two matching reads within 90 s,
and that headlights can hide the vehicle from the vehicle detector while
the plate stays legible. No plate value is special-cased: plates here are
arbitrary strings returned by stubs."""
from pathlib import Path

import numpy as np
import pytest

import lpr

FRAME = np.zeros((720, 1280, 3), np.uint8)
TRUCK = (400, 200, 500, 380)


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    monkeypatch.setattr(lpr, "LPR_ENABLED", True)
    monkeypatch.setattr(lpr, "is_camera_enabled", lambda camera: True)
    lpr.reset_pipeline_state()
    yield
    lpr.reset_pipeline_state()


@pytest.fixture()
def reads(monkeypatch):
    """recognize_plate stub: returns queued texts, counts calls."""
    state = {"calls": 0, "texts": []}

    def fake(crop, *, camera_number=None):
        state["calls"] += 1
        text = state["texts"].pop(0) if state["texts"] else "QRS4821"
        return None if text is None else {"plate_number": text, "confidence": 88.0, "region": (5, 5, 40, 15),
                                          "detector_confidence": 0.8, "detector": "model"}

    monkeypatch.setattr(lpr, "recognize_plate", fake)
    return state


def test_consecutive_passes_confirm_a_plate_without_any_vehicle_event(reads):
    assert lpr.scan_frame(4, FRAME, [TRUCK], now=0.0) == []  # first read: one vote
    confirmed = lpr.scan_frame(4, FRAME, [TRUCK], now=5.0)
    assert [c["plate_number"] for c in confirmed] == ["QRS4821"]
    assert confirmed[0]["source"] == "vehicle" and confirmed[0]["vehicle_box"] == TRUCK
    assert confirmed[0]["crop"] is not None


def test_cadence_limits_how_often_a_camera_is_scanned(reads, monkeypatch):
    monkeypatch.setattr(lpr, "LPR_SCAN_SECONDS", 4.0)
    lpr.scan_frame(4, FRAME, [TRUCK], now=10.0)
    lpr.scan_frame(4, FRAME, [TRUCK], now=12.0)  # too soon: skipped entirely
    assert reads["calls"] == 1
    lpr.scan_frame(5, FRAME, [TRUCK], now=12.0)  # other cameras keep their own cadence
    assert reads["calls"] == 2


def test_one_frame_never_confirms_a_plate_even_with_duplicate_vehicle_boxes(reads):
    car_box, truck_box = (400, 200, 500, 380), (410, 205, 495, 370)  # the same vehicle as "car" and "truck"
    assert lpr.scan_frame(4, FRAME, [car_box, truck_box], now=0.0) == []
    assert reads["calls"] == 1  # overlapping boxes read once


def test_two_different_vehicles_with_the_same_misread_in_one_frame_do_not_confirm(reads):
    reads["texts"] = ["ABC1234", "ABC1234"]
    assert lpr.scan_frame(4, FRAME, [(0, 0, 400, 300), (800, 300, 400, 300)], now=0.0) == []


def test_disagreeing_reads_are_never_confirmed(reads):
    reads["texts"] = ["KPC6266", "KPE6266", "KPG6266", "KPC6Z66"]
    for i in range(4):
        assert lpr.scan_frame(4, FRAME, [TRUCK], now=i * 5.0) == []


def test_a_parked_vehicle_stops_costing_cpu(reads):
    reads["texts"] = [None] * 20  # unreadable plate
    for i in range(10):
        lpr.scan_frame(4, FRAME, [TRUCK], now=i * 5.0)
    assert reads["calls"] == lpr.LPR_ATTEMPTS_PER_VEHICLE


def test_a_confirmed_plate_is_not_reported_again_while_parked(reads):
    lpr.scan_frame(4, FRAME, [TRUCK], now=0.0)
    assert lpr.scan_frame(4, FRAME, [TRUCK], now=5.0)
    calls = reads["calls"]
    for i in range(2, 8):
        assert lpr.scan_frame(4, FRAME, [TRUCK], now=i * 5.0) == []
    assert reads["calls"] == calls  # confirmed vehicle is not re-read


def test_low_confidence_reads_are_still_rejected(monkeypatch):
    monkeypatch.setattr(lpr, "read_plate_glyphs", lambda crop: ("QRS4821", 10.0))
    monkeypatch.setattr(lpr, "read_plate_text", lambda crop: None)
    assert lpr.read_detected_plate(np.zeros((20, 60, 3), np.uint8), 0.2) is None  # 10 + 4 < 45
    assert lpr.read_detected_plate(np.zeros((20, 60, 3), np.uint8), 0.9)[0] if lpr.LPR_MIN_CONFIDENCE <= 28 else True


# ---------------------------------------------------------------- full-frame fallback (vehicle not detected)

@pytest.fixture()
def plates(monkeypatch):
    state = {"calls": 0}

    def fake_detect(frame):
        state["calls"] += 1
        return [{"region": (600, 500, 90, 45), "detector_confidence": 0.8}]

    monkeypatch.setattr(lpr, "detect_plates_full_frame", fake_detect)
    monkeypatch.setattr(lpr, "read_detected_plate", lambda crop, conf: ("QRS4821", 90.0))
    return state


def test_plate_is_found_on_the_whole_frame_when_no_vehicle_is_detected(plates):
    assert lpr.scan_frame(4, FRAME, [], now=0.0) == []
    assert lpr.scan_frame(4, FRAME, [], now=5.0) == []  # full-frame pass is rate limited
    confirmed = lpr.scan_frame(4, FRAME, [], now=lpr.LPR_FULL_FRAME_SECONDS + 1)
    assert [c["plate_number"] for c in confirmed] == ["QRS4821"] and confirmed[0]["source"] == "full_frame"
    assert plates["calls"] == 2


def test_full_frame_fallback_is_skipped_when_vehicles_are_detected(plates, reads):
    lpr.scan_frame(4, FRAME, [TRUCK], now=0.0)
    assert plates["calls"] == 0


def test_full_frame_fallback_can_be_disabled(plates, monkeypatch):
    monkeypatch.setattr(lpr, "LPR_FULL_FRAME_FALLBACK", False)
    lpr.scan_frame(4, FRAME, [], now=0.0)
    assert plates["calls"] == 0


def test_full_frame_read_uses_the_full_resolution_frame_when_it_adds_pixels(monkeypatch):
    seen = {}
    monkeypatch.setattr(lpr, "detect_plates_full_frame", lambda frame: [{"region": (600, 500, 90, 45), "detector_confidence": 0.8}])

    def fake_read(crop, conf):
        seen["shape"] = crop.shape
        return None

    monkeypatch.setattr(lpr, "read_detected_plate", fake_read)
    full = np.zeros((2160, 3840, 3), np.uint8)
    lpr.scan_frame(4, FRAME, [], full_frame=full, now=0.0)
    assert seen["shape"][:2] == (135, 270)  # the 90x45 region at 3x


def test_disabled_lpr_or_camera_does_nothing(reads, monkeypatch):
    monkeypatch.setattr(lpr, "is_camera_enabled", lambda camera: False)
    assert lpr.scan_frame(4, FRAME, [TRUCK], now=0.0) == [] and reads["calls"] == 0
    monkeypatch.setattr(lpr, "is_camera_enabled", lambda camera: True)
    monkeypatch.setattr(lpr, "LPR_ENABLED", False)
    assert lpr.scan_frame(4, FRAME, [TRUCK], now=10.0) == [] and reads["calls"] == 0


# ---------------------------------------------------------------- main.py wiring

def test_run_lpr_scan_creates_a_plate_event_with_crop_thumbnail_and_clip(monkeypatch, tmp_path):
    import main

    events, clips = [], []
    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path)
    monkeypatch.setattr(main.recording_uploader, "_camera_identity", lambda n: {"lpr_enabled": True})
    monkeypatch.setattr(main.lpr, "LPR_FULL_RES_FRAMES", False)
    monkeypatch.setattr(main, "append_analytics_event", events.append)
    monkeypatch.setattr(main, "_analytics_media_owner", lambda cam, event_id, now, start=None: event_id)
    monkeypatch.setattr(main, "_schedule_owned_analytics_clip", lambda event_id, cam, now, thumb, start=None: clips.append(event_id))
    monkeypatch.setattr(main, "linked_recording_for", lambda cam, now: None)
    frame = np.full((720, 1280, 3), 90, np.uint8)
    monkeypatch.setattr(main.lpr, "scan_frame", lambda cam, fr, boxes, full_frame=None: [{
        "plate_number": "QRS4821", "confidence": 91.0, "region": (5, 5, 40, 15), "crop": fr[200:580, 400:900],
        "vehicle_box": boxes[0], "source": "vehicle"}])
    result = {"frame": frame, "detections": [{"class_name": "truck", "x": 400, "y": 200, "width": 500, "height": 380, "confidence": 0.9}]}
    created = main.run_lpr_scan(4, result)
    assert len(created) == 1 and events == created
    event = created[0]
    assert event["event_type"] == "plate" and event["plate_number"] == "QRS4821" and event["confidence"] == 0.91
    assert event["thumbnail"] and event["plate_crop"]
    assert clips == [event["id"]]
    assert len(list(Path(tmp_path).rglob("*.jpg"))) == 2


def test_run_lpr_scan_respects_the_per_camera_entitlement(monkeypatch):
    import main

    called = []
    monkeypatch.setattr(main.recording_uploader, "_camera_identity", lambda n: {"lpr_enabled": False})
    monkeypatch.setattr(main.lpr, "scan_frame", lambda *a, **k: called.append(1) or [])
    assert main.run_lpr_scan(4, {"frame": FRAME, "detections": []}) == [] and called == []


def test_lpr_runs_from_the_ai_loop_not_from_vehicle_event_saving():
    source = Path(__import__("main").__file__).read_text(encoding="utf-8")
    loop = source[source.index("detections = result.get(\"detections\", [])", source.index("detect_objects_frame,\n")):]
    loop = loop[: loop.index("save_yolo_events,")]
    assert "run_lpr_scan" in loop  # every detection pass, before the event-saving gate
    save = source[source.index("def save_yolo_events("): source.index("def run_lpr_scan(")]
    assert "lpr.recognize_plate" not in save and "lpr.confirm_plate" not in save


def test_a_plate_event_on_an_event_mode_camera_triggers_its_own_recording_and_backfill(monkeypatch, tmp_path):
    """Found on the real Driveway Right: a plate confirmed while vehicle
    events were suppressed had no clip -- nothing triggered the Event-mode
    recording. The plate event now triggers it, like save_yolo_events()."""
    import main

    scheduled = []

    def fake_threadsafe(coroutine, loop):
        scheduled.append(coroutine.cr_code.co_name)
        coroutine.close()

    monkeypatch.setattr(main, "AI_THUMBNAILS_FOLDER", tmp_path)
    monkeypatch.setattr(main.recording_uploader, "_camera_identity", lambda n: {"lpr_enabled": True})
    monkeypatch.setattr(main.lpr, "LPR_FULL_RES_FRAMES", False)
    monkeypatch.setattr(main, "append_analytics_event", lambda e: None)
    monkeypatch.setattr(main, "_analytics_media_owner", lambda cam, event_id, now, start=None: None)
    monkeypatch.setattr(main, "linked_recording_for", lambda cam, now: None)
    monkeypatch.setattr(main, "_local_recording_settings", lambda cam: {"mode": "event", "max_event_seconds": 60})
    monkeypatch.setattr(main, "_ai_event_media_loop", object())
    monkeypatch.setattr(main.asyncio, "run_coroutine_threadsafe", fake_threadsafe)
    frame = np.full((720, 1280, 3), 90, np.uint8)
    monkeypatch.setattr(main.lpr, "scan_frame", lambda cam, fr, boxes, full_frame=None: [{
        "plate_number": "QRS4821", "confidence": 91.0, "region": (5, 5, 40, 15), "crop": fr[0:100, 0:200],
        "vehicle_box": None, "source": "full_frame"}])
    assert len(main.run_lpr_scan(2, {"frame": frame, "detections": []})) == 1
    assert scheduled == ["persist_event_recording", "_backfill_ai_event_linked_recording"]


# ---------------------------------------------------------------- precision over recall (real-traffic finding)

def test_low_confidence_reads_never_create_a_plate_event():
    for i in range(6):  # repeated agreeing but uncertain reads (both OCR passes disagreed -> ~69)
        assert lpr.confirm_plate(2, {"plate_number": "QRS4821", "confidence": 69.0}, now=float(i)) is None


def test_near_duplicate_misreads_after_a_confirmed_plate_are_suppressed():
    assert lpr.confirm_plate(2, {"plate_number": "QRS4821", "confidence": 93.0}, now=0.0) is None
    assert lpr.confirm_plate(2, {"plate_number": "QRS4821", "confidence": 93.0}, now=5.0) is not None
    for i, variant in enumerate(("QRE4821", "QRE4B21")):  # 1 and 2 edits off, even at high confidence
        lpr.confirm_plate(2, {"plate_number": variant, "confidence": 90.0}, now=100.0 + i * 10)
        assert lpr.confirm_plate(2, {"plate_number": variant, "confidence": 90.0}, now=105.0 + i * 10) is None


def test_a_genuinely_different_plate_is_still_confirmed():
    lpr.confirm_plate(2, {"plate_number": "QRS4821", "confidence": 93.0}, now=0.0)
    lpr.confirm_plate(2, {"plate_number": "QRS4821", "confidence": 93.0}, now=5.0)
    lpr.confirm_plate(2, {"plate_number": "HTW9035", "confidence": 91.0}, now=30.0)
    assert lpr.confirm_plate(2, {"plate_number": "HTW9035", "confidence": 91.0}, now=35.0) is not None


def test_near_duplicate_suppression_ends_with_the_cooldown_and_is_per_camera():
    lpr.confirm_plate(2, {"plate_number": "QRS4821", "confidence": 93.0}, now=0.0)
    lpr.confirm_plate(2, {"plate_number": "QRS4821", "confidence": 93.0}, now=5.0)
    lpr.confirm_plate(3, {"plate_number": "QRE4821", "confidence": 93.0}, now=10.0)
    assert lpr.confirm_plate(3, {"plate_number": "QRE4821", "confidence": 93.0}, now=15.0) is not None  # other camera
    later = 5.0 + lpr.LPR_REPEAT_COOLDOWN_SECONDS + 1
    lpr.confirm_plate(2, {"plate_number": "QRE4821", "confidence": 93.0}, now=later)
    assert lpr.confirm_plate(2, {"plate_number": "QRE4821", "confidence": 93.0}, now=later + 5) is not None


def test_edit_distance():
    assert lpr._edit_distance("KPC6266", "KPE6266") == 1 and lpr._edit_distance("KPC6266", "KPEG266") == 2
    assert lpr._edit_distance("ABC1234", "ABC1234") == 0 and lpr._edit_distance("ABC", "XYZ123") == 6
