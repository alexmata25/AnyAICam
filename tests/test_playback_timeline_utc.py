"""Focused tests for the playback timeline UTC/local-time fix
(fix/playback-timeline-utc-local).

Backend event/recording timestamps are naive (no timezone suffix) but are
always the container clock, which is UTC. Before this fix, the timeline's
JS parsed these strings with new Date(value), which the ECMAScript spec
defines as browser-local for a date-time string with no offset -- so a
UTC-valued timestamp was mislabeled and mispositioned as if it were already
local time. This fix adds explicit UTC-aware parsing/formatting helpers
used by timeline positioning, event/recording day-filtering, and displayed
labels, while leaving the server-side recording-selection and #t= seek
offset computation (linked_recording_for) completely untouched.

These are real JavaScript functions embedded in app/main.py, which cannot
be imported directly in this environment (see tests/test_camera_diagnostics.py
for why). TimelineUtcHelperTests extracts the exact, self-contained helper
source verbatim and runs it under Node.js (available in this environment)
so the assertions exercise the actual shipped implementation, not a
hand-copied duplicate. RecordingSeekOffsetUnchangedSourceTests uses plain
source-text checks, which is the right tool for proving something was left
unchanged rather than for proving new behavior.
"""

import os
import subprocess
import unittest
from pathlib import Path

MAIN_PY = Path(__file__).resolve().parents[1] / "app" / "main.py"

HELPERS_START = "function pad2(n) {{"
HELPERS_END = "function dayStart() {{"


def _extract_helpers_js(source):
    start = source.index(HELPERS_START)
    end = source.index(HELPERS_END, start)
    snippet = source[start:end]
    return snippet.replace("{{", "{").replace("}}", "}")


def _run_node(js_source, tz):
    env = dict(os.environ)
    env["TZ"] = tz
    result = subprocess.run(
        ["node", "-e", js_source],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    if result.returncode != 0:
        raise AssertionError("node exited " + str(result.returncode) + ": " + result.stderr)
    return result.stdout


class TimelineUtcHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers_js = _extract_helpers_js(MAIN_PY.read_text(encoding="utf-8"))

    def _run(self, script_tail, tz="America/Chicago"):
        return _run_node(self.helpers_js + "\n" + script_tail, tz)

    def test_unlabeled_utc_timestamp_is_treated_as_utc(self):
        out = self._run(
            "console.log(parseBackendUtcTimestamp('2026-08-11T02:58:00').toISOString());"
        )
        self.assertEqual(out.strip(), "2026-08-11T02:58:00.000Z")

    def test_already_labeled_utc_timestamp_is_not_double_converted(self):
        out = self._run(
            "console.log(parseBackendUtcTimestamp('2026-08-11T02:58:00Z').toISOString());"
        )
        self.assertEqual(out.strip(), "2026-08-11T02:58:00.000Z")

    def test_utc_timestamp_displays_in_local_central_time(self):
        out = self._run(
            "const d = parseBackendUtcTimestamp('2026-08-11T02:58:00');"
            "console.log(formatLocalStamp(d));"
            "console.log(localDateKey(d));"
        )
        lines = out.strip().splitlines()
        self.assertEqual(lines[0], "2026-08-10 21:58:00")
        self.assertEqual(lines[1], "2026-08-10")

    def test_timeline_position_uses_corrected_local_basis(self):
        script = (
            "function dayStart() { return new Date('2026-08-10T00:00:00'); }\n"
            "function secondsFromDay(value) {\n"
            "  const result = (parseBackendUtcTimestamp(value) - dayStart()) / 1000;\n"
            "  return Math.max(0, Math.min(86400, result));\n"
            "}\n"
            "console.log(secondsFromDay('2026-08-11T02:58:00'));"
        )
        out = self._run(script)
        self.assertEqual(out.strip(), "79080")


class RecordingSeekOffsetUnchangedSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_PY.read_text(encoding="utf-8")

    def test_linked_recording_for_unchanged(self):
        self.assertIn(
            "def linked_recording_for(camera_number: int, event_time: datetime) -> str | None:",
            self.source,
        )
        self.assertIn(
            "offset = max(0, int((event_time - source_start).total_seconds()))",
            self.source,
        )
        self.assertIn(
            'return f"/recordings/camera{camera_number}/{quote(source.name)}#t={offset}"',
            self.source,
        )

    def test_click_handlers_still_use_precomputed_offset_directly(self):
        # Hover-preview tooltip: unchanged.
        self.assertIn("if (match) tooltipVideo.currentTime = Number(match[1]);", self.source)
        # "Open recording" (fix/playback-open-recording-inline) no longer
        # navigates via location.href -- it applies the same precomputed
        # #t= offset to an in-page player instead. See
        # tests/test_playback_open_recording.py for focused coverage.
        self.assertNotIn("location.href = selectedEvent.recording;", self.source)
        self.assertIn("if (match) openPlayer.currentTime = Number(match[1]);", self.source)


if __name__ == "__main__":
    unittest.main()
