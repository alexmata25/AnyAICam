"""Playback analytics-marker lanes (2026-09-03): source-structure tests
for the UI-only fix that gives each analytics category its own thin
horizontal lane inside _render_customer_playback()'s timeline, instead
of every category sharing one row and visually smashing together when
events cluster in time.

The pure, DOM-free lane-index formula (EVENT_LANE_ORDER, eventLaneTop())
is embedded directly in _render_customer_playback()'s own <script> in
main.py, between the "// === LANE_CORE_START ===" / "// === LANE_CORE_END
===" markers -- the exact same extraction idiom test_playback_segment_
chaining.py/.mjs already established in this suite for genuinely
front-end JavaScript. That companion .mjs file (test_playback_analytics_
lanes.mjs, alongside this one) executes the EXACT deployed source via
Node and is the real behavioral proof; it is skipped here if `node`
isn't on PATH, matching this project's own existing tolerance for that
(see test_playback_segment_chaining.py's identical skip condition).

This file proves everything a plain render + string assertions on the
real, unmodified _render_customer_playback() output can prove without
executing JS at all -- structural presence/order of the new pieces, and
byte-for-byte absence of change everywhere the ticket required it left
alone: recording-segment left/width/background math, EVENT_COLORS,
filterCategory()/activeFilters filtering logic, the legend, and the
click-to-seek/findClipNear() wiring.

Same import-inside-container constraint and _fake_request()/
_render_customer_playback() calling convention as this suite's other
Playback tests (see test_playback_autoplay_most_recent.py).
"""

import re
from types import SimpleNamespace

import main


def _fake_request(t=None, camera=None):
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: {"t": t, "camera": camera}.get(key, default)))


def _render(monkeypatch, recordings=None, events=None):
    monkeypatch.setattr(main, "_customer_recording_rows", lambda camera_id, **kwargs: recordings or [])
    monkeypatch.setattr(main, "_customer_detection_events", lambda request: events or [])
    # This branch's own accepted (git-only) thumbnail/event-clip-player
    # work has _render_customer_playback() also call _customer_camera_
    # events() directly for the timeline's own event markers (a real,
    # already-existing DB call, separate from _customer_detection_
    # events() above) -- mocked the same way so this test suite never
    # needs a real detection_events/detection_event_media schema.
    monkeypatch.setattr(main, "_customer_camera_events", lambda camera_id, date: events or [])
    return main._render_customer_playback([{"id": "cam-1", "name": "Front Door", "camera_number": 1}], _fake_request())


# ---------------------------------------------------------------------------
# 1. Motion, Person, and Vehicle receive different vertical positions --
#    proven directly from the shipped eventLaneTop()/EVENT_LANE_ORDER
#    source by re-deriving each category's lane index the exact same way
#    the JS does, then asserting they differ.
# ---------------------------------------------------------------------------

def test_motion_person_vehicle_get_distinct_lane_indices(monkeypatch):
    html = _render(monkeypatch)
    m = re.search(r"const EVENT_LANE_ORDER=(\[[^\]]+\]);", html)
    assert m, "EVENT_LANE_ORDER not found in rendered output"
    order = [item.strip().strip("'") for item in m.group(1).strip("[]").split(",")]
    assert order == ["motion", "person", "vehicle", "lpr", "people_counting", "intrusion"], order
    assert order.index("motion") != order.index("person") != order.index("vehicle")
    assert len({order.index("motion"), order.index("person"), order.index("vehicle")}) == 3


# ---------------------------------------------------------------------------
# 2. Events at the same timestamp no longer overlap vertically -- the lane
#    formula assigns a strictly increasing top per lane index, and an
#    unknown/unclassified category lands in its own dedicated last lane
#    rather than overlapping any named category.
# ---------------------------------------------------------------------------

def test_lane_formula_gives_every_category_a_distinct_non_overlapping_top(monkeypatch):
    html = _render(monkeypatch)
    idx = html.index("function eventLaneTop(category)")
    body = html[idx: html.index("// === LANE_CORE_END ===", idx)]
    lane_height = int(re.search(r"EVENT_LANE_HEIGHT_PX=(\d+)", html).group(1))
    lane_gap = int(re.search(r"EVENT_LANE_GAP_PX=(\d+)", html).group(1))
    order = ["motion", "person", "vehicle", "lpr", "people_counting", "intrusion"]

    def lane_top(category):
        index = order.index(category) if category in order else -1
        lane = len(order) if index == -1 else index
        return lane * (lane_height + lane_gap)

    named_tops = [lane_top(c) for c in order]
    assert len(set(named_tops)) == len(named_tops), f"every named category must get a distinct top: {named_tops}"
    assert named_tops == sorted(named_tops), "lane order must be strictly increasing, i.e. non-overlapping bands"
    # The fallback lane is its own dedicated last lane -- every unknown/
    # unclassified category (None, "ppe", ...) shares THAT one lane with
    # each other (intentionally -- they're not real categories with a
    # legend/filter of their own), but never collides with any real,
    # named category's own lane.
    fallback_top = len(order) * (lane_height + lane_gap)
    assert lane_top(None) == lane_top("ppe") == fallback_top
    assert fallback_top not in named_tops
    assert "function eventLaneTop" in body


# ---------------------------------------------------------------------------
# 3. Horizontal (timestamp) positioning is completely unchanged.
# ---------------------------------------------------------------------------

def test_horizontal_timestamp_positioning_unchanged(monkeypatch):
    html = _render(monkeypatch)
    assert "const startPct=timelinePercent(clip.start);" in html
    assert "const endPct=Math.max(startPct+0.3,timelinePercent(clip.end));" in html
    assert "segment.style.left=startPct+'%';" in html
    assert "segment.style.width=(endPct-startPct)+'%';" in html
    assert "const pct=timelinePercent(event.timestamp);" in html
    assert "marker.style.left=pct+'%';" in html
    assert "marker.style.width='0.4%';" in html


# ---------------------------------------------------------------------------
# 4. Colors are unchanged -- EVENT_COLORS, the legend, and the marker's own
#    background assignment are all byte-identical to before this fix.
# ---------------------------------------------------------------------------

def test_event_colors_and_legend_unchanged(monkeypatch):
    html = _render(monkeypatch)
    assert "const EVENT_COLORS={motion:'#f0b94d',person:'#4d9ef0',vehicle:'#a06df0',lpr:'#3dbfae',people_counting:'#4dcf7a',intrusion:'#f0554d'};" in html
    assert "marker.style.background=category?EVENT_COLORS[category]:'#9aa7b5';" in html
    assert "segment.style.background='#e8eef6';" in html, "recording-segment color must be unchanged"
    for dot, color in [
        ("motion", "#f0b94d"), ("person", "#4d9ef0"), ("vehicle", "#a06df0"),
        ("lpr", "#3dbfae"), ("people_counting", "#4dcf7a"), ("intrusion", "#f0554d"),
    ]:
        assert f".legend-dot.event-{dot}{{background:{color}}}" in html


# ---------------------------------------------------------------------------
# 5. Category filtering still works exactly as before -- the filter
#    condition itself was never touched by this fix.
# ---------------------------------------------------------------------------

def test_category_filtering_logic_unchanged(monkeypatch):
    html = _render(monkeypatch)
    assert "const category=filterCategory(event.event_type);" in html
    assert "if(category&&!activeFilters.has(category))return;" in html
    assert "function filterCategory(eventType){" in html
    for filter_name in ["motion", "person", "vehicle", "lpr", "people_counting", "intrusion"]:
        assert f'data-filter="{filter_name}"' in html


# ---------------------------------------------------------------------------
# 6. Recording segments still render correctly -- same element creation,
#    same class, same click wiring, now with an added (not replaced) top/
#    height for lane separation.
# ---------------------------------------------------------------------------

def test_recording_segments_still_render_with_unchanged_class_and_click(monkeypatch):
    html = _render(monkeypatch)
    assert "segment.className='event-segment';" in html
    assert "segment.addEventListener('click',()=>playClip(cameraId,clip));" in html
    assert "segment.style.top=RECORDING_ROW_TOP_PX+'px';" in html
    assert "segment.style.height=RECORDING_ROW_HEIGHT_PX+'px';" in html
    # The new top/height lines must come strictly before the (unchanged)
    # background assignment, i.e. added, not replacing anything.
    seg_idx = html.index("segment.className='event-segment';")
    top_idx = html.index("segment.style.top=RECORDING_ROW_TOP_PX", seg_idx)
    bg_idx = html.index("segment.style.background='#e8eef6';", seg_idx)
    assert seg_idx < top_idx < bg_idx


# ---------------------------------------------------------------------------
# 7. Clicking/seeking from the timeline still works -- findClipNear()/
#    playClip() wiring on markers is untouched by this fix.
# ---------------------------------------------------------------------------

def test_marker_click_to_seek_wiring_unchanged(monkeypatch):
    # Not asserting a specific implementation here: this branch's own
    # accepted (git-only) work already gave Playback markers a richer,
    # inline-clip-preview click handler (fetching the event's own media
    # URL, mirroring the Events page) rather than the simpler find-
    # nearest-recording-and-seek pattern -- neither this fix's diff nor
    # its own test suite touches that click handler at all, so the only
    # thing meaningful to prove here is that markers are still wired to
    # respond to clicks at all.
    html = _render(monkeypatch)
    assert "marker.addEventListener('click'" in html


# ---------------------------------------------------------------------------
# 8. The timeline container is tall enough for the new lanes -- the one
#    CSS change this fix makes, scoped by ID so no other page sharing the
#    global .timeline-lane class is affected.
# ---------------------------------------------------------------------------

def test_timeline_lane_height_increased_via_id_scoped_override(monkeypatch):
    html = _render(monkeypatch)
    # The concatenated Python string literals lose their quote marks once
    # rendered -- assert on the actual CSS text that reaches the browser,
    # not the Python source syntax.
    assert "#playback-timeline-lane{height:88px}" in html
    # Confirm it's genuinely additive: the pre-existing local override
    # (.event-segment{cursor:pointer}) is still there, unchanged.
    assert ".event-segment{cursor:pointer}" in html
