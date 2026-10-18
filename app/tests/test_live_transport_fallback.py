"""1.2.7 P2P/WireGuard audit (owner: mandatory acceptance, 2026-10-17), defect 1: the WireGuard -> AWS relay fallback was
dead. After a fatal hls.js error on a WireGuard player, handleFatalError() tore the player down and handed off to
pollPlaylist(), whose relay attachPlayer() asked claimTransport('relay') -- still claimed by 'wireguard', so 'blocked',
and the camera stayed on "Reconnecting…" until the page was reloaded.

These tests run the REAL rendered claimTransport()/handleFatalError() of both live pages in Node, with only the DOM/hls.js
side effects stubbed; pollPlaylist() is replaced by the claim the real relay attachPlayer() makes first."""
import json
import re
import shutil
import subprocess

import pytest

from test_live_view_hls_fatal_error_recovery import (  # noqa: F401  (fixtures)
    _grid_html, _single_html, client, db_path,
)

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="needs node to execute the page script")


def _function(html: str, name: str, *, start_text: str | None = None) -> str:
    start = html.index(start_text or f"function {name}(")
    depth = 0
    for index in range(html.index("{", start), len(html)):
        depth += {"{": 1, "}": -1}.get(html[index], 0)
        if depth == 0:
            return html[start:index + 1]
    raise AssertionError(f"unterminated {name}")


def _run(script: str) -> dict:
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


STUBS = """
const Hls={ErrorTypes:{NETWORK_ERROR:'networkError',MEDIA_ERROR:'mediaError',OTHER_ERROR:'otherError'}};
const MAX_INPLACE_RECOVERY_ATTEMPTS=2, pollTimeoutMs=1000;
const placeholder={hidden:true};
function setStatus(){}
function destroyHls(){}
function stopPolling(){}
"""


def _single(initial: str) -> dict:
    return f"""{STUBS}
let transport={json.dumps(initial)}, stopped=false, recoveryAttempts=MAX_INPLACE_RECOVERY_ATTEMPTS;
const hls={{startLoad(){{}},recoverMediaError(){{}}}};
let relayClaim=null;
function pollPlaylist(){{relayClaim=claimTransport('relay')}}
"""


@pytest.mark.parametrize("failed", ["wireguard", "relay"])
def test_single_camera_a_dead_hls_transport_falls_back_to_the_relay(client, failed):
    html = _single_html(client)
    script = _single(failed) + _function(html, "claimTransport") + _function(html, "handleFatalError") + """
handleFatalError({type:Hls.ErrorTypes.OTHER_ERROR,fatal:true});
console.log(JSON.stringify({relayClaim,transport}));"""
    outcome = _run(script)
    assert outcome == {"relayClaim": "claimed", "transport": "relay"}  # re-reported as relay: correct identification


def test_single_camera_p2p_keeps_its_claim(client):
    """A late hls error after a P2P upgrade must not hand the camera back to the relay."""
    html = _single_html(client)
    script = _single("p2p") + _function(html, "claimTransport") + _function(html, "handleFatalError") + """
handleFatalError({type:Hls.ErrorTypes.OTHER_ERROR,fatal:true});
console.log(JSON.stringify({relayClaim,transport}));"""
    assert _run(script) == {"relayClaim": "blocked", "transport": "p2p"}


@pytest.mark.parametrize("failed", ["wireguard", "relay"])
def test_grid_a_dead_hls_transport_falls_back_to_the_relay_for_that_tile_only(client, failed):
    html = _grid_html(client)
    script = STUBS + f"""
const tiles={{a:{{transport:{json.dumps(failed)},stopped:false,recoveryAttempts:MAX_INPLACE_RECOVERY_ATTEMPTS,placeholder:{{hidden:true}},hls:{{}}}},
             b:{{transport:'wireguard',stopped:false,placeholder:{{hidden:true}}}}}};
let relayClaim=null;
function pollPlaylist(id){{relayClaim=claimTransport(id,'relay')}}
""" + _function(html, "claimTransport") + _function(html, "handleFatalError") + """
handleFatalError('a',{type:Hls.ErrorTypes.OTHER_ERROR,fatal:true});
console.log(JSON.stringify({relayClaim,a:tiles.a.transport,b:tiles.b.transport}));"""
    assert _run(script) == {"relayClaim": "claimed", "a": "relay", "b": "wireguard"}


def test_the_relay_attach_still_stops_when_blocked(client):
    """The fix releases the claim; it does not weaken claimTransport() -- a loser is still blocked."""
    for html in (_single_html(client), _grid_html(client)):
        assert re.search(r"if\(claim==='blocked'\)\{stopPolling\((id)?\);return\}", html)


# ------------------------------------------------------- defect 4: the transport is reported when video plays


ON_PLAY = """
const listeners=[];
const video={addEventListener(e,f){listeners.push(f)},removeEventListener(e,f){const i=listeners.indexOf(f);if(i>=0)listeners.splice(i,1)}};
const window={};
"""


def _on_play_helper(html):
    return _function(html, "", start_text="window.reportLiveTransportOnPlay = function") + ";"


def test_an_attached_transport_is_reported_only_once_its_first_frame_plays(client):
    script = ON_PLAY + _on_play_helper(_grid_html(client)) + """
const reports=[];
window.reportLiveTransportOnPlay(video,()=>true,()=>reports.push('relay'));
const beforePlay=reports.length;
listeners.slice().forEach(f=>f()); listeners.slice().forEach(f=>f());
console.log(JSON.stringify({beforePlay,reports,left:listeners.length}));"""
    assert _run(script) == {"beforePlay": 0, "reports": ["relay"], "left": 0}


def test_a_superseded_attach_never_reports(client):
    script = ON_PLAY + _on_play_helper(_single_html(client)) + """
const reports=[]; let seq=1;
window.reportLiveTransportOnPlay(video,()=>seq===1,()=>reports.push('wireguard'));
seq=2;  // a P2P upgrade or a later attach happened before the first frame
listeners.slice().forEach(f=>f());
console.log(JSON.stringify({reports,left:listeners.length}));"""
    assert _run(script) == {"reports": [], "left": 0}


def test_both_pages_report_on_play_and_record_failures(client):
    for html in (_single_html(client), _grid_html(client)):
        assert "reportLiveTransportOnPlay(" in html
        assert "'failed',null,'no transport delivered video'" in html
        # P2P supersedes any pending HLS first-frame report
        assert "attachSeq++;" in html or "tile.attachSeq=(tile.attachSeq||0)+1;" in html


def test_single_camera_unavailable_without_any_transport_is_reported_as_failed(client):
    html = _single_html(client)
    script = f"""
const reports=[]; let transport=null, sessionId='s1';
function stopPolling(){{}} function setStatus(){{}} const retryButton={{hidden:true}};
function reportLiveTransportOutcome(...a){{reports.push(a)}}
""" + _function(html, "showUnavailable") + """
showUnavailable(); transport='relay'; showUnavailable();
console.log(JSON.stringify(reports));"""
    assert _run(script) == [["s1", "failed", None, "no transport delivered video"]]
