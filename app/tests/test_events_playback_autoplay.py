"""Event-specific Playback autoplay (2026-09-02): clicking "Playback" on
an /events row must open the event's own camera, select the recording
covering that event, seek to the event's exact offset inside it, and
call playClip() -- without making ordinary /playback navigation or
camera switching ever autoplay. The signal is an explicit
?autoplay=event query param (never inferred from ?t= alone), and ?t=
itself is now an unambiguous UTC epoch-millisecond integer -- never a
timezone-naive string a browser's Date parser could misread -- per
Alejandro's explicit constraint after the prior fix's t= was suspected
of being interpreted incorrectly.

Two layers are tested: the Python href/embedding layer directly, and
the actual rendered JS's pure timestamp functions (playbackDate,
findClipNear) executed for real in Node, in a real America/Chicago
timezone, against real epoch-ms/naive-string data -- not just asserted
as text -- since a source-only assertion would not have caught the
original t= bug class.
"""

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import main


def _fake_request(params=None):
    params = params or {}
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: params.get(key, default)))


# --------------------------------------------------------- server-side: the link itself


def test_customer_event_actions_link_has_camera_epoch_ms_timestamp_and_autoplay_signal():
    html = main._customer_event_actions("cam-living-room", "2026-09-02T00:38:58.419155")
    m = re.search(r'href="(/playback\?[^"]+)"', html)
    assert m, html
    href = m.group(1)
    assert "camera=cam-living-room" in href
    t_match = re.search(r"[?&]t=(\d+)(&|$)", href)
    assert t_match, f"no numeric t= in {href}"
    assert "autoplay=event" in href
    # The exact, independently-computed epoch-ms value for this UTC instant.
    expected_ms = int(datetime(2026, 9, 2, 0, 38, 58, 419155, tzinfo=timezone.utc).timestamp() * 1000)
    assert int(t_match.group(1)) == expected_ms


def test_customer_event_actions_without_timestamp_has_no_autoplay_signal():
    html = main._customer_event_actions("cam-1")
    assert "autoplay=event" not in html
    assert 'href="/playback?camera=cam-1"' in html


def test_naive_utc_timestamp_to_epoch_ms_matches_independent_computation():
    assert main._naive_utc_timestamp_to_epoch_ms("2026-09-02T00:38:58") == int(
        datetime(2026, 9, 2, 0, 38, 58, tzinfo=timezone.utc).timestamp() * 1000
    )
    assert main._naive_utc_timestamp_to_epoch_ms("not-a-timestamp") is None


# --------------------------------------------------------- server-side: the flag is explicit, never inferred


def test_autoplay_flag_only_set_when_query_param_is_exactly_event(monkeypatch):
    cameras = [{"id": "cam-1", "name": "Front Door", "camera_number": 1}]
    monkeypatch.setattr(main, "_customer_recording_rows", lambda camera_id, **kw: [])
    monkeypatch.setattr(main, "_customer_detection_events", lambda request: [])

    html_event = main._render_customer_playback(cameras, _fake_request({"camera": "cam-1", "t": "123", "autoplay": "event"}))
    assert "const autoplayFromEvent=true;" in html_event

    # t= present but WITHOUT the exact marker -- ordinary/future deep
    # link, must stay non-autoplay.
    html_bare_t = main._render_customer_playback(cameras, _fake_request({"camera": "cam-1", "t": "123"}))
    assert "const autoplayFromEvent=false;" in html_bare_t

    # Ordinary Playback navigation: no params at all.
    html_ordinary = main._render_customer_playback(cameras, _fake_request({}))
    assert "const autoplayFromEvent=false;" in html_ordinary


def test_camera_switch_path_never_calls_render_camera_with_a_seek_timestamp():
    """cameraTiles' click handler must call renderCamera() with no
    argument -- the one thing that keeps camera switching on the
    select-only branch regardless of autoplayFromEvent."""
    html = main._render_customer_playback(
        [{"id": "cam-1", "name": "Front Door", "camera_number": 1}], _fake_request({})
    )
    assert "await renderCamera();" in html


# --------------------------------------------------------- real JS execution: playbackDate/findClipNear


def _extract_function(html: str, name: str) -> str:
    """Exact source of one top-level `function name(...){...}` from the
    rendered page, found by brace-depth counting rather than a fragile
    string marker (the naive "next \\n  }" search matches a nested
    block's closing brace, not the function's own, for any function
    with an inner {}-block -- which findClipNear has)."""
    start = html.index(f"function {name}(")
    brace_start = html.index("{", start)
    depth = 0
    pos = brace_start
    while True:
        ch = html[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[start:pos + 1]
        pos += 1


def _extract_pure_functions(html: str) -> str:
    # Only the two pure, DOM-free functions under test -- deliberately
    # not a text slice of everything in between, which pulls in
    # top-level statements (e.g. browseButton.addEventListener(...))
    # that execute immediately and throw with no DOM present.
    return _extract_function(html, "playbackDate") + "\n" + _extract_function(html, "findClipNear")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed in this environment")
def test_findclipnear_matches_covering_recording_for_a_real_epoch_ms_deep_link(tmp_path):
    """Real event/recording pair traced 2026-09-02 (Living Room,
    camera_id 498413234d): event at 2026-09-02T00:38:58.419155 UTC
    inside recording 2026-09-02T00:35:42 - 2026-09-02T00:40:41.992562."""
    html = main._render_customer_playback(
        [{"id": "cam-1", "name": "Living Room", "camera_number": 5}], _fake_request({})
    )
    functions_js = _extract_pure_functions(html)

    epoch_ms = main._naive_utc_timestamp_to_epoch_ms("2026-09-02T00:38:58.419155")
    clips = [
        {"start": "2026-09-01T22:55:41", "end": "2026-09-01T23:00:41.738815"},
        {"start": "2026-09-02T00:35:42", "end": "2026-09-02T00:40:41.992562"},
    ]

    script = f"""
{functions_js}
const clips = {json.dumps(clips)};
const nearby = findClipNear(clips, {epoch_ms});
console.log(JSON.stringify(nearby));
"""
    script_path = tmp_path / "find_clip_near_check.js"
    script_path.write_text(script)
    result = subprocess.run(
        ["node", str(script_path)],
        capture_output=True, text=True, check=True,
        env={"TZ": "America/Chicago", "PATH": __import__("os").environ["PATH"]},
    )
    matched = json.loads(result.stdout.strip())
    assert matched is not None
    assert matched["start"] == "2026-09-02T00:35:42"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed in this environment")
def test_seek_offset_computation_lands_within_the_recording_not_at_zero(tmp_path):
    """The same real event/recording pair -- the offset math the
    autoplay branch uses to seek must land inside [0, duration], not
    at 0 (which would silently discard "seek to the event moment") and
    not negative/past the end (which would be a parsing regression)."""
    html = main._render_customer_playback(
        [{"id": "cam-1", "name": "Living Room", "camera_number": 5}], _fake_request({})
    )
    functions_js = _extract_pure_functions(html)
    epoch_ms = main._naive_utc_timestamp_to_epoch_ms("2026-09-02T00:38:58.419155")
    clip_start = "2026-09-02T00:35:42"
    clip_end = "2026-09-02T00:40:41.992562"

    script = f"""
{functions_js}
const offsetSeconds=(playbackDate({epoch_ms}).getTime()-playbackDate({json.dumps(clip_start)}).getTime())/1000;
const durationSeconds=(playbackDate({json.dumps(clip_end)}).getTime()-playbackDate({json.dumps(clip_start)}).getTime())/1000;
console.log(JSON.stringify({{offsetSeconds, durationSeconds}}));
"""
    script_path = tmp_path / "seek_offset_check.js"
    script_path.write_text(script)
    result = subprocess.run(
        ["node", str(script_path)],
        capture_output=True, text=True, check=True,
        env={"TZ": "America/Chicago", "PATH": __import__("os").environ["PATH"]},
    )
    data = json.loads(result.stdout.strip())
    assert 0 < data["offsetSeconds"] < data["durationSeconds"]
    assert round(data["offsetSeconds"]) == 196  # 00:38:58.419 - 00:35:42 = 196.4s


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed in this environment")
def test_playbackdate_handles_epoch_ms_number_and_naive_string_identically_to_utc(tmp_path):
    html = main._render_customer_playback(
        [{"id": "cam-1", "name": "Front Door", "camera_number": 1}], _fake_request({})
    )
    functions_js = _extract_pure_functions(html)
    epoch_ms = main._naive_utc_timestamp_to_epoch_ms("2026-09-02T00:38:58")
    script = f"""
{functions_js}
const fromNumber=playbackDate({epoch_ms}).getTime();
const fromNaiveString=playbackDate({json.dumps("2026-09-02T00:38:58")}).getTime();
console.log(JSON.stringify({{fromNumber, fromNaiveString, equal: fromNumber===fromNaiveString}}));
"""
    script_path = tmp_path / "playbackdate_equiv_check.js"
    script_path.write_text(script)
    result = subprocess.run(
        ["node", str(script_path)],
        capture_output=True, text=True, check=True,
        env={"TZ": "America/Chicago", "PATH": __import__("os").environ["PATH"]},
    )
    data = json.loads(result.stdout.strip())
    assert data["equal"] is True


# --------------------------------------------------------- static branch checks: playClip is only called on the flag


def test_autoplay_branch_calls_playclip_and_seeks_before_it():
    html = main._render_customer_playback(
        [{"id": "cam-1", "name": "Front Door", "camera_number": 1}], _fake_request({})
    )
    i = html.index("if(seekTimestamp){")
    j = html.index("}else if(clips.length){", i)
    branch = html[i:j]
    assert "if(autoplayFromEvent){" in branch
    seek_i = branch.index("video.addEventListener('loadedmetadata'")
    play_i = branch.index("playClip(cameraId,nearby);")
    assert seek_i < play_i  # listener registered before the load it must catch
    assert "selectedClip=nearby;" in branch  # the non-autoplay else-branch is still present, unchanged
