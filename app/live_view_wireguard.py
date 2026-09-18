"""WireGuard-carried live view -- Phase C, controlled single-camera proof.

Adds a fourth transport-race candidate alongside LAN, WebRTC P2P
(live_view_p2p.py), and the AWS relay (live_relay_uploader.py/
live_playlist.py): fetch the appliance's OWN local HLS output
(local_live_hls.py -- the exact same bytes a LAN viewer already gets)
through the cloud-side WireGuard gateway's internal proxy
(wireguard_gateway/proxy_server.py) instead of via S3/CloudFront.

## Security boundary (read this before changing anything below)

The browser NEVER learns a WireGuard tunnel address, a gateway hostname,
or a port. Every URL handed to the browser is portal-relative
(/api/customer/live/sessions/{id}/wireguard/...), same-origin, and
requires the SAME cookie session every other customer route already
requires. The only two places a tunnel address ever appears are (a) the
`appliance_wireguard_peers` database row and (b) the query string of the
SERVER-SIDE call this module makes to the gateway's internal proxy
(proxy_server.py) over the docker-internal network -- never in any
response body this module returns to the browser.

Authorization is re-checked here on EVERY request, exactly like every
other live-view module in this family (see live_view_p2p.py's own module
docstring: "Authorization logic ... is deliberately duplicated ... rather
than imported"). _customer_identity()/_own_session() below are that same
duplication, not an import -- proxy_server.py/proxy.py have no
authorization concept of their own and must never be treated as one:
_own_session() below is what stands between a mis-scoped tunnel_address
lookup and a cross-tenant read.

The remote m3u8 text is NEVER passed through to the browser unmodified --
mirrors local_live_hls.py's own established discipline for exactly the
same reason (a manifest is attacker-adjacent input, arriving over a
network from a device this project does not control end-to-end): every
line is validated against a small allow-list of playlist tags and
segment-name shapes; anything else (an injected external URL, a key/map
tag, a discontinuity tag naming an arbitrary URI) is silently dropped
rather than forwarded. Segment filenames are re-validated against the
same `camera{N}_{digits}.ts` shape local_live_hls.py's own
segment_path() already enforces before ever reaching the gateway.

## Fail-safe behavior

This route is only ever one more bounded attempt in the browser's
existing transport race (live_view_page.py's claimTransport()); the
always-on relay command is queued by live_view_sessions.start_live_view()
completely independently of this module's existence. A tunnel outage, a
revoked/never-enrolled peer, a disabled flag, an appliance not on the
single-camera allow-list, or the gateway's internal proxy being
unreachable all reduce to the exact same case from the browser's own
perspective: "this attempt produced nothing," identical in effect to
today's P2P timeout already falling back to relay. No new fallback logic
was written for this -- the existing race's own shape already generalizes.

## Rollout controls

Off everywhere by default (ANYAICAM_WIREGUARD_LIVE_ENABLED=false). Even
once enabled, restricted to an explicit camera-id allow-list
(ANYAICAM_WIREGUARD_LIVE_CAMERA_IDS, comma-separated, empty by default --
an empty allow-list with the flag on still allows nothing, fail-closed)
for the controlled single-camera proof this task's own directive asked
for; expanding to more cameras later is exactly one env value away, no
code change.

## Event-clip support (prepared, not yet wired to any UI)

register_event_media_wireguard_routes() below exists so a LATER pass can
measure Hybrid AWS-transfer avoidance by fetching an event clip directly
from the appliance instead of downloading it from S3 -- reusing this same
authenticated-proxy mechanism. It depends on `detection_event_media.
local_relative_path` (see db_migrations.py's 20260918_event_media_local_path
and event_media_uploader.py's own now-additive payload field), which is
only ever populated going forward, from the next upload the appliance
performs, and stays NULL on every already-uploaded clip -- so this route
correctly, deliberately 404s for every clip that predates this change,
never assumes a path that was never recorded.
"""

from __future__ import annotations

import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from fastapi import FastAPI, HTTPException, Request, Response

from partner_db import connection
from partner_portal import partner_identity
from wireguard_remote import active_peers_for_appliance

log = logging.getLogger("anyaicam.live_view_wireguard")

LIVE_WIREGUARD_ENABLED = os.environ.get("ANYAICAM_WIREGUARD_LIVE_ENABLED", "false").strip().lower() == "true"
_ALLOWED_LIVE_CAMERA_IDS = {
    value.strip() for value in os.environ.get("ANYAICAM_WIREGUARD_LIVE_CAMERA_IDS", "").split(",") if value.strip()
}
EVENT_MEDIA_WIREGUARD_ENABLED = os.environ.get(
    "ANYAICAM_WIREGUARD_EVENT_MEDIA_ENABLED", "false"
).strip().lower() == "true"

# The gateway's internal proxy is reachable only on the shared
# deploy_default docker network (see proxy_server.py) -- unset means "no
# gateway configured for this environment," and every route below fails
# closed (503) rather than guessing a URL.
GATEWAY_INTERNAL_URL = os.environ.get("ANYAICAM_WIREGUARD_GATEWAY_INTERNAL_URL", "").rstrip("/")
GATEWAY_APPLIANCE_PORT = int(os.environ.get("ANYAICAM_WIREGUARD_GATEWAY_APPLIANCE_PORT", "8000"))
GATEWAY_FETCH_TIMEOUT_SECONDS = float(os.environ.get("ANYAICAM_WIREGUARD_GATEWAY_FETCH_TIMEOUT_SECONDS", "8"))

_SEGMENT_NAME_RE = re.compile(r"camera(\d+)_[0-9]+\.ts")
_PLAYLIST_TAG_RE = re.compile(r"#EXT-X-(?:VERSION|TARGETDURATION|MEDIA-SEQUENCE|INDEPENDENT-SEGMENTS):.*")
_EXTINF_RE = re.compile(r"#EXTINF:[0-9]+(?:\.[0-9]+)?,")


class GatewayUnavailable(Exception):
    pass


def _customer_identity(request: Request) -> dict:
    identity = partner_identity(request)
    if not identity or identity.get('role') not in {'customer_owner', 'customer_viewer'}:
        raise HTTPException(status_code=403, detail='Customer account required.')
    return identity


def _own_session(db, session_id: str, identity: dict) -> dict:
    session = db.execute(
        'SELECT * FROM live_view_sessions WHERE id=? AND customer_id=?',
        (session_id, identity['customer_id']),
    ).fetchone()
    if not session:
        raise HTTPException(status_code=404, detail='Live view session not found.')
    return dict(session)


def _live_camera_allowed(camera_id: str) -> bool:
    return LIVE_WIREGUARD_ENABLED and camera_id in _ALLOWED_LIVE_CAMERA_IDS


def _tunnel_address_for_appliance(db, appliance_id: str) -> str | None:
    peers = active_peers_for_appliance(db, appliance_id)
    return peers[0]['tunnel_address'] if peers else None


def _fetch_via_gateway(tunnel_address: str, port: int, path: str) -> bytes:
    """Server-side only: the tunnel_address/port/gateway URL below never
    leaves this process -- callers get back bytes, never this function's
    own arguments. Raises GatewayUnavailable for anything the browser
    should see as "this transport didn't work" (never a 5xx that would
    read as a portal outage)."""
    if not GATEWAY_INTERNAL_URL:
        raise GatewayUnavailable('WireGuard gateway is not configured for this environment.')
    query = urllib.parse.urlencode({'tunnel': tunnel_address, 'port': port, 'path': path})
    try:
        with urllib.request.urlopen(
            f'{GATEWAY_INTERNAL_URL}/appliance-fetch?{query}', timeout=GATEWAY_FETCH_TIMEOUT_SECONDS
        ) as response:
            if response.status != 200:
                raise GatewayUnavailable(f'Gateway returned HTTP {response.status}.')
            return response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise GatewayUnavailable(f'Could not reach the WireGuard gateway: {error}') from error


def _sanitize_playlist(raw_text: str, camera_number: int, rewrite_prefix: str) -> str:
    """Mirrors local_live_hls.py's local_playlist() discipline exactly:
    an appliance-sourced manifest is untrusted input from a network this
    project does not fully control end-to-end, so every line is checked
    against a small allow-list before being forwarded -- anything else
    (an injected external URL, a key/map tag, a URI-bearing discontinuity
    tag) is silently dropped, never passed through."""
    output = ['#EXTM3U']
    pending = None
    for line in raw_text.splitlines():
        line = line.strip()
        if _PLAYLIST_TAG_RE.fullmatch(line):
            output.append(line)
        elif _EXTINF_RE.fullmatch(line):
            pending = line
        elif line and not line.startswith('#'):
            match = _SEGMENT_NAME_RE.fullmatch(line)
            if not match or int(match.group(1)) != camera_number:
                pending = None
                continue
            if pending:
                output.extend([pending, f'{rewrite_prefix}/{urllib.parse.quote(line, safe="")}'])
            pending = None
    return '\n'.join(output) + '\n'


def register_live_view_wireguard_routes(app: FastAPI) -> None:
    @app.get('/api/customer/live/wireguard/config')
    def wireguard_config(request: Request) -> dict:
        _customer_identity(request)
        return {'enabled': LIVE_WIREGUARD_ENABLED}

    @app.get('/api/customer/live/sessions/{session_id}/wireguard/{filename}')
    def wireguard_fetch(request: Request, session_id: str, filename: str):
        identity = _customer_identity(request)
        with connection() as db:
            session = _own_session(db, session_id, identity)
            if not _live_camera_allowed(session['camera_id']):
                raise HTTPException(status_code=404, detail='WireGuard live view is not enabled for this camera.')
            camera = db.execute(
                'SELECT appliance_id, camera_number FROM cameras WHERE id=?', (session['camera_id'],)
            ).fetchone()
            if not camera or not camera['appliance_id'] or camera['camera_number'] is None:
                raise HTTPException(status_code=404, detail='Camera is not eligible for WireGuard live view.')
            tunnel_address = _tunnel_address_for_appliance(db, camera['appliance_id'])
        if not tunnel_address:
            raise HTTPException(status_code=503, detail='No active WireGuard tunnel for this appliance.')

        camera_number = camera['camera_number']
        rewrite_prefix = f'/api/customer/live/sessions/{urllib.parse.quote(session_id, safe="")}/wireguard'

        if filename == 'playlist.m3u8':
            try:
                raw = _fetch_via_gateway(tunnel_address, GATEWAY_APPLIANCE_PORT, f'/static/hls/camera{camera_number}.m3u8')
            except GatewayUnavailable:
                raise HTTPException(status_code=503, detail='Could not reach the appliance over WireGuard.')
            body = _sanitize_playlist(raw.decode('utf-8', errors='replace'), camera_number, rewrite_prefix)
            return Response(content=body, media_type='application/vnd.apple.mpegurl')

        match = _SEGMENT_NAME_RE.fullmatch(filename)
        if not match or int(match.group(1)) != camera_number:
            raise HTTPException(status_code=404, detail='Segment not found.')
        try:
            body = _fetch_via_gateway(tunnel_address, GATEWAY_APPLIANCE_PORT, f'/static/hls/{filename}')
        except GatewayUnavailable:
            raise HTTPException(status_code=503, detail='Could not reach the appliance over WireGuard.')
        return Response(content=body, media_type='video/mp2t')


def register_event_media_wireguard_routes(app: FastAPI) -> None:
    """Prepared for a later Hybrid-cost measurement pass -- not wired
    into any customer-facing UI yet. See module docstring's Event-clip
    support section for why this reliably 404s on every clip uploaded
    before local_relative_path existed, by design, not as a bug."""

    @app.get('/api/customer/event-media/{media_id}/wireguard')
    def event_media_fetch(request: Request, media_id: str):
        identity = _customer_identity(request)
        if not EVENT_MEDIA_WIREGUARD_ENABLED:
            raise HTTPException(status_code=404, detail='WireGuard event-clip fetch is not enabled.')
        with connection() as db:
            media = db.execute(
                'SELECT customer_id, camera_id, local_relative_path FROM detection_event_media WHERE id=? AND customer_id=?',
                (media_id, identity['customer_id']),
            ).fetchone()
            if not media:
                raise HTTPException(status_code=404, detail='Event clip not found.')
            local_relative_path = media['local_relative_path']
            if not local_relative_path or not local_relative_path.startswith('/recordings/') or '..' in local_relative_path:
                raise HTTPException(status_code=404, detail='Direct appliance fetch is not available for this clip.')
            camera = db.execute('SELECT appliance_id FROM cameras WHERE id=?', (media['camera_id'],)).fetchone()
            if not camera or not camera['appliance_id']:
                raise HTTPException(status_code=404, detail='Direct appliance fetch is not available for this clip.')
            tunnel_address = _tunnel_address_for_appliance(db, camera['appliance_id'])
        if not tunnel_address:
            raise HTTPException(status_code=503, detail='No active WireGuard tunnel for this appliance.')
        try:
            body = _fetch_via_gateway(tunnel_address, GATEWAY_APPLIANCE_PORT, local_relative_path)
        except GatewayUnavailable:
            raise HTTPException(status_code=503, detail='Could not reach the appliance over WireGuard.')
        return Response(content=body, media_type='video/mp4')
