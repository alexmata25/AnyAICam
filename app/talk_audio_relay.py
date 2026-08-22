"""Push-to-talk audio transport -- cloud relay half.

Architecture: browser --(WebSocket, PCM audio frames)--> cloud
--(WebSocket, JSON-enveloped PCM)--> appliance --(ffmpeg transcode +
ONVIF backchannel)--> camera speaker.

Deliberately a SINGLE persistent WebSocket per appliance
(/api/appliance/talk/channel), not a new connection per talk session:
the appliance opens this once (reconnecting on drop) and authenticates
with its existing Bearer/nonce credential scheme, matching the
"appliance only ever connects outbound, never accepts inbound" security
posture this whole project has followed since the recording/live-relay
work. A customer's per-session WebSocket
(/api/customer/talk/sessions/{session_id}/audio) is authorized from
scratch on connect -- re-checking customer/camera
ownership, can_talk, and talk_down_supported==1 exactly as
talk_sessions.py's REST start route does, imported directly rather
than duplicated (unlike this project's usual small-helper-duplication
convention -- a deliberate exception here, since this authorization
logic is too security-critical to risk two copies drifting apart) --
then, once authorized, is bound to the target appliance's already-open
control channel and every audio frame received is JSON-enveloped with
the session_id and forwarded there. The cloud never trusts a
customer/site/camera/appliance identifier the browser merely claims:
customer_id comes only from the signed session cookie, and camera ->
appliance ownership comes only from a fresh database lookup on every
single connection.

Per-session state (_active_relays) is purely in-memory, matching this
project's existing precedent for ephemeral relay state (e.g.
live_relay_uploader.py's own in-memory _uploaded_segments); the
authoritative durable record of a session's existence and outcome is
still customer_talk_sessions (talk_sessions.py's own table), which
this module updates to 'stopped' the moment a relay ends for any
reason -- explicit stop, idle timeout, max-duration timeout, appliance
disconnect, or an authorization failure discovered after the fact.

No audio is ever transcoded or sent to a camera by this module -- that
happens entirely on the appliance side (talk_audio_relay.py there).
This module only ever moves opaque bytes between two already-
authenticated WebSocket connections.
"""

import asyncio
import base64
import json
import logging
import time
from datetime import datetime

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from appliance_cloud import authenticate_appliance
from partner_db import connection
from partner_portal import partner_identity
from talk_sessions import TALK_SESSION_DURATION_SECONDS, _authorized_talk_camera

logger = logging.getLogger("anyaicam.talk_audio_relay")

# No audio frame received from the customer for this long -> treat the
# connection as stalled/dead and close it. This is what makes "browser
# disconnect terminates session" and "timeout terminates session" true
# even when the underlying TCP connection never sends a clean close
# frame -- the read side always wakes up at least this often to
# re-check both the idle condition and the max-duration ceiling below.
IDLE_TIMEOUT_SECONDS = 10

# Reuses talk_sessions.py's own session-duration ceiling (currently
# 60s) as the hard cap on a single relay's lifetime -- one source of
# truth for "how long can a single press-and-hold possibly run",
# rather than a second, potentially-drifting constant here.
MAX_RELAY_SECONDS = TALK_SESSION_DURATION_SECONDS

_appliance_channels: dict[str, WebSocket] = {}  # appliance_id -> its single open control WebSocket
_active_relays: dict[str, dict] = {}  # session_id -> {"camera_id","appliance_id","customer_id","created_at"}


def _customer_identity_ws(websocket: WebSocket) -> dict | None:
    """WebSocket-safe version of talk_sessions.py's _customer_identity():
    returns None instead of raising, since there is no HTTP response to
    attach an HTTPException to once a WebSocket handshake has begun --
    the caller closes the socket with an explicit code instead."""
    try:
        identity = partner_identity(websocket)
    except Exception:
        return None
    if not identity or identity.get("role") not in {"customer_owner", "customer_viewer"}:
        return None
    return identity


async def _end_relay(session_id: str, notify_appliance: bool) -> None:
    """The single cleanup path for every way a relay can end -- explicit
    stop, idle timeout, max-duration timeout, appliance disconnect, or
    the customer socket simply dropping. Always removes the session
    from _active_relays (so a later frame for this session_id can never
    be forwarded again -- this is what makes "no audio sent after
    stop" true) and always marks the durable customer_talk_sessions row
    'stopped' if it was still 'requested'. Idempotent: calling this
    twice for the same session_id is a harmless no-op the second time."""
    relay = _active_relays.pop(session_id, None)
    if relay is None:
        return
    if notify_appliance:
        channel = _appliance_channels.get(relay["appliance_id"])
        if channel is not None:
            try:
                await channel.send_text(json.dumps({"type": "stop", "session_id": session_id}))
            except Exception:
                pass
    now = datetime.now().isoformat()
    with connection() as db:
        db.execute(
            "UPDATE customer_talk_sessions SET state='stopped',ended_at=? WHERE id=? AND state='requested'",
            (now, session_id),
        )


def register_talk_audio_relay_routes(app: FastAPI) -> None:
    @app.websocket("/api/appliance/talk/channel")
    async def appliance_talk_channel(websocket: WebSocket):
        try:
            appliance = authenticate_appliance(websocket)
        except HTTPException:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        _appliance_channels[appliance["id"]] = websocket
        try:
            while True:
                await websocket.receive_text()  # reserved for future ack/error signalling; currently just keeps the loop alive until disconnect
        except WebSocketDisconnect:
            pass
        finally:
            if _appliance_channels.get(appliance["id"]) is websocket:
                del _appliance_channels[appliance["id"]]
            # Every relay this appliance was mid-flight on just became
            # unreachable -- end them now rather than leaving each
            # customer-side connection waiting out its own idle timeout
            # for no reason.
            for session_id, relay in list(_active_relays.items()):
                if relay["appliance_id"] == appliance["id"]:
                    await _end_relay(session_id, notify_appliance=False)

    @app.websocket("/api/customer/talk/sessions/{session_id}/audio")
    async def customer_talk_audio(websocket: WebSocket, session_id: str):
        identity = _customer_identity_ws(websocket)
        if not identity:
            await websocket.close(code=4403)
            return

        with connection() as db:
            session_row = db.execute(
                "SELECT * FROM customer_talk_sessions WHERE id=? AND customer_id=?",
                (session_id, identity["customer_id"]),
            ).fetchone()
        if not session_row or session_row["state"] != "requested":
            await websocket.close(code=4404)
            return

        # Re-derive authorization from scratch -- ownership, can_talk,
        # and talk_down_supported==1 -- exactly the same check the REST
        # /start route already made, never trusted to still hold just
        # because it held a moment ago at /start time.
        try:
            with connection() as db:
                camera = _authorized_talk_camera(db, session_row["camera_id"], identity)
        except HTTPException as error:
            await websocket.close(code=4000 + error.status_code)
            return

        appliance_channel = _appliance_channels.get(camera["appliance_id"])
        if appliance_channel is None:
            await websocket.close(code=4503)  # this camera's appliance has no live control channel right now
            return

        await websocket.accept()
        now = time.monotonic()
        _active_relays[session_id] = {
            "camera_id": camera["id"], "appliance_id": camera["appliance_id"], "customer_id": identity["customer_id"],
            "created_at": now,
        }
        try:
            metadata = json.loads(camera["talk_down_metadata"]) if camera.get("talk_down_metadata") else {}
        except (TypeError, ValueError):
            metadata = {}
        # The browser's own AudioContext sample rate -- read from the
        # connect URL's query string, since it varies by browser/device
        # and the appliance-side transcode step needs the real value
        # rather than a guess. Falls back to a conservative common
        # default (48000, the modern browser norm) if absent/malformed
        # -- never crashes the relay over a missing/bad query param.
        try:
            sample_rate = int(websocket.query_params.get("sample_rate", "48000"))
        except (TypeError, ValueError):
            sample_rate = 48000
        await appliance_channel.send_text(json.dumps({
            "type": "start", "session_id": session_id, "camera_id": camera["id"],
            "metadata": metadata, "sample_rate": sample_rate,
        }))

        try:
            while True:
                relay = _active_relays.get(session_id)
                if relay is None:
                    break  # ended from elsewhere (appliance disconnect, a concurrent cleanup) -- stop forwarding immediately
                if time.monotonic() - relay["created_at"] > MAX_RELAY_SECONDS:
                    break
                try:
                    frame = await asyncio.wait_for(websocket.receive_bytes(), timeout=IDLE_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    break  # idle timeout
                channel = _appliance_channels.get(relay["appliance_id"])
                if channel is None:
                    break
                await channel.send_text(json.dumps({
                    "type": "audio", "session_id": session_id, "pcm_b64": base64.b64encode(frame).decode(),
                }))
        except WebSocketDisconnect:
            pass
        finally:
            await _end_relay(session_id, notify_appliance=True)
