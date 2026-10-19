"""Regression tests for the frame-timestamp input boundary (detection_timing.normalize_frame_time).

Red, 2026-10-18: Codex's 5c5971c1 test fed an ISO *string* frame time into save_yolo_events and the worker thread died
with "TypeError: '>' not supported between instances of 'str' and 'datetime.datetime'" at detection_timing.py
(plausible_frame_time). Every real producer passes a naive local datetime, but the boundary now normalizes or
rejects anything else so a timestamp can never again crash detection or be compared raw."""
from datetime import datetime, timedelta, timezone

import pytest

import detection_timing as dt

NOW = datetime(2026, 10, 18, 12, 0, 20)


def test_naive_datetime_is_unchanged():
    t = datetime(2026, 10, 18, 12, 0, 0)
    assert dt.normalize_frame_time(t) is t


def test_aware_datetime_becomes_the_same_instant_in_naive_local_time():
    aware = datetime(2026, 10, 18, 17, 0, 0, tzinfo=timezone.utc)
    got = dt.normalize_frame_time(aware)
    assert got.tzinfo is None
    assert got == aware.astimezone().replace(tzinfo=None)


@pytest.mark.parametrize("text", ["2026-10-18T12:00:00", " 2026-10-18T12:00:00.250 "])
def test_iso_text_is_parsed(text):
    assert dt.normalize_frame_time(text) == datetime.fromisoformat(text.strip())


def test_iso_text_with_z_is_utc():
    got = dt.normalize_frame_time("2026-10-18T17:00:00Z")
    assert got == datetime(2026, 10, 18, 17, 0, 0, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def test_epoch_seconds_are_accepted_in_range():
    ts = datetime(2026, 10, 18, 12, 0, 0).timestamp()
    assert dt.normalize_frame_time(ts) == datetime.fromtimestamp(ts)
    assert dt.normalize_frame_time(int(ts)) == datetime.fromtimestamp(int(ts))


@pytest.mark.parametrize("bad", ["", "yesterday", "12:00", True, False, [], {}, object(),
                                 1760788800000.0,      # milliseconds, not seconds
                                 -5, 10.0])
def test_anything_else_is_rejected_as_unknown(bad, caplog):
    assert dt.normalize_frame_time(bad) is None
    assert "detection_timing.frame_time_rejected" in caplog.text


def test_none_is_unknown_without_a_warning(caplog):
    assert dt.normalize_frame_time(None) is None
    assert caplog.text == ""


@pytest.mark.parametrize("frame_time", ["2026-10-18T12:00:00", datetime(2026, 10, 18, 12, 0, 0),
                                        datetime(2026, 10, 18, 12, 0, 0).timestamp()])
def test_detection_event_span_accepts_every_supported_form(frame_time):
    start, moment = dt.detection_event_span(frame_time, None, now=NOW, scan_interval_seconds=2.0)
    assert moment == datetime(2026, 10, 18, 12, 0, 0)
    assert start == moment - timedelta(seconds=2.0)


@pytest.mark.parametrize("bad", ["garbage", True, [], 1760788800000.0])
def test_detection_event_span_falls_back_to_now_and_never_raises(bad):
    start, moment = dt.detection_event_span(bad, bad, now=NOW, scan_interval_seconds=2.0)
    assert moment == NOW
    assert start == NOW - timedelta(seconds=2.0)


def test_previous_frame_time_as_text_is_used_for_the_event_start():
    start, moment = dt.detection_event_span(datetime(2026, 10, 18, 12, 0, 10), "2026-10-18T12:00:08",
                                            now=NOW, scan_interval_seconds=2.0)
    assert (start, moment) == (datetime(2026, 10, 18, 12, 0, 8), datetime(2026, 10, 18, 12, 0, 10))


def test_a_future_text_frame_time_is_still_refused_as_implausible():
    _, moment = dt.detection_event_span("2026-10-18T12:05:00", None, now=NOW, scan_interval_seconds=2.0)
    assert moment == NOW


def test_aware_now_comparison_never_raises_for_naive_producers():
    # The producers are naive; an aware candidate must be converted, never compared with a naive `now` raw.
    aware = (NOW - timedelta(seconds=5)).astimezone()
    _, moment = dt.detection_event_span(aware, None, now=NOW, scan_interval_seconds=2.0)
    assert moment == NOW - timedelta(seconds=5)
