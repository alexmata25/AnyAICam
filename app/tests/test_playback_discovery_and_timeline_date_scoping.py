"""Two genuine, customer-reported Playback bugs found and fixed 2026-09-15:

1. Discovery: renderCamera() (the default, just-opened-Playback view)
   already populated #playback-clip-list with real data on every load,
   but the panel containing it (#playback-clip-panel) starts `hidden` in
   the page's own markup and was previously only ever revealed by
   playClip() or the Browse button -- loadRecordingsForDate() (the
   date-picker path) already unconditionally reveals it; renderCamera()
   now does the same, for the same reason.

2. Timeline date-blindness: renderTimeline() used to plot every passed-in
   clip/event using only its time-of-day fraction, never checking which
   actual calendar day it belongs to. Correct only when every item
   genuinely belongs to the one day the 00:00-24:00 axis represents (true
   for loadRecordingsForDate()'s own date=-scoped fetch) but NOT true for
   renderCamera()'s default view, whose clips are simply "the most recent
   N recordings" with no date filter -- e.g. first thing in the morning,
   before today has N recordings yet, last night's footage was silently
   plotted onto an axis with no date label, looking exactly like "today".
   renderTimeline() now takes an explicit dayString and only plots
   clips/events that actually overlap that one local day.

Also fixed, same pass, same root class of bug (the server's own
APPLIANCE_TIMEZONE constant is hardcoded to America/Chicago, with no
stored per-customer/per-site timezone anywhere in this product to read
instead): loadRecordingsForDate() now computes the requested local day's
exact UTC bounds in the customer's own browser (the only party that
genuinely knows their real local offset, DST included) and sends them as
day_start_utc/day_end_utc alongside the existing date= param;
customer_recordings_metadata() uses them when present, falling back to
the pre-existing APPLIANCE_TIMEZONE-based conversion only for a caller
that doesn't send them (an older client, or a direct API call) --
_customer_recordings_for_date()'s own existing behavior/contract is
completely unchanged for that fallback case.

Same import-inside-container constraint as the other main.py-importing
test files in this suite.
"""

from types import SimpleNamespace

import main


def _fake_request(t=None, camera=None):
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: {"t": t, "camera": camera}.get(key, default)))


def _render(monkeypatch, recordings=None, events=None):
    monkeypatch.setattr(main, "_customer_recording_rows", lambda camera_id, **kwargs: recordings or [])
    monkeypatch.setattr(main, "_customer_detection_events", lambda request: events or [])
    monkeypatch.setattr(main, "_customer_camera_events", lambda camera_id, date: events or [])
    return main._render_customer_playback([{"id": "cam-1", "name": "Front Door", "camera_number": 1}], _fake_request())


# ---------------------------------------------------------------------------
# Bug 1: the clip panel must be revealed by the default (no date picked) view.
# ---------------------------------------------------------------------------

def test_render_camera_reveals_clip_panel_without_requiring_a_click(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("async function renderCamera(seekTimestamp){")
    end = html.index("async function playEventClipDeepLink(", idx)
    body = html[idx:end]
    assert "clipPanel.hidden=false;" in body
    # Must run before the clip list is actually populated, not after.
    assert body.index("clipPanel.hidden=false;") < body.index("renderClipList(cameraId,clips);")


# ---------------------------------------------------------------------------
# Bug 2: renderTimeline() must be date-scoped at every call site, and must
# filter by an overlap test (not plot everything it's handed blindly).
# ---------------------------------------------------------------------------

def test_render_timeline_definition_filters_by_day_overlap(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("function renderTimeline(cameraId,clips,events,dayString){")
    end = html.index("let visibleRecordingCount=6;", idx)
    body = html[idx:end]
    assert "const dayStartMs=new Date(dayY,dayM-1,dayD,0,0,0,0).getTime();" in body
    assert "const dayEndMs=dayStartMs+86400000;" in body
    assert "const dayClips=clips.filter(clip=>playbackDate(clip.end).getTime()>dayStartMs&&playbackDate(clip.start).getTime()<dayEndMs);" in body
    assert "[...dayClips].reverse().forEach(clip=>{" in body
    assert "dayEvents.forEach(event=>{" in body
    # renderMobileRecentEvents (the separate "most recent N regardless of
    # day" mobile cards list) must still get the full, unfiltered sets.
    assert "renderMobileRecentEvents(cameraId,clips,events);" in body


def test_render_camera_passes_todays_local_date_when_no_deep_link(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("async function renderCamera(seekTimestamp){")
    end = html.index("async function playEventClipDeepLink(", idx)
    body = html[idx:end]
    assert "const dayString=seekTimestamp?localDateStringOf(playbackDate(seekTimestamp)):localDateStringOf(new Date());" in body
    assert "renderTimeline(cameraId,clips,events,dayString);" in body


def test_load_recordings_for_date_passes_the_selected_date(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("async function loadRecordingsForDate(cameraId,date){")
    end = html.index("dateInput.addEventListener", idx)
    body = html[idx:end]
    assert "renderTimeline(cameraId,clips,eventsForLocalDate(analyticsByCamera[cameraId]||[],date),date);" in body



# ---------------------------------------------------------------------------
# Bug 2's root cause on the query side: no server timezone should be
# trusted when the browser can compute exact bounds itself.
# ---------------------------------------------------------------------------

def test_load_recordings_for_date_sends_browser_computed_utc_bounds(monkeypatch):
    html = _render(monkeypatch)
    assert "function localDayBoundsToUtcNaiveIso(dateString){" in html
    idx = html.index("async function loadRecordingsForDate(cameraId,date){")
    end = html.index("dateInput.addEventListener", idx)
    body = html[idx:end]
    assert "const [day_start_utc,day_end_utc]=localDayBoundsToUtcNaiveIso(date);" in body
    assert "fetchClipsMetadata(cameraId,{date,day_start_utc,day_end_utc})" in body


def test_customer_recordings_metadata_prefers_browser_bounds_over_server_timezone_guess(monkeypatch):
    captured = {}

    def fake_range(camera_id, query_start, query_end):
        captured["range"] = (camera_id, query_start, query_end)
        return []

    def fake_for_date(camera_id, date):
        captured["fallback_date"] = (camera_id, date)
        return []

    monkeypatch.setattr(main, "_recordings_overlapping_utc_range", fake_range)
    monkeypatch.setattr(main, "_customer_recordings_for_date", fake_for_date)
    monkeypatch.setattr(main, "_customer_authorized_camera_id", lambda request, camera_id: True)

    request = SimpleNamespace()
    result = main.customer_recordings_metadata(
        "cam-1", request, date="2026-09-15",
        day_start_utc="2026-09-15T05:00:00", day_end_utc="2026-09-16T05:00:00",
    )
    assert result == {"clips": []}
    assert captured["range"] == ("cam-1", "2026-09-15T05:00:00", "2026-09-16T05:00:00")
    assert "fallback_date" not in captured  # the APPLIANCE_TIMEZONE guess must never run once real bounds are given


def test_customer_recordings_metadata_falls_back_to_appliance_timezone_when_bounds_absent(monkeypatch):
    captured = {}

    def fake_for_date(camera_id, date):
        captured["fallback_date"] = (camera_id, date)
        return []

    monkeypatch.setattr(main, "_customer_recordings_for_date", fake_for_date)
    monkeypatch.setattr(main, "_customer_authorized_camera_id", lambda request, camera_id: True)

    request = SimpleNamespace()
    result = main.customer_recordings_metadata("cam-1", request, date="2026-09-15")
    assert result == {"clips": []}
    assert captured["fallback_date"] == ("cam-1", "2026-09-15")


def test_recordings_overlapping_utc_range_is_a_pure_overlap_query(monkeypatch):
    """A recording entirely before the requested range's start must never
    be returned -- this is the exact shape of the customer-reported bug
    ("recordings from last night appear on today's timeline") once it's
    reached the database layer: a recording that both started and ended
    before query_start must not overlap [query_start, query_end)."""
    calls = []

    class _FakeCursor(list):
        def fetchall(self):
            return self

    class _FakeDb:
        def execute(self, query, params):
            calls.append((query, params))
            camera_id, query_end, query_start = params
            rows = [
                {"id": "last-night", "s3_key": "k1", "started_at": "2026-09-14T22:00:00", "ended_at": "2026-09-14T22:05:00"},
                {"id": "today", "s3_key": "k2", "started_at": "2026-09-15T14:00:00", "ended_at": "2026-09-15T14:05:00"},
            ]
            return _FakeCursor(row for row in rows if row["started_at"] < query_end and row["ended_at"] > query_start)

    class _FakeConn:
        def __enter__(self):
            return _FakeDb()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(main, "_catalog_local_recordings_for_camera", lambda camera_id: None)
    fake_partner_db = SimpleNamespace(connection=lambda: _FakeConn())
    monkeypatch.setitem(__import__("sys").modules, "partner_db", fake_partner_db)

    result = main._recordings_overlapping_utc_range("cam-1", "2026-09-15T05:00:00", "2026-09-16T05:00:00")
    ids = [row["id"] for row in result]
    assert ids == ["today"]
    assert "last-night" not in ids
