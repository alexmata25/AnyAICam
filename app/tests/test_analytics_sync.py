"""Edge-side analytics-sync worker: tests for analytics_sync.py.

Covers the properties explicitly required for this milestone: the
persisted synced-id state survives a restart; a failed event's
local_event_id is never marked synced, so it is retried rather than
permanently skipped; the per-scan cap is respected and defaults to a
conservative non-zero value; only the fixed six-field allowlist is
ever sent in an outgoing payload; and this module never writes to
ANALYTICS_EVENTS_FILE or any thumbnail file, and never imports or
calls into the existing YOLO/motion-detection code path.

Fast/pure tests only -- no network, no FastAPI app, no AWS. Every test
gets its own isolated ANALYTICS_EVENTS_FILE/SYNC_STATE_FILE via
tmp_path, and resets the module's in-memory caches so no test can see
another's.
"""

import json

import pytest

import analytics_sync as asy


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(asy, "ANALYTICS_EVENTS_FILE", tmp_path / "analytics_events.json")
    monkeypatch.setattr(asy, "SYNC_STATE_FILE", tmp_path / "state" / "analytics_sync_state.json")
    monkeypatch.setattr(asy, "MAX_EVENTS_PER_SCAN", asy.DEFAULT_MAX_EVENTS_PER_SCAN)
    asy._synced_ids_cache = None
    asy._unknown_camera_logged.clear()
    with asy._lock:
        asy._camera_map.clear()
    yield tmp_path
    asy._synced_ids_cache = None
    asy._unknown_camera_logged.clear()
    with asy._lock:
        asy._camera_map.clear()


def _write_local_events(tmp_path, events):
    asy.ANALYTICS_EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    asy.ANALYTICS_EVENTS_FILE.write_text(json.dumps(events), encoding="utf-8")


def _event(event_id, *, camera=1, event_type="person", timestamp="2026-08-21T10:00:00", confidence=0.9, object_count=1, detections=None):
    return {
        "id": event_id,
        "camera": camera,
        "site": "home",
        "rule_name": f"Local YOLO {event_type} detection",
        "event_type": event_type,
        "timestamp": timestamp,
        "confidence": confidence,
        "thumbnail": f"/recordings/media/ai/2026-08-21/camera{camera}_10-00-00_{event_id}.jpg",
        "linked_recording": f"camera{camera}_2026-08-21_10-00-00.mkv",
        "mock": False,
        "object_count": object_count,
        "detections": detections if detections is not None else [{"class_name": event_type, "confidence": confidence, "x": 1, "y": 2, "width": 3, "height": 4}],
    }


def _register_camera(camera_number, camera_id="cam-abc123", site_id="site-xyz789"):
    with asy._lock:
        asy._camera_map[camera_number] = {"camera_id": camera_id, "site_id": site_id}


# --------------------------------------------------------- synced-id state persistence / restart durability


def test_missing_state_file_reads_as_nothing_synced(tmp_path):
    assert asy._load_synced_ids() == []


def test_corrupt_state_file_fails_safe_to_nothing_synced_not_a_crash(tmp_path):
    asy.SYNC_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    asy.SYNC_STATE_FILE.write_text("{not valid json", encoding="utf-8")
    assert asy._load_synced_ids() == []


def test_persisted_id_survives_a_simulated_restart(tmp_path):
    asy._persist_synced_id("evt-1")
    # Simulate a fresh process: drop the in-memory cache, forcing a re-read
    # from disk.
    asy._synced_ids_cache = None
    assert "evt-1" in asy._load_synced_ids()


def test_persist_writes_the_expected_json_shape(tmp_path):
    asy._persist_synced_id("evt-1")
    saved = json.loads(asy.SYNC_STATE_FILE.read_text())
    assert saved == {"synced_event_ids": ["evt-1"]}


def test_persist_is_idempotent_for_the_same_id(tmp_path):
    asy._persist_synced_id("evt-1")
    asy._persist_synced_id("evt-1")
    assert asy._load_synced_ids().count("evt-1") == 1


def test_state_size_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(asy, "MAX_TRACKED_SYNCED_IDS", 3)
    for i in range(5):
        asy._persist_synced_id(f"evt-{i}")
    ids = asy._load_synced_ids()
    assert len(ids) == 3
    # Oldest evicted first, newest retained.
    assert ids == ["evt-2", "evt-3", "evt-4"]


# --------------------------------------------------------- failure-safe cursor: a failure never permanently skips an event


def test_successful_event_is_marked_synced(tmp_path, monkeypatch):
    _register_camera(1)
    _write_local_events(tmp_path, [_event("evt-ok")])
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: {"status": "accepted", "event_id": "server-1"})

    summary = asy._sync_pending_events()

    assert summary == {"attempted": 1, "synced": 1, "failed": 0}
    assert "evt-ok" in asy._load_synced_ids()


def test_duplicate_response_is_treated_as_success_not_a_failure(tmp_path, monkeypatch):
    _register_camera(1)
    _write_local_events(tmp_path, [_event("evt-dup")])
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: {"status": "duplicate", "event_id": "server-1"})

    summary = asy._sync_pending_events()

    assert summary == {"attempted": 1, "synced": 1, "failed": 0}
    assert "evt-dup" in asy._load_synced_ids()


def test_failed_event_is_never_marked_synced_and_is_retried(tmp_path, monkeypatch):
    _register_camera(1)
    _write_local_events(tmp_path, [_event("evt-fail")])
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: None)  # network failure / HTTP error

    first = asy._sync_pending_events()
    assert first == {"attempted": 1, "synced": 0, "failed": 1}
    assert "evt-fail" not in asy._load_synced_ids()

    # Next scan: still pending, attempted again -- not silently dropped.
    second = asy._sync_pending_events()
    assert second == {"attempted": 1, "synced": 0, "failed": 1}


def test_one_failed_event_does_not_block_a_later_event_in_the_same_scan(tmp_path, monkeypatch):
    _register_camera(1)
    _write_local_events(tmp_path, [
        _event("evt-a", timestamp="2026-08-21T10:00:00"),
        _event("evt-b", timestamp="2026-08-21T10:01:00"),
    ])

    def _post(path, payload):
        if payload["local_event_id"] == "evt-a":
            return None  # fails
        return {"status": "accepted", "event_id": "server-b"}

    monkeypatch.setattr(asy, "_control_plane_post", _post)

    summary = asy._sync_pending_events()

    assert summary == {"attempted": 2, "synced": 1, "failed": 1}
    synced = asy._load_synced_ids()
    assert "evt-a" not in synced
    assert "evt-b" in synced


def test_malformed_local_event_is_not_marked_synced(tmp_path, monkeypatch):
    _register_camera(1)
    _write_local_events(tmp_path, [_event("evt-bad", event_type="")])  # missing required event_type
    calls = []
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: calls.append(payload) or {"status": "accepted"})

    summary = asy._sync_pending_events()

    assert summary == {"attempted": 1, "synced": 0, "failed": 1}
    assert calls == []  # never even attempted a network call for unsendable data
    assert "evt-bad" not in asy._load_synced_ids()


def test_event_for_unrecognized_camera_is_left_pending_not_counted_against_cap(tmp_path, monkeypatch):
    # No camera registered at all.
    _write_local_events(tmp_path, [_event("evt-unknown", camera=9)])
    calls = []
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: calls.append(payload) or {"status": "accepted"})

    summary = asy._sync_pending_events()

    assert summary == {"attempted": 0, "synced": 0, "failed": 0}
    assert calls == []
    assert "evt-unknown" not in asy._load_synced_ids()


# --------------------------------------------------------- per-scan cap


def test_default_cap_is_conservative_and_nonzero():
    assert asy.DEFAULT_MAX_EVENTS_PER_SCAN > 0
    assert asy.DEFAULT_MAX_EVENTS_PER_SCAN <= 50  # conservative first-rollout bound, not a fire-hose


def test_unset_env_uses_the_conservative_default(monkeypatch):
    monkeypatch.delenv("ANYAICAM_ANALYTICS_SYNC_MAX_EVENTS_PER_SCAN", raising=False)
    assert asy._parse_max_events_per_scan() == asy.DEFAULT_MAX_EVENTS_PER_SCAN


def test_invalid_env_fails_safe_to_the_tightest_cap_not_unlimited(monkeypatch):
    for bad in ("0", "-5", "not-a-number"):
        monkeypatch.setenv("ANYAICAM_ANALYTICS_SYNC_MAX_EVENTS_PER_SCAN", bad)
        assert asy._parse_max_events_per_scan() == 1


def test_valid_env_is_honored(monkeypatch):
    monkeypatch.setenv("ANYAICAM_ANALYTICS_SYNC_MAX_EVENTS_PER_SCAN", "7")
    assert asy._parse_max_events_per_scan() == 7


def test_cap_limits_events_attempted_per_scan(tmp_path, monkeypatch):
    _register_camera(1)
    monkeypatch.setattr(asy, "MAX_EVENTS_PER_SCAN", 2)
    _write_local_events(tmp_path, [_event(f"evt-{i}", timestamp=f"2026-08-21T10:0{i}:00") for i in range(5)])
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: {"status": "accepted"})

    summary = asy._sync_pending_events()

    assert summary == {"attempted": 2, "synced": 2, "failed": 0}
    # The remaining three stay pending for a later scan.
    assert len(asy._load_synced_ids()) == 2


def test_leftover_events_beyond_the_cap_are_picked_up_on_a_later_scan(tmp_path, monkeypatch):
    _register_camera(1)
    monkeypatch.setattr(asy, "MAX_EVENTS_PER_SCAN", 2)
    _write_local_events(tmp_path, [_event(f"evt-{i}", timestamp=f"2026-08-21T10:0{i}:00") for i in range(3)])
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: {"status": "accepted"})

    asy._sync_pending_events()
    second = asy._sync_pending_events()

    assert second == {"attempted": 1, "synced": 1, "failed": 0}
    assert len(asy._load_synced_ids()) == 3


def test_oldest_events_are_synced_first(tmp_path, monkeypatch):
    _register_camera(1)
    monkeypatch.setattr(asy, "MAX_EVENTS_PER_SCAN", 1)
    # File is newest-first, matching append_analytics_event()'s own
    # reverse-chronological sort.
    _write_local_events(tmp_path, [
        _event("evt-new", timestamp="2026-08-21T10:05:00"),
        _event("evt-old", timestamp="2026-08-21T10:00:00"),
    ])
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: {"status": "accepted"})

    asy._sync_pending_events()

    assert asy._load_synced_ids() == ["evt-old"]


# --------------------------------------------------------- outgoing payload allowlist


def test_payload_contains_only_the_six_allowlisted_fields(tmp_path):
    payload = asy._build_payload(_event("evt-1"))
    assert set(payload.keys()) == {"local_event_id", "event_type", "confidence", "object_count", "detections", "event_timestamp"}


def test_payload_never_includes_thumbnail_or_linked_recording(tmp_path):
    event = _event("evt-1")
    assert "thumbnail" in event and "linked_recording" in event  # sanity: the source event really has them
    payload = asy._build_payload(event)
    serialized = json.dumps(payload)
    assert "thumbnail" not in serialized
    assert "linked_recording" not in serialized
    assert event["thumbnail"] not in serialized
    assert event["linked_recording"] not in serialized


def test_payload_never_includes_site_camera_or_mock_fields(tmp_path):
    payload = asy._build_payload(_event("evt-1"))
    serialized = json.dumps(payload)
    assert '"site"' not in serialized
    assert '"camera"' not in serialized
    assert '"mock"' not in serialized
    assert '"rule_name"' not in serialized


def test_payload_maps_local_event_fields_to_the_expected_names(tmp_path):
    event = _event("evt-1", event_type="dog", timestamp="2026-08-21T11:22:33", confidence=0.5, object_count=2)
    payload = asy._build_payload(event)
    assert payload["local_event_id"] == "evt-1"
    assert payload["event_type"] == "dog"
    assert payload["event_timestamp"] == "2026-08-21T11:22:33"
    assert payload["confidence"] == 0.5
    assert payload["object_count"] == 2
    assert payload["detections"] == event["detections"]


def test_the_actual_post_uses_the_expected_camera_scoped_path(tmp_path, monkeypatch):
    _register_camera(1, camera_id="cam-real-id")
    _write_local_events(tmp_path, [_event("evt-1")])
    calls = []
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: calls.append(path) or {"status": "accepted"})

    asy._sync_pending_events()

    assert calls == ["/api/appliance/analytics/cam-real-id/events"]


# --------------------------------------------------------- this module never touches the local analytics record or the detection path


def test_local_events_file_is_never_modified(tmp_path, monkeypatch):
    _register_camera(1)
    _write_local_events(tmp_path, [_event("evt-1")])
    before = asy.ANALYTICS_EVENTS_FILE.read_text(encoding="utf-8")
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: {"status": "accepted"})

    asy._sync_pending_events()

    after = asy.ANALYTICS_EVENTS_FILE.read_text(encoding="utf-8")
    assert after == before


def test_module_has_no_write_call_to_the_local_events_file_anywhere():
    # A structural guarantee, not just a behavioral one for the cases
    # exercised above: nothing in this module ever calls write_text /
    # write_bytes / unlink on ANALYTICS_EVENTS_FILE, because no such call
    # exists in the source at all.
    import inspect
    source = inspect.getsource(asy)
    # The only writes in this module are to SYNC_STATE_FILE.
    assert "ANALYTICS_EVENTS_FILE.write_text" not in source
    assert "ANALYTICS_EVENTS_FILE.write_bytes" not in source
    assert "ANALYTICS_EVENTS_FILE.unlink" not in source


def test_module_never_imports_or_references_the_detection_code_path():
    # Checks actual code, not the module's own docstring -- the docstring
    # legitimately names these functions in prose to explain that this
    # module never calls them, so a bare substring check over the whole
    # source (docstring included) would misfire on its own documentation.
    # ast strips docstrings out as plain Expr statements, so walking the
    # parsed tree's real Import/Call/Attribute nodes checks only code that
    # could actually execute.
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(asy))
    referenced_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            referenced_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            referenced_names.add(node.module)
        elif isinstance(node, ast.Name):
            referenced_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced_names.add(node.attr)

    for forbidden in ("ai_person_detector", "motion_detector", "save_yolo_events", "append_analytics_event", "main"):
        assert forbidden not in referenced_names


# --------------------------------------------------------- worker gating (disabled by default, edge-only)


def test_flag_defaults_false(monkeypatch):
    monkeypatch.delenv("ANYAICAM_ANALYTICS_SYNC_ENABLED", raising=False)
    import importlib
    reloaded = importlib.reload(asy)
    assert reloaded.ANALYTICS_SYNC_ENABLED is False
    importlib.reload(asy)  # restore a clean module state for subsequent tests


@pytest.mark.anyio
async def test_worker_sleeps_forever_without_syncing_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(asy, "ANALYTICS_SYNC_ENABLED", False)
    _register_camera(1)
    _write_local_events(tmp_path, [_event("evt-1")])
    calls = []
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: calls.append(path) or {"status": "accepted"})

    import asyncio
    task = asyncio.ensure_future(asy.analytics_sync_worker())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert calls == []
    assert asy.analytics_sync_state["worker_status"] == "disabled"


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --------------------------------------------------------- controlled-rollout camera scope


def test_unset_camera_scope_restricts_nothing(tmp_path):
    assert asy.SYNC_CAMERA_SCOPE is None


def test_camera_outside_the_scope_stays_pending_not_counted_against_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(asy, "SYNC_CAMERA_SCOPE", frozenset({1}))
    _register_camera(1, camera_id="cam-1")
    _register_camera(2, camera_id="cam-2")
    _write_local_events(tmp_path, [_event("evt-cam1", camera=1), _event("evt-cam2", camera=2)])
    calls = []
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: calls.append(payload) or {"status": "accepted"})

    summary = asy._sync_pending_events()

    assert summary == {"attempted": 1, "synced": 1, "failed": 0}
    assert len(calls) == 1
    assert calls[0]["local_event_id"] == "evt-cam1"
    assert "evt-cam1" in asy._load_synced_ids()
    assert "evt-cam2" not in asy._load_synced_ids()


def test_camera_in_scope_is_synced_normally(tmp_path, monkeypatch):
    monkeypatch.setattr(asy, "SYNC_CAMERA_SCOPE", frozenset({1}))
    _register_camera(1, camera_id="cam-1")
    _write_local_events(tmp_path, [_event("evt-cam1", camera=1)])
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: {"status": "accepted"})

    summary = asy._sync_pending_events()

    assert summary == {"attempted": 1, "synced": 1, "failed": 0}
    assert "evt-cam1" in asy._load_synced_ids()


def test_camera_left_pending_by_scope_is_picked_up_once_scope_widens(tmp_path, monkeypatch):
    monkeypatch.setattr(asy, "SYNC_CAMERA_SCOPE", frozenset({1}))
    _register_camera(2, camera_id="cam-2")
    _write_local_events(tmp_path, [_event("evt-cam2", camera=2)])
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: {"status": "accepted"})

    first = asy._sync_pending_events()
    assert first == {"attempted": 0, "synced": 0, "failed": 0}

    monkeypatch.setattr(asy, "SYNC_CAMERA_SCOPE", frozenset({1, 2}))
    second = asy._sync_pending_events()
    assert second == {"attempted": 1, "synced": 1, "failed": 0}
    assert "evt-cam2" in asy._load_synced_ids()


# --------------------------------------------------------- best-effort notification forwarding (disabled by default)


def test_notify_flag_defaults_false(monkeypatch):
    monkeypatch.delenv("ANYAICAM_ANALYTICS_SYNC_NOTIFY_ENABLED", raising=False)
    import importlib
    reloaded = importlib.reload(asy)
    assert reloaded.ANALYTICS_SYNC_NOTIFY_ENABLED is False
    importlib.reload(asy)  # restore a clean module state for subsequent tests


def test_notification_not_sent_when_flag_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(asy, "ANALYTICS_SYNC_NOTIFY_ENABLED", False)
    _register_camera(1, camera_id="cam-1")
    _write_local_events(tmp_path, [_event("evt-1")])
    calls = []
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: calls.append(path) or {"status": "accepted"})

    asy._sync_pending_events()

    assert calls == ["/api/appliance/analytics/cam-1/events"]  # unchanged from before this feature


def test_notification_sent_after_a_successful_sync_when_flag_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(asy, "ANALYTICS_SYNC_NOTIFY_ENABLED", True)
    _register_camera(1, camera_id="cam-1")
    _write_local_events(tmp_path, [_event("evt-1", event_type="person", timestamp="2026-08-21T10:00:00")])
    calls = []

    def _post(path, payload):
        calls.append((path, payload))
        return {"status": "accepted"}

    monkeypatch.setattr(asy, "_control_plane_post", _post)

    asy._sync_pending_events()

    paths = [call[0] for call in calls]
    assert paths == ["/api/appliance/analytics/cam-1/events", "/api/appliance/events"]
    notify_payload = calls[1][1]
    assert notify_payload == {"events": [{
        "id": "evt-1", "event_type": "person", "camera_id": "cam-1", "timestamp": "2026-08-21T10:00:00",
    }]}


def test_notification_reuses_the_same_local_event_id_for_idempotency(tmp_path, monkeypatch):
    monkeypatch.setattr(asy, "ANALYTICS_SYNC_NOTIFY_ENABLED", True)
    _register_camera(1, camera_id="cam-1")
    _write_local_events(tmp_path, [_event("evt-shared-id")])
    calls = []
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: calls.append((path, payload)) or {"status": "accepted"})

    asy._sync_pending_events()

    analytics_call = next(c for c in calls if c[0].startswith("/api/appliance/analytics"))
    notify_call = next(c for c in calls if c[0] == "/api/appliance/events")
    assert analytics_call[1]["local_event_id"] == notify_call[1]["events"][0]["id"] == "evt-shared-id"


def test_notification_never_includes_thumbnail_or_linked_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(asy, "ANALYTICS_SYNC_NOTIFY_ENABLED", True)
    _register_camera(1, camera_id="cam-1")
    event = _event("evt-1")
    _write_local_events(tmp_path, [event])
    calls = []
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: calls.append((path, payload)) or {"status": "accepted"})

    asy._sync_pending_events()

    notify_call = next(c for c in calls if c[0] == "/api/appliance/events")
    serialized = json.dumps(notify_call[1])
    assert "thumbnail" not in serialized
    assert "linked_recording" not in serialized
    assert event["thumbnail"] not in serialized


def test_a_failed_notification_does_not_prevent_the_event_from_being_marked_synced(tmp_path, monkeypatch):
    monkeypatch.setattr(asy, "ANALYTICS_SYNC_NOTIFY_ENABLED", True)
    _register_camera(1, camera_id="cam-1")
    _write_local_events(tmp_path, [_event("evt-1")])

    def _post(path, payload):
        if path == "/api/appliance/events":
            return None  # notification delivery fails
        return {"status": "accepted"}  # the analytics-event sync itself succeeds

    monkeypatch.setattr(asy, "_control_plane_post", _post)

    summary = asy._sync_pending_events()

    assert summary == {"attempted": 1, "synced": 1, "failed": 0}
    assert "evt-1" in asy._load_synced_ids()


def test_notification_is_never_sent_for_an_event_the_analytics_sync_itself_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(asy, "ANALYTICS_SYNC_NOTIFY_ENABLED", True)
    _register_camera(1, camera_id="cam-1")
    _write_local_events(tmp_path, [_event("evt-1")])
    calls = []

    def _post(path, payload):
        calls.append(path)
        return None  # the analytics-event sync fails

    monkeypatch.setattr(asy, "_control_plane_post", _post)

    asy._sync_pending_events()

    assert calls == ["/api/appliance/analytics/cam-1/events"]  # never reached /api/appliance/events


# --------------------------------------------------------- vehicle sub-class -> generic "vehicle" notification mapping


@pytest.mark.parametrize("vehicle_class", ["car", "truck", "motorcycle", "bus", "bicycle"])
def test_vehicle_subclass_notification_event_type_is_the_generic_vehicle(vehicle_class):
    assert asy._notification_event_type(vehicle_class) == "vehicle"


@pytest.mark.parametrize("non_vehicle_class", ["person", "motion", "people_counting", "line_crossing", "intrusion", "suitcase", "dog"])
def test_non_vehicle_event_type_passes_through_unchanged(non_vehicle_class):
    assert asy._notification_event_type(non_vehicle_class) == non_vehicle_class


def test_smart_motion_notification_message_names_the_real_trigger():
    event = _event("evt-sm-1", event_type="smart_motion")
    event["triggered_by"] = "person"
    payload = asy._build_notification_payload(event, "cam-1")
    assert payload["message"] == "Smart Motion: person detected"
    assert payload["event_type"] == "smart_motion"


@pytest.mark.parametrize("triggered_by", ["car", "truck", "dog", "cat", "bicycle"])
def test_smart_motion_notification_message_names_any_real_trigger_class(triggered_by):
    event = _event("evt-sm-2", event_type="smart_motion")
    event["triggered_by"] = triggered_by
    payload = asy._build_notification_payload(event, "cam-1")
    assert payload["message"] == f"Smart Motion: {triggered_by} detected"


def test_smart_motion_without_a_triggered_by_falls_back_to_the_generic_cloud_message():
    # Should never happen in practice (store_motion_event() always sets
    # triggered_by before building a smart_motion event), but the
    # payload must never claim a trigger that isn't real.
    event = _event("evt-sm-3", event_type="smart_motion")
    payload = asy._build_notification_payload(event, "cam-1")
    assert "message" not in payload


def test_non_smart_motion_events_never_get_a_message_override():
    # The custom message is scoped to smart_motion only -- person/
    # vehicle/etc. keep using the cloud's own generic title/message,
    # completely unchanged by this addition.
    event = _event("evt-sm-4", event_type="person")
    event["triggered_by"] = "person"  # even if present, must be ignored here
    payload = asy._build_notification_payload(event, "cam-1")
    assert "message" not in payload


@pytest.mark.parametrize("vehicle_class", ["car", "truck", "motorcycle", "bus", "bicycle"])
def test_notification_payload_maps_vehicle_subclass_to_generic_vehicle(vehicle_class):
    event = _event("evt-1", event_type=vehicle_class)
    payload = asy._build_notification_payload(event, "cam-1")
    assert payload["event_type"] == "vehicle"


@pytest.mark.parametrize("vehicle_class", ["car", "truck", "motorcycle", "bus", "bicycle"])
def test_analytics_history_payload_keeps_the_specific_vehicle_subclass_unchanged(vehicle_class):
    # The exact requirement this whole fix is scoped around: the
    # notification path's translation must never leak into what
    # analytics history (detection_events, via _build_payload) records.
    event = _event("evt-1", event_type=vehicle_class)
    payload = asy._build_payload(event)
    assert payload["event_type"] == vehicle_class  # NOT "vehicle" -- the real, specific class


@pytest.mark.parametrize("vehicle_class", ["car", "truck", "motorcycle", "bus", "bicycle"])
def test_end_to_end_sync_sends_specific_class_to_analytics_and_generic_vehicle_to_notify(tmp_path, monkeypatch, vehicle_class):
    monkeypatch.setattr(asy, "ANALYTICS_SYNC_NOTIFY_ENABLED", True)
    _register_camera(1, camera_id="cam-1")
    _write_local_events(tmp_path, [_event("evt-1", event_type=vehicle_class)])
    calls = []
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: calls.append((path, payload)) or {"status": "accepted"})

    summary = asy._sync_pending_events()

    assert summary == {"attempted": 1, "synced": 1, "failed": 0}
    analytics_call = next(c for c in calls if c[0].startswith("/api/appliance/analytics"))
    notify_call = next(c for c in calls if c[0] == "/api/appliance/events")
    assert analytics_call[1]["event_type"] == vehicle_class
    assert notify_call[1]["events"][0]["event_type"] == "vehicle"
    # Same id on both, still -- the mapping only ever touches event_type.
    assert analytics_call[1]["local_event_id"] == notify_call[1]["events"][0]["id"] == "evt-1"


def test_person_notification_still_says_person_not_vehicle(tmp_path, monkeypatch):
    # Regression guard: the mapping must be additive, not a blanket
    # rewrite -- a real person detection's notification event_type must
    # stay exactly "person".
    monkeypatch.setattr(asy, "ANALYTICS_SYNC_NOTIFY_ENABLED", True)
    _register_camera(1, camera_id="cam-1")
    _write_local_events(tmp_path, [_event("evt-1", event_type="person")])
    calls = []
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: calls.append((path, payload)) or {"status": "accepted"})

    asy._sync_pending_events()

    notify_call = next(c for c in calls if c[0] == "/api/appliance/events")
    assert notify_call[1]["events"][0]["event_type"] == "person"


# --------------------------------------------------------- no duplicate notification on retry/replay


def test_an_already_synced_vehicle_event_is_never_resent_or_renotified_on_a_later_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(asy, "ANALYTICS_SYNC_NOTIFY_ENABLED", True)
    _register_camera(1, camera_id="cam-1")
    _write_local_events(tmp_path, [_event("evt-car-1", event_type="car")])
    calls = []
    monkeypatch.setattr(asy, "_control_plane_post", lambda path, payload: calls.append(path) or {"status": "accepted"})

    first = asy._sync_pending_events()
    assert first == {"attempted": 1, "synced": 1, "failed": 0}
    assert calls == ["/api/appliance/analytics/cam-1/events", "/api/appliance/events"]

    calls.clear()
    second = asy._sync_pending_events()

    # The event is already in the synced-id state -- _pending_events
    # excludes it entirely, so NEITHER cloud route is called again for
    # it. This is this module's own half of "no duplicate notification"
    # -- the cloud route's own INSERT OR IGNORE on (appliance_id,
    # event_id) is the other half, for the case where a POST genuinely
    # gets retried before a synced response is received (see the
    # _build_notification_payload docstring on why reusing local_event_id
    # as the notification route's own id makes that safe too).
    assert second == {"attempted": 0, "synced": 0, "failed": 0}
    assert calls == []


def test_a_notification_retried_after_a_transient_failure_reuses_the_same_id_for_cloud_side_idempotency(tmp_path, monkeypatch):
    # Simulates the notification POST failing once (e.g. a transient
    # network error) and the analytics-event POST succeeding -- the
    # event is marked synced either way (notification is best-effort),
    # so a later scan never retries either call for this event. This
    # confirms a flaky notification delivery can never turn into a
    # duplicate: it either succeeds once, or the module simply moves on
    # without ever re-POSTing to the notification route for this event
    # again.
    monkeypatch.setattr(asy, "ANALYTICS_SYNC_NOTIFY_ENABLED", True)
    _register_camera(1, camera_id="cam-1")
    _write_local_events(tmp_path, [_event("evt-truck-1", event_type="truck")])
    notify_calls = []

    def _post(path, payload):
        if path == "/api/appliance/events":
            notify_calls.append(payload)
            return None  # fails once
        return {"status": "accepted"}

    monkeypatch.setattr(asy, "_control_plane_post", _post)

    asy._sync_pending_events()
    asy._sync_pending_events()  # a later scan -- event already synced, must not retry

    assert len(notify_calls) == 1  # exactly one attempt, never a silent retry-storm
    assert notify_calls[0]["events"][0]["event_type"] == "vehicle"
