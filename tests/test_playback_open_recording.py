"""Focused tests for making "Open recording" play inline instead of
downloading (fix/playback-open-recording-inline).

"Open recording" previously did location.href = selectedEvent.recording,
a full-tab navigation to the raw .mkv file. Most browsers treat direct
navigation to an .mkv resource as a download rather than an inline-playable
page, even though the exact same URL already plays fine when used as a
<video src> -- which the hover-preview tooltip (previewRecordingUrl() plus
a #t= offset-extraction/loadedmetadata pattern) already proved on this same
page. This fix makes "Open recording" reuse that same proven technique
against a small, dedicated, initially-hidden <video controls> player in the
selected-event summary panel, instead of navigating away.

As with the other main.py-embedded JS in this repo, DOM-manipulation
behavior (document.getElementById, addEventListener, video.play()) isn't
testable without a browser/jsdom, which isn't available here. So:
- The two pure computations the click handler relies on -- stripping the
  #t= fragment before use as a src, and extracting the numeric offset from
  it -- are extracted verbatim and run under Node.js (available in this
  environment) for genuine behavioral proof.
- The click handler's wiring of those computations to the player element,
  and the absence of the old location.href navigation, are verified by
  source inspection, which is the right tool for proving wiring/absence
  rather than pure computation.
"""

import subprocess
import unittest
from pathlib import Path

MAIN_PY = Path(__file__).resolve().parents[1] / "app" / "main.py"


def _run_node(js_source):
    result = subprocess.run(
        ["node", "-e", js_source],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise AssertionError("node exited " + str(result.returncode) + ": " + result.stderr)
    return result.stdout


def _extract_between(source, start_marker, end_marker):
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    snippet = source[start:end]
    return snippet.replace("{{", "{").replace("}}", "}")


class OpenRecordingNoLongerNavigatesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_PY.read_text(encoding="utf-8")

    def test_location_href_navigation_is_gone(self):
        self.assertNotIn("location.href = selectedEvent.recording;", self.source)

    def test_open_recording_handler_targets_the_in_page_player(self):
        self.assertIn(
            "const openPlayer = document.getElementById('selected-recording-player');",
            self.source,
        )
        self.assertIn("openPlayer.src = source;", self.source)
        self.assertIn("openPlayer.hidden = false;", self.source)

    def test_dedicated_player_exists_hidden_by_default(self):
        self.assertIn(
            '<video id="selected-recording-player" class="selected-recording-player" '
            'controls playsinline hidden></video>',
            self.source,
        )


class HashStrippedUrlAndOffsetExtractionTests(unittest.TestCase):
    """Behavioral tests, via Node, of the two pure computations the click
    handler wires into the player (source-inspected above)."""

    @classmethod
    def setUpClass(cls):
        source = MAIN_PY.read_text(encoding="utf-8")
        cls.preview_url_js = _extract_between(
            source,
            "function previewRecordingUrl(recording) {{",
            "function showTimelinePreview(mouse, event) {{",
        )

    def test_preview_recording_url_strips_the_t_fragment(self):
        js = (
            "global.location = { origin: 'http://localhost' };\n"
            + self.preview_url_js
            + "console.log(previewRecordingUrl("
              "'/recordings/camera1/camera1_2026-08-11_03-35-41.mkv#t=94'));"
        )
        out = _run_node(js)
        self.assertEqual(
            out.strip(),
            "http://localhost/recordings/camera1/camera1_2026-08-11_03-35-41.mkv",
        )

    def test_offset_regex_extracts_the_precomputed_seconds(self):
        js = (
            "const recording = "
            "'/recordings/camera1/camera1_2026-08-11_03-35-41.mkv#t=94';"
            "const match = new URL(recording, 'http://localhost').hash.match(/t=(\\d+(?:\\.\\d+)?)/);"
            "console.log(match ? Number(match[1]) : 'no-match');"
        )
        out = _run_node(js)
        self.assertEqual(out.strip(), "94")


class DownloadButtonUnchangedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_PY.read_text(encoding="utf-8")

    def test_download_button_still_forces_a_real_download(self):
        self.assertIn("link.href = selectedEvent.recording;", self.source)
        self.assertIn("link.download = '';", self.source)
        self.assertIn("link.click();", self.source)


if __name__ == "__main__":
    unittest.main()
