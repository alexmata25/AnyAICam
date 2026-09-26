"""Inline event-clip player on /events (2026-09-02): clicking a
has_event_clip=true event's thumbnail now plays that event's own short
clip inline in the Events table, instead of requiring a navigation to
/playback?event=... . A same-page click is a genuine, synchronous user
gesture -- the browser's best real chance of allowing audible
autoplay, unlike a cross-page navigation where activation does not
reliably carry over to the freshly-loaded document. Reuses the exact
same authorized /api/customer/events/{camera_id}/{event_id}/media/url
route the deep-link path already uses -- no second media API, no
change to that route's authorization. The /playback?event=... deep
link keeps working unchanged as a secondary path.
"""

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import main


def _fake_request():
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: default))


def _render(events, monkeypatch, cameras=None):
    monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: cameras or [
        {"id": "cam-1", "name": "Front Door", "camera_number": 1}
    ])
    monkeypatch.setattr(main, "_customer_detection_events", lambda request, **kwargs: events)
    return main._render_customer_events(_fake_request())


def _table_body(html):
    """Just the <tbody>...</tbody> markup -- P0 #5 Phase 3 (2026-09-05)
    added a client-side row-builder to the page's own <script> block
    whose JS source text necessarily contains the same
    class="event-thumb-player" string this suite greps for (it has to,
    to build that markup once media becomes ready) -- asserting against
    the whole page would false-positive on that JS source, not the
    actual server-rendered row."""
    start = html.index("<tbody>")
    end = html.index("</tbody>") + len("</tbody>")
    return html[start:end]


# --------------------------------------------------------- HTML generation


def test_playable_event_thumbnail_gets_a_clickable_inline_player_wrapper(monkeypatch):
    html = _render([{
        "id": "ev-1", "camera": 1, "camera_id": "cam-1", "camera_name": "Front Door",
        "event_type": "motion", "timestamp": "2026-09-02T00:38:58", "confidence": 0.9,
        "thumbnail": "/api/customer/recordings/cam-1/r1/thumbnail", "has_event_clip": True,
    }], monkeypatch)
    assert 'class="event-thumb-player"' in html
    assert 'data-camera-id="cam-1"' in html
    assert 'data-event-id="ev-1"' in html
    assert 'role="button"' in html
    assert 'tabindex="0"' in html


def test_analytics_only_event_thumbnail_stays_plain_and_non_interactive(monkeypatch):
    html = _render([{
        "id": "ev-2", "camera": 1, "camera_id": "cam-1", "camera_name": "Front Door",
        "event_type": "motion", "timestamp": "2026-09-02T00:40:00", "confidence": 0.4,
        "thumbnail": "/api/customer/recordings/cam-1/r2/thumbnail", "has_event_clip": False,
    }], monkeypatch)
    row_html = _table_body(html)
    assert 'class="event-thumb-player"' not in row_html
    assert '<img src="/api/customer/recordings/cam-1/r2/thumbnail"' in row_html


def test_playable_event_without_a_thumbnail_still_gets_the_clickable_wrapper(monkeypatch):
    """No preview image yet is not the same as no clip -- the click
    target (and the em-dash placeholder inside it) must still exist."""
    html = _render([{
        "id": "ev-3", "camera": 1, "camera_id": "cam-1", "camera_name": "Front Door",
        "event_type": "motion", "timestamp": "2026-09-02T00:41:00", "confidence": 0.9,
        "thumbnail": None, "has_event_clip": True,
    }], monkeypatch)
    assert 'class="event-thumb-player"' in html
    assert 'data-event-id="ev-3"' in html


def test_deep_link_playback_action_column_is_unchanged_by_this_feature(monkeypatch):
    """The secondary /playback?event=... path (Action column) must
    keep working exactly as the direct-event-playback fix left it."""
    html = _render([{
        "id": "ev-1", "camera": 1, "camera_id": "cam-1", "camera_name": "Front Door",
        "event_type": "motion", "timestamp": "2026-09-02T00:38:58", "confidence": 0.9,
        "thumbnail": None, "has_event_clip": True,
    }], monkeypatch)
    assert 'href="/playback?camera=cam-1&event=ev-1&autoplay=event"' in html


def test_inline_player_fetches_the_existing_authorized_route_no_second_api(monkeypatch):
    html = _render([{
        "id": "ev-1", "camera": 1, "camera_id": "cam-1", "camera_name": "Front Door",
        "event_type": "motion", "timestamp": "2026-09-02T00:38:58", "confidence": 0.9,
        "thumbnail": None, "has_event_clip": True,
    }], monkeypatch)
    player_source = (Path(__file__).parents[1] / "static" / "event_media.js").read_text()
    fetch_call = "/api/customer/events/${encodeURIComponent(cameraId)}/${encodeURIComponent(eventId)}/media/url`"
    assert fetch_call in player_source
    # No second/new route pattern introduced for this feature -- exactly
    # one actual fetch() call target, no matter how many times the
    # route is mentioned in comments.
    assert player_source.count("fetcher(`/api/customer/events/") == 1
    assert "AnyAiCamEventMedia.player" in html


# --------------------------------------------------------- real JS execution: single-player behavior


def _extract_function(html: str, name: str) -> str:
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed in this environment")
def test_starting_a_second_inline_clip_stops_the_first_one(tmp_path, monkeypatch):
    """Real execution (not just a text assertion) of the actual
    rendered revertToThumbnail()/playEventClipInline() functions,
    against a minimal hand-built DOM/fetch stub, proving: clicking a
    second event card pauses+clears the first card's <video> before
    the second one loads."""
    html = _render([
        {"id": "ev-1", "camera": 1, "camera_id": "cam-1", "camera_name": "Front Door",
         "event_type": "motion", "timestamp": "2026-09-02T00:38:58", "confidence": 0.9,
         "thumbnail": None, "has_event_clip": True},
        {"id": "ev-2", "camera": 1, "camera_id": "cam-1", "camera_name": "Front Door",
         "event_type": "motion", "timestamp": "2026-09-02T00:40:00", "confidence": 0.9,
         "thumbnail": None, "has_event_clip": True},
    ], monkeypatch)

    revert_fn = _extract_function(html, "revertToThumbnail")
    play_fn = _extract_function(html, "playEventClipInline")

    player_source = (Path(__file__).parents[1] / "static" / "event_media.js").read_text()
    script = f"""
let pausedCalls = [];
global.window = globalThis;
{player_source}

// Minimal DOM/fetch stubs -- just enough surface for these two
// functions, not a full browser. innerHTML assignment "materializes"
// a fake <video> child whenever the markup contains one, mirroring
// what a real DOM would give querySelector('video') afterward.
function makeContainer(id) {{
  const state = {{
    dataset: {{}},
    classList: {{ list: new Set(), add(c){{this.list.add(c)}}, remove(c){{this.list.delete(c)}} }},
    id,
    _video: null,
    _status: {{ textContent: '' }},
  }};
  Object.defineProperty(state, 'innerHTML', {{
    get() {{ return state._html || ''; }},
    set(html) {{
      state._html = html;
      state._video = html.includes('<video') ? {{
        paused: false,
        src: '',
        play(){{ return Promise.resolve(); }},
        pause(){{ pausedCalls.push(state.id); this.paused = true; }},
        removeAttribute(){{}},
        _listeners: {{}},
        load(){{ if(this._listeners.canplay) this._listeners.canplay(); }},
        addEventListener(name, callback){{ this._listeners[name] = callback; }},
        removeEventListener(name, callback){{ if(this._listeners[name] === callback) delete this._listeners[name]; }},
      }} : null;
    }},
  }});
  state.querySelector = function(sel) {{
    if(sel === 'video') return state._video;
    if(sel === 'p') return state._status;
    return null;
  }};
  return state;
}}

{revert_fn}
{play_fn}

const containerA = makeContainer('A');
const containerB = makeContainer('B');

// Simulate container A already mid-playback with a live <video>.
containerA.innerHTML = '<video controls playsinline></video>';
currentlyPlaying = containerA;
containerA.classList.add('playing');

// fetch stub used by playEventClipInline -- resolves with a fake url.
global.fetch = function(url) {{
  return Promise.resolve({{ ok: true, json: () => Promise.resolve({{ url: 'https://example.com/clip.mp4' }}) }});
}};

playEventClipInline(containerB);

setTimeout(() => {{
  console.log(JSON.stringify({{
    aPaused: pausedCalls.includes('A'),
    aStillPlayingFlag: containerA.classList.list.has('playing'),
    currentlyPlayingIsB: currentlyPlaying === containerB,
  }}));
}}, 50);
"""
    script_path = tmp_path / "single_player_check.js"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script_path)],
        capture_output=True, text=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr
    import json
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert data["aPaused"] is True
    assert data["aStillPlayingFlag"] is False
    assert data["currentlyPlayingIsB"] is True
