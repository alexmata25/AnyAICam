"""Focused tests for the camera-page day-event list feature
(feature/camera-page-day-events).

Scope (established investigation, scoped down to #1-#3 of 4 proposed
pieces -- manual-clip entitlement gating is explicitly deferred):

1. On /live (def home()), clicking a camera's live view/image (not just
   the small existing toolbar icon) now opens that camera's dedicated
   /camera/{n} page, via a new openCameraPage(n) helper bound to the
   .camera-view wrapper's onclick. The existing toolbar link ("Open the
   dedicated Camera {n} page") and all other camera-tools buttons are
   unchanged.
2. /camera/{n} (def camera_detail()) now renders a day-scoped motion-event
   list, reusing the exact /playback date-selector UI convention (prev/
   next/today + <input type="date">) and the existing GET /api/events
   endpoint -- extended with a small, backward-compatible optional `date`
   (YYYY-MM-DD) filter. Omitting `date` (every pre-existing caller)
   preserves the endpoint's exact prior behavior.
3. Each rendered event becomes a clickable <a> to its own linked_recording
   when one exists (reusing the same linked_recording field and direct-
   navigation pattern already used by the dashboard widget, analytics
   results, and /playback's own "Open recording" button), or a
   non-clickable <div> with a "No recording linked" note otherwise.

As with the other main.py-embedded logic in this repo, app/main.py can't
be imported directly here. Python-side behavior (the new /api/events date
filter) is proven by extracting the real events_api() source and exec'ing
it with a stubbed load_motion_events(); JavaScript-side behavior (the new
event-list renderer and day-shift helper) is proven by extracting the
real, embedded JS source and running it under Node -- not a hand-copied
duplicate of either. UntouchedSubsystemsSourceTests confirms live
streaming, motion detection, recording, /playback, /api/clips, and the
manual-clip section were not touched by this change.
"""

import json
import subprocess
import unittest
from pathlib import Path

MAIN_PY = Path(__file__).resolve().parents[1] / "app" / "main.py"


def _extract(source: str, start_marker: str, end_marker: str, include_end: bool = True) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    if include_end:
        end += len(end_marker)
    return source[start:end]


def _run_node(js_source: str):
    result = subprocess.run(
        ["node", "-e", js_source],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(f"node exited {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}")
    return result.stdout


class EventsApiDateFilterBehaviorTests(unittest.TestCase):
    """Extracts the real events_api() source (with a stubbed
    load_motion_events()) and executes it directly."""

    @classmethod
    def setUpClass(cls):
        source = MAIN_PY.read_text(encoding="utf-8")

        motion_flag_src = _extract(
            source,
            'MOTION_DETECTION_ENABLED = os.environ.get(',
            '\n',
        )
        function_src = _extract(
            source,
            "def events_api(camera: int | None = None, date: str | None = None, limit: int = 100) -> dict:",
            'return {"events": events[:safe_limit], "motion_detection": MOTION_DETECTION_ENABLED}',
        )

        cls.fixture_events = [
            {"id": "e1", "camera": 1, "start_time": "2026-08-09T23:50:00", "confidence": 40.0},
            {"id": "e2", "camera": 1, "start_time": "2026-08-10T08:15:00", "confidence": 55.0},
            {"id": "e3", "camera": 1, "start_time": "2026-08-10T09:30:00", "confidence": 70.0},
            {"id": "e4", "camera": 2, "start_time": "2026-08-10T09:31:00", "confidence": 65.0},
            {"id": "e5", "camera": 1, "start_time": "2026-08-11T00:05:00", "confidence": 20.0},
        ]

        namespace = {
            "os": __import__("os"),
            "load_motion_events": lambda: list(cls.fixture_events),
        }
        exec(compile(motion_flag_src, "app/main.py (MOTION_DETECTION_ENABLED)", "exec"), namespace)
        exec(compile(function_src, "app/main.py (extracted events_api)", "exec"), namespace)
        cls.events_api = staticmethod(namespace["events_api"])

    def test_no_date_filter_preserves_existing_behavior(self):
        result = self.events_api(camera=1)
        ids = [event["id"] for event in result["events"]]
        self.assertEqual(ids, ["e5", "e3", "e2", "e1"])

    def test_date_filter_scopes_to_that_calendar_day_only(self):
        result = self.events_api(camera=1, date="2026-08-10")
        ids = {event["id"] for event in result["events"]}
        self.assertEqual(ids, {"e2", "e3"})

    def test_date_filter_combines_with_camera_filter(self):
        result = self.events_api(camera=2, date="2026-08-10")
        ids = {event["id"] for event in result["events"]}
        self.assertEqual(ids, {"e4"})

    def test_date_filter_excludes_adjacent_days(self):
        result = self.events_api(camera=1, date="2026-08-10")
        ids = {event["id"] for event in result["events"]}
        self.assertNotIn("e1", ids)
        self.assertNotIn("e5", ids)

    def test_empty_date_string_is_treated_as_no_filter(self):
        result = self.events_api(camera=1, date="")
        ids = [event["id"] for event in result["events"]]
        self.assertEqual(ids, ["e5", "e3", "e2", "e1"])

    def test_still_sorted_newest_first_and_respects_limit(self):
        result = self.events_api(camera=1, limit=2)
        ids = [event["id"] for event in result["events"]]
        self.assertEqual(ids, ["e5", "e3"])


class CameraEventsJavaScriptBehaviorTests(unittest.TestCase):
    """Extracts the real, embedded JS from camera_detail()'s
    events_scripts_template and runs it under Node."""

    @classmethod
    def setUpClass(cls):
        source = MAIN_PY.read_text(encoding="utf-8")
        template_src = _extract(
            source,
            'events_scripts_template = """<script>',
            '</script>"""',
        )
        # Mirror the exact substitution app/main.py performs at request
        # time (events_scripts_template.replace(...).replace(...)) with
        # known test values, rather than exercising the raw, unsubstituted
        # placeholder template.
        cls.today_value = "2026-08-10"
        cls.camera_number = 99
        substituted = template_src.replace(
            "__CAMERA_NUMBER__", str(cls.camera_number)
        ).replace("__TODAY_VALUE__", cls.today_value)
        start = substituted.index("(function()")
        end = substituted.rindex("</script>")
        cls.js_body = substituted[start:end]

    def _run(self, script_tail: str) -> str:
        harness = f"""
'use strict';
const listeners = {{}};
function makeElement(id) {{
  return {{
    id,
    value: '',
    innerHTML: '',
    addEventListener(type, handler) {{ listeners[id + ':' + type] = handler; }},
  }};
}}
const elements = {{
  'camera-events-date': makeElement('camera-events-date'),
  'camera-events-list': makeElement('camera-events-list'),
  'camera-events-prev-day': makeElement('camera-events-prev-day'),
  'camera-events-next-day': makeElement('camera-events-next-day'),
  'camera-events-today': makeElement('camera-events-today'),
}};
const document = {{ getElementById: (id) => elements[id] }};
let fetchCalls = [];
let fetchResponse = {{ events: [] }};
function fetch(url) {{
  fetchCalls.push(url);
  return Promise.resolve({{ json: () => Promise.resolve(fetchResponse) }});
}}
{self.js_body}
{script_tail}
"""
        return _run_node(harness)

    def test_prev_day_button_shifts_date_backward_and_refetches(self):
        output = self._run(r"""
elements['camera-events-date'].value = '2026-08-10';
listeners['camera-events-prev-day:click']();
setTimeout(() => {
  console.log(JSON.stringify({date: elements['camera-events-date'].value, calls: fetchCalls}));
}, 0);
""")
        payload = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(payload["date"], "2026-08-09")
        self.assertTrue(any("camera=99" in url and "date=2026-08-09" in url for url in payload["calls"]))

    def test_next_day_button_shifts_date_forward(self):
        output = self._run(r"""
elements['camera-events-date'].value = '2026-08-10';
listeners['camera-events-next-day:click']();
setTimeout(() => console.log(elements['camera-events-date'].value), 0);
""")
        self.assertEqual(output.strip().splitlines()[-1], "2026-08-11")

    def test_today_button_resets_to_the_rendered_today_value(self):
        output = self._run(r"""
elements['camera-events-date'].value = '2020-01-01';
listeners['camera-events-today:click']();
setTimeout(() => console.log(elements['camera-events-date'].value), 0);
""")
        self.assertEqual(output.strip().splitlines()[-1], "2026-08-10")

    def test_event_with_recording_renders_as_clickable_link(self):
        output = self._run(r"""
fetchResponse = {events: [
  {start_time: '2026-08-10T09:30:00', confidence: 62.5, thumbnail: null,
   linked_recording: '/recordings/camera1/clip.mkv#t=5'}
]};
elements['camera-events-date'].value = '2026-08-10';
listeners['camera-events-date:change']();
setTimeout(() => console.log(elements['camera-events-list'].innerHTML), 0);
""")
        html = output.strip().splitlines()[-1]
        self.assertTrue(html.startswith('<a class="feature-card" href="/recordings/camera1/clip.mkv#t=5">'))
        self.assertIn("62.5% confidence", html)
        self.assertNotIn("No recording linked", html)

    def test_event_without_recording_renders_as_non_clickable_note(self):
        output = self._run(r"""
fetchResponse = {events: [
  {start_time: '2026-08-10T09:30:00', confidence: 12.0, thumbnail: null, linked_recording: null}
]};
elements['camera-events-date'].value = '2026-08-10';
listeners['camera-events-date:change']();
setTimeout(() => console.log(elements['camera-events-list'].innerHTML), 0);
""")
        html = output.strip().splitlines()[-1]
        self.assertTrue(html.startswith('<div class="feature-card">'))
        self.assertIn("No recording linked", html)

    def test_no_events_shows_empty_state(self):
        output = self._run(r"""
fetchResponse = {events: []};
elements['camera-events-date'].value = '2026-08-10';
listeners['camera-events-date:change']();
setTimeout(() => console.log(elements['camera-events-list'].innerHTML), 0);
""")
        html = output.strip().splitlines()[-1]
        self.assertEqual(html, '<div class="empty-stage">No motion events for this day.</div>')

    def test_recording_url_is_html_escaped(self):
        output = self._run(r"""
fetchResponse = {events: [
  {start_time: '2026-08-10T09:30:00', confidence: 1, thumbnail: null,
   linked_recording: '"><script>alert(1)</script>'}
]};
elements['camera-events-date'].value = '2026-08-10';
listeners['camera-events-date:change']();
setTimeout(() => console.log(elements['camera-events-list'].innerHTML), 0);
""")
        html = output.strip().splitlines()[-1]
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


class RouteWiringSourceTests(unittest.TestCase):
    """Source-inspection proof that the new pieces are wired together the
    way the behavioral tests above assume, and that the click-to-open
    change on /live is additive alongside the existing toolbar link."""

    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_PY.read_text(encoding="utf-8")

    def test_camera_view_click_opens_dedicated_camera_page(self):
        self.assertIn(
            '<div class="camera-view" style="cursor:pointer" onclick="openCameraPage({n})">',
            self.source,
        )
        self.assertIn("function openCameraPage(n){location.href=`/camera/${n}`}", self.source)

    def test_existing_toolbar_open_camera_link_is_unchanged(self):
        self.assertIn(
            '<a class="camera-action" title="Open the dedicated Camera {n} page" href="/camera/{n}">',
            self.source,
        )

    def test_camera_detail_computes_today_and_appends_events_section(self):
        self.assertIn('    today = datetime.now().strftime("%Y-%m-%d")', self.source)
        self.assertIn(
            'return page_shell(f"Camera {camera_number}", "live", content + events_section, scripts + events_scripts)',
            self.source,
        )

    def test_events_fetch_is_scoped_to_this_camera_number(self):
        self.assertIn(
            "fetch('/api/events?camera=__CAMERA_NUMBER__&date='+dateInput.value+'&limit=100')",
            self.source,
        )
        self.assertIn(
            'events_scripts = events_scripts_template.replace("__CAMERA_NUMBER__", str(camera_number))'
            '.replace("__TODAY_VALUE__", today)',
            self.source,
        )

    def test_camera_detail_route_still_validates_camera_number(self):
        self.assertIn('@app.get("/camera/{camera_number}", response_class=HTMLResponse)', self.source)
        self.assertIn("def camera_detail(camera_number: int) -> str:", self.source)
        self.assertIn('raise HTTPException(status_code=404, detail="Camera not found")', self.source)


class UntouchedSubsystemsSourceTests(unittest.TestCase):
    """Confirms live streaming, motion detection, recording, /playback,
    /api/clips, and the manual-clip section are untouched -- manual-clip
    entitlement gating was explicitly deferred out of this change."""

    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_PY.read_text(encoding="utf-8")

    def test_live_hls_stream_wiring_unchanged(self):
        self.assertIn("function connectCamera(n){const video=document.getElementById(`camera${n}`)", self.source)
        self.assertIn('source=`/static/hls/camera${n}.m3u8`', self.source)

    def test_camera_grid_video_element_unchanged(self):
        self.assertIn(
            '<video id="camera{n}" autoplay muted controls playsinline aria-label="Camera {n} live stream"></video>',
            self.source,
        )

    def test_motion_detection_and_recording_helpers_unchanged(self):
        self.assertIn("async def store_motion_event(", self.source)
        self.assertIn("def linked_recording_for(camera_number: int, event_time: datetime) -> str | None:", self.source)

    def test_playback_route_and_date_selector_pattern_untouched(self):
        self.assertIn('@app.get("/playback", response_class=HTMLResponse)', self.source)
        self.assertIn(
            '<button id="previous-day">&lt;</button><input id="monitor-date" type="date" value="{today}">'
            '<button id="next-day">&gt;</button><button id="today-button">Today</button>',
            self.source,
        )

    def test_manual_clip_section_and_api_untouched(self):
        self.assertIn(
            '<div class="panel-head"><div><h2>Create manual clip</h2>'
            '<div class="health-detail">Create a clip from completed recordings.</div></div></div>',
            self.source,
        )
        self.assertIn('@app.post("/api/clips")', self.source)
        self.assertIn("async def create_clip(request: ClipRequest) -> dict:", self.source)
        self.assertIn('return {"status": "error", "message": "Manual clips are limited to one hour."}', self.source)

    def test_no_recording_plan_or_entitlement_gating_was_introduced(self):
        # Explicitly deferred per instruction -- this change must not add
        # any new recording-plan/entitlement flag or check.
        self.assertNotIn("continuous_recording", self.source)


if __name__ == "__main__":
    unittest.main()
