"""store_motion_event(): a failure to look up the recording mode or to schedule persist_event_recording() never
escapes (independent Codex review of R0 77070be, 2026-11-06, P2). Before the fix the exception left the function, so
the alert rule and Smart Motion handling after it were skipped and the camera's motion worker could restart."""
import asyncio
from datetime import datetime
from unittest import mock

import main
from test_smart_motion_event_media_wiring import (  # noqa: F401
    _by_event_type, _classify_as, _standard_motion_mocks, _store_and_drain, fake_media_pipeline)


def _logged(monkeypatch):
    lines = []
    monkeypatch.setattr(main, "structured_log", lambda event, level="info", **fields: lines.append((event, level, fields)))
    return lines


def _rule_spy(monkeypatch):
    calls = []

    def rule(camera_number):
        calls.append(camera_number)
        return mock.Mock(enabled=False, event_types=[])

    monkeypatch.setattr(main, "get_alert_rule", rule)
    return calls


async def _fake_clip(event_id, camera_number, start, end):
    return f"/recordings/clips/motion/motion_{event_id}.mp4"


def test_a_recording_scheduling_failure_is_logged_and_the_rest_of_the_event_still_runs(monkeypatch, fake_media_pipeline):
    analytics_events = _standard_motion_mocks(monkeypatch)
    rule_calls = _rule_spy(monkeypatch)
    _classify_as(monkeypatch, "person")
    monkeypatch.setattr(main, "build_motion_event_clip", _fake_clip)
    lines = _logged(monkeypatch)
    real_create_task = asyncio.create_task
    refused = []

    def selective_create_task(coro, *args, **kwargs):
        if getattr(coro, "__name__", "") == "persist_event_recording":
            refused.append(coro)
            raise RuntimeError("simulated scheduling failure")
        return real_create_task(coro, *args, **kwargs)

    monkeypatch.setattr(main.asyncio, "create_task", selective_create_task)
    now = datetime.now()
    asyncio.run(_store_and_drain(11, now, now))  # must not raise

    assert refused and refused[0].cr_frame is None  # the refused coroutine was closed, never left un-awaited
    assert rule_calls == [11]  # the alert rule after the scheduling point still ran
    assert _by_event_type(analytics_events, "smart_motion")["triggered_by"] == "person"
    assert fake_media_pipeline.upload_calls  # the base clip task still ran
    failures = [line for line in lines if line[0] == "event_recording.schedule_failed"]
    assert failures and failures[0][1] == "error" and failures[0][2]["error"] == "RuntimeError"


def test_a_mode_lookup_failure_still_schedules_the_event_recording(monkeypatch, fake_media_pipeline):
    _standard_motion_mocks(monkeypatch)
    rule_calls = _rule_spy(monkeypatch)
    monkeypatch.setattr(main.smart_motion, "classify_motion", lambda camera_number: None)
    monkeypatch.setattr(main, "build_motion_event_clip", _fake_clip)
    lines = _logged(monkeypatch)

    def unreadable(camera_number):
        raise OSError("synthetic settings failure")

    monkeypatch.setattr(main, "_local_recording_settings", unreadable)
    persisted = []

    async def fake_persist(camera_number, start, end, *, detector, trigger_id):
        persisted.append((camera_number, detector, trigger_id))

    monkeypatch.setattr(main, "persist_event_recording", fake_persist)
    now = datetime.now()
    asyncio.run(_store_and_drain(12, now, now))  # must not raise

    assert persisted and persisted[0][:2] == (12, "basic_motion")  # R-REC-1: the mode is always event
    assert rule_calls == [12]
    assert any(line[0] == "event_recording.mode_lookup_failed" for line in lines)
