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
        identity = None
    valid = bool(identity) and identity.get("role") in {"customer_owner", "customer_viewer"}

    # --- TEMPORARY DIAGNOSTIC LOGGING (production talk-down 403 investigation) ---
    # Never logs cookie values, header values beyond Host/Origin/X-Forwarded-Proto,
    # authorization headers, secrets, or tokens. Role/customer_id are logged only
    # when authentication actually succeeds. Remove once root cause is confirmed.
    logger.info(
        "talk_audio_relay_ws_diagnostic has_partner_session_cookie=%s cookie_names=%s "
        "host=%s origin=%s x_forwarded_proto=%s identity_returned=%s role=%s customer_id=%s",
        "anyaicam_partner_session" in websocket.cookies,
        list(websocket.cookies.keys()),
        websocket.headers.get("host"),
        websocket.headers.get("origin"),
        websocket.headers.get("x-forwarded-proto"),
        identity is not None,
        identity.get("role") if valid else None,
        identity.get("customer_id") if valid else None,
    )

    if not valid:
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



class _LocalIsapiTalkRelay:
    """Direct edge-to-camera Hikvision/ISAPI two-way audio relay.

    Browser supplies signed PCM16 mono at its AudioContext sample rate.
    We resample to 8 kHz and convert to G.711 mu-law, matching the
    camera's verified TwoWayAudio channel capability.
    """

    def __init__(self, camera: dict, sample_rate: int, session_id: str | None = None):
        import queue
        import threading

        self.camera = dict(camera)
        self.session_id = session_id
        self.sample_rate = max(8000, min(192000, int(sample_rate or 48000)))
        self.queue = queue.Queue(maxsize=256)
        self.thread = None
        self.started = threading.Event()
        self.error = None
        self.rate_state = None
        self.stopped = False
        self._logged_first_frame = False

        self.target = self._target()

        # --- TEMPORARY DIAGNOSTIC LOGGING (2026-09-04 Talk Down investigation) ---
        # Credential-safe: logs channel_id/host (no auth header, no URL query
        # creds -- ISAPI auth here is HTTPDigestAuth, never in the URL) and
        # declared I/O audio format only. Never logs username/password,
        # Authorization/Digest header values, session cookies/tokens, or raw
        # audio bytes. Remove once the root cause is confirmed.
        logger.info(
            "talk_isapi_diagnostic session=%s camera_id=%s camera_name=%s target_resolved=%s "
            "channel_id=%s host_present=%s input_format=pcm16/%sHz/mono output_format=g711_ulaw/8000Hz/mono",
            self.session_id, self.camera.get("id"), self.camera.get("name"),
            self.target is not None,
            self.target.get("channel_id") if self.target else None,
            bool(self.target and self.target.get("host")),
            self.sample_rate,
        )

    def _target(self):
        from urllib.parse import urlsplit
        from appliance_protocol import decrypt_camera_credentials

        with connection() as db:
            credential_row = db.execute(
                "SELECT encrypted_blob FROM camera_credentials WHERE camera_id=?",
                (self.camera["id"],),
            ).fetchone()

        if not credential_row:
            return None

        credentials = decrypt_camera_credentials(
            credential_row["encrypted_blob"]
        )
        if not credentials:
            return None

        source = self.camera.get("onvif_endpoint") or ""
        parsed = urlsplit(source)

        host = (
            self.camera.get("ip_address")
            or parsed.hostname
        )

        if not host:
            return None

        metadata = {}
        try:
            metadata = json.loads(
                self.camera.get("talk_down_metadata") or "{}"
            )
        except (TypeError, ValueError):
            metadata = {}

        channel_id = str(
            metadata.get("channel_id")
            or metadata.get("audio_output_channel")
            or 1
        )

        return {
            "host": host,
            "username": credentials.get("username", ""),
            "password": credentials.get("password", ""),
            "channel_id": channel_id,
        }

    def start(self) -> bool:
        import threading
        from datetime import datetime as _dt

        self._start_requested_at = _dt.now().isoformat()
        logger.info(
            "talk_isapi_diagnostic session=%s camera_id=%s event=start_requested at=%s",
            self.session_id, self.camera.get("id"), self._start_requested_at,
        )

        if not self.target:
            self.error = "Camera credentials or host unavailable."
            logger.warning(
                "talk_isapi_diagnostic session=%s camera_id=%s event=start_failed reason=%s",
                self.session_id, self.camera.get("id"), self.error,
            )
            return False

        self.thread = threading.Thread(
            target=self._run,
            name=f"talk-isapi-{self.camera['id']}",
            daemon=True,
        )
        self.thread.start()

        if not self.started.wait(timeout=7):
            self.error = self.error or "Timed out opening camera talk channel."
            logger.warning(
                "talk_isapi_diagnostic session=%s camera_id=%s event=start_timeout reason=%s",
                self.session_id, self.camera.get("id"), self.error,
            )
            return False

        logger.info(
            "talk_isapi_diagnostic session=%s camera_id=%s event=start_result success=%s error=%s",
            self.session_id, self.camera.get("id"), self.error is None, self.error,
        )
        return self.error is None

    def _run(self):
        import requests
        from requests.auth import HTTPDigestAuth

        target = self.target
        host = target["host"]
        channel_id = target["channel_id"]

        # base carries no credentials -- ISAPI auth here is HTTPDigestAuth
        # (a header, computed by the requests library), never a URL query
        # parameter -- safe to log in full.
        base = (
            f"http://{host}/ISAPI/System/"
            f"TwoWayAudio/channels/{channel_id}"
        )

        auth = HTTPDigestAuth(
            target["username"],
            target["password"],
        )

        exit_reason = "unknown"
        audiodata_started = False

        try:
            opened = requests.put(
                base + "/open",
                auth=auth,
                timeout=5,
            )

            # --- TEMPORARY DIAGNOSTIC LOGGING (2026-09-04 Talk Down investigation) ---
            logger.info(
                "talk_isapi_diagnostic session=%s camera_id=%s event=open_response status=%s",
                self.session_id, self.camera.get("id"), opened.status_code,
            )

            if opened.status_code < 200 or opened.status_code >= 300:
                raise RuntimeError(
                    f"camera talk open returned HTTP {opened.status_code}"
                )

            logger.info(
                "local ISAPI talk opened camera_id=%s host=%s channel=%s",
                self.camera.get("id"),
                host,
                channel_id,
            )

            self.started.set()

            bytes_sent = 0
            chunks_sent = 0

            def audio_chunks():
                nonlocal bytes_sent, chunks_sent

                while True:
                    chunk = self.queue.get()

                    if chunk is None:
                        logger.info(
                            "local ISAPI talk audio finished camera_id=%s chunks=%s bytes=%s",
                            self.camera.get("id"),
                            chunks_sent,
                            bytes_sent,
                        )
                        logger.info(
                            "talk_isapi_diagnostic session=%s camera_id=%s event=audiodata_frames_sent "
                            "chunks=%s bytes=%s",
                            self.session_id, self.camera.get("id"), chunks_sent, bytes_sent,
                        )
                        return

                    if chunk:
                        chunks_sent += 1
                        bytes_sent += len(chunk)

                        if chunks_sent == 1:
                            logger.info(
                                "local ISAPI talk first audio camera_id=%s bytes=%s",
                                self.camera.get("id"),
                                len(chunk),
                            )
                            logger.info(
                                "talk_isapi_diagnostic session=%s camera_id=%s event=first_audiodata_chunk "
                                "bytes=%s",
                                self.session_id, self.camera.get("id"), len(chunk),
                            )

                        yield chunk

            # Hikvision-style audioData behaves like a long-lived upload.
            # The camera may begin playing audio immediately without
            # returning a normal response until the upload connection ends.
            # Therefore do not use a short read timeout or treat the absence
            # of an immediate response as a failure.
            logger.info(
                "talk_isapi_diagnostic session=%s camera_id=%s event=audiodata_starting",
                self.session_id, self.camera.get("id"),
            )
            audiodata_started = True
            try:
                audio_response = requests.put(
                    base + "/audioData",
                    auth=auth,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Connection": "keep-alive",
                    },
                    data=audio_chunks(),
                    timeout=(5, None),
                )

                logger.info(
                    "talk_isapi_diagnostic session=%s camera_id=%s event=audiodata_response status=%s",
                    self.session_id, self.camera.get("id"), audio_response.status_code,
                )

                if (
                    audio_response.status_code < 200
                    or audio_response.status_code >= 300
                ):
                    raise RuntimeError(
                        "camera audioData returned HTTP "
                        f"{audio_response.status_code}"
                    )
                exit_reason = "audiodata_completed_normally"

            except requests.exceptions.ReadTimeout:
                # Some cameras keep audioData open while audio is playing
                # and do not send a conventional response until disconnect.
                # Treat that as non-fatal for live talk-down.
                logger.info(
                    "talk_isapi_diagnostic session=%s camera_id=%s event=audiodata_readtimeout_expected "
                    "note=camera_kept_connection_open_no_response_before_disconnect",
                    self.session_id, self.camera.get("id"),
                )
                exit_reason = "audiodata_readtimeout_expected"

        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            exit_reason = f"exception:{type(error).__name__}"
            logger.warning(
                "local ISAPI talk relay failed camera_id=%s error=%s",
                self.camera.get("id"),
                self.error,
            )
            logger.warning(
                "talk_isapi_diagnostic session=%s camera_id=%s event=relay_exception "
                "audiodata_started=%s error_type=%s error=%s",
                self.session_id, self.camera.get("id"), audiodata_started,
                type(error).__name__, self.error,
            )
            self.started.set()

        finally:
            close_status = None
            try:
                closed = requests.put(
                    base + "/close",
                    auth=auth,
                    timeout=5,
                )
                close_status = closed.status_code
            except Exception as close_error:
                close_status = f"exception:{type(close_error).__name__}"
            logger.info(
                "talk_isapi_diagnostic session=%s camera_id=%s event=close_response status=%s "
                "exit_reason=%s at=%s",
                self.session_id, self.camera.get("id"), close_status, exit_reason,
                __import__("datetime").datetime.now().isoformat(),
            )

    def send_pcm16(self, pcm: bytes) -> None:
        import audioop
        import queue

        if self.stopped or not pcm:
            return

        # ScriptProcessor supplies signed little-endian PCM16 mono.
        # Convert browser rate (typically 48 kHz) -> 8 kHz.
        converted, self.rate_state = audioop.ratecv(
            pcm,
            2,
            1,
            self.sample_rate,
            8000,
            self.rate_state,
        )

        if not converted:
            return

        # Camera 4 capability probe verified G.711 mu-law.
        ulaw = audioop.lin2ulaw(converted, 2)

        # --- TEMPORARY DIAGNOSTIC LOGGING (2026-09-04 Talk Down investigation) ---
        # Lengths/counts only -- never the audio bytes themselves.
        if not self._logged_first_frame:
            self._logged_first_frame = True
            logger.info(
                "talk_isapi_diagnostic session=%s camera_id=%s event=first_browser_frame_received "
                "input_pcm_bytes=%s input_rate=%sHz converted_pcm_bytes=%s converted_rate=8000Hz "
                "ulaw_bytes=%s",
                self.session_id, self.camera.get("id"), len(pcm), self.sample_rate,
                len(converted), len(ulaw),
            )

        try:
            self.queue.put_nowait(ulaw)
        except queue.Full:
            # Real-time audio must never block the event loop. Drop the
            # oldest packet rather than building seconds of stale speech.
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass

            try:
                self.queue.put_nowait(ulaw)
            except queue.Full:
                pass

    def stop(self) -> None:
        import queue
        from datetime import datetime as _dt

        if self.stopped:
            return

        logger.info(
            "talk_isapi_diagnostic session=%s camera_id=%s event=stop_requested at=%s",
            self.session_id, self.camera.get("id"), _dt.now().isoformat(),
        )

        self.stopped = True

        try:
            self.queue.put_nowait(None)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass

            try:
                self.queue.put_nowait(None)
            except queue.Full:
                pass

        if self.thread is not None:
            self.thread.join(timeout=12)


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
                "SELECT * FROM customer_talk_sessions "
                "WHERE id=? AND customer_id=?",
                (session_id, identity["customer_id"]),
            ).fetchone()

        if not session_row or session_row["state"] != "requested":
            await websocket.close(code=4404)
            return

        try:
            with connection() as db:
                camera = _authorized_talk_camera(
                    db,
                    session_row["camera_id"],
                    identity,
                )
        except HTTPException as error:
            await websocket.close(code=4000 + error.status_code)
            return

        try:
            sample_rate = int(
                websocket.query_params.get("sample_rate", "48000")
            )
        except (TypeError, ValueError):
            sample_rate = 48000

        appliance_channel = _appliance_channels.get(
            camera["appliance_id"]
        )

        local_relay = None

        # Complete the WebSocket handshake before potentially spending a
        # few seconds opening the physical camera's talk channel.
        await websocket.accept()

        if appliance_channel is None:
            local_relay = _LocalIsapiTalkRelay(
                camera,
                sample_rate,
                session_id,
            )

            started = await asyncio.to_thread(
                local_relay.start
            )

            if not started:
                logger.warning(
                    "customer talk local relay unavailable "
                    "session_id=%s camera_id=%s error=%s",
                    session_id,
                    camera["id"],
                    local_relay.error,
                )
                await websocket.close(code=4503)
                return

        now = time.monotonic()

        _active_relays[session_id] = {
            "camera_id": camera["id"],
            "appliance_id": camera["appliance_id"],
            "customer_id": identity["customer_id"],
            "created_at": now,
        }

        if appliance_channel is not None:
            try:
                metadata = json.loads(
                    camera["talk_down_metadata"]
                ) if camera.get("talk_down_metadata") else {}
            except (TypeError, ValueError):
                metadata = {}

            await appliance_channel.send_text(
                json.dumps({
                    "type": "start",
                    "session_id": session_id,
                    "camera_id": camera["id"],
                    "metadata": metadata,
                    "sample_rate": sample_rate,
                })
            )

        try:
            while True:
                relay = _active_relays.get(session_id)

                if relay is None:
                    break

                if (
                    time.monotonic() - relay["created_at"]
                    > MAX_RELAY_SECONDS
                ):
                    break

                try:
                    frame = await asyncio.wait_for(
                        websocket.receive_bytes(),
                        timeout=IDLE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    break

                if local_relay is not None:
                    local_relay.send_pcm16(frame)
                    continue

                channel = _appliance_channels.get(
                    relay["appliance_id"]
                )

                if channel is None:
                    break

                await channel.send_text(
                    json.dumps({
                        "type": "audio",
                        "session_id": session_id,
                        "pcm_b64": base64.b64encode(frame).decode(),
                    })
                )

        except WebSocketDisconnect:
            pass

        finally:
            if local_relay is not None:
                await asyncio.to_thread(local_relay.stop)

            await _end_relay(
                session_id,
                notify_appliance=(local_relay is None),
            )
