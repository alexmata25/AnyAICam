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

from tests.test_live_view_hls_fatal_error_recovery import (  # noqa: F401  (fixtures)
    _grid_html, _single_html, client, db_path,
)

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="needs node to execute the page script")


def _function(html: str, name: str) -> str:
    start = html.index(f"function {name}(")
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
