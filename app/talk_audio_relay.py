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
from partner_db import audit, connection
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

# How long the cloud waits for the appliance to confirm a session reached
# the camera before telling the browser it is live anyway (older appliance
# builds never acknowledge).
APPLIANCE_ACK_WAIT_SECONDS = 4

# Talkdown completion pass (2026-09-26): a camera that rejects its login
# (HTTP/RTSP 401 or 403) must not be retried on every press -- many
# cameras lock the account out after repeated failures, and each press
# was another digest attempt. Talk to that camera is paused for this long
# after an auth failure, failing fast without contacting it.
CAMERA_AUTH_COOLDOWN_SECONDS = 300
_camera_auth_failures: dict[str, float] = {}  # camera_id -> monotonic time of the last auth rejection

# User-facing wording for why a talk session could not start or ended.
TALK_ERROR_MESSAGES = {
    "camera_auth_failed": "The camera rejected its login. Check the camera's username and password; talk is paused for a few minutes to avoid locking the camera.",
    "camera_auth_cooldown": "Talk is paused for this camera after a recent login failure. Try again in a few minutes.",
    "camera_unreachable": "The camera's speaker could not be reached. Check that the camera is online.",
    "camera_talk_unavailable": "The camera did not accept the talk request.",
    "no_camera_credentials": "This camera has no stored login, so talk cannot connect.",
    "unknown_camera": "The appliance does not recognise this camera yet.",
}


def is_camera_auth_error(text: str | None) -> bool:
    """True for an HTTP or RTSP 401/403 status in an error/status string."""
    text = f" {text or ''} "
    return any(marker in text for marker in (" 401 ", " 403 ", "HTTP 401", "HTTP 403", " 401\n", " 403\n"))


def note_camera_auth_failure(camera_id: str | None) -> None:
    if camera_id:
        _camera_auth_failures[camera_id] = time.monotonic()


def camera_auth_cooldown_remaining(camera_id: str | None) -> int:
    """Seconds left before talk may contact this camera again (0 = allowed)."""
    failed_at = _camera_auth_failures.get(camera_id or "")
    if failed_at is None:
        return 0
    remaining = CAMERA_AUTH_COOLDOWN_SECONDS - (time.monotonic() - failed_at)
    if remaining <= 0:
        _camera_auth_failures.pop(camera_id, None)
        return 0
    return int(remaining) + 1



# ---------------------------------------------------------------- ISAPI talk upload (2026-09-27)

class IsapiUploadRejected(RuntimeError):
    def __init__(self, status_code: int):
        super().__init__(f"camera audioData returned HTTP {status_code}")
        self.status_code = status_code


class _IsapiAudioUpload:
    """An accepted audioData upload: raw G.711 bytes go straight onto the
    camera's TCP connection (no HTTP chunk framing)."""

    def __init__(self, sock, status_code):
        self._sock = sock
        self.status_code = status_code  # None: the camera sent no immediate reply

    def sendall(self, data: bytes) -> None:
        self._sock.sendall(data)

    def close(self) -> None:
        import socket as _socket

        try:
            self._sock.shutdown(_socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()


def _read_http_head(sock, timeout: float):
    """(status, lowercase headers) of one HTTP response; its body is read
    and discarded when it has a Content-Length."""
    sock.settimeout(timeout)
    data = b""
    while b"\r\n\r\n" not in data:
        piece = sock.recv(4096)
        if not piece:
            raise ConnectionError("camera closed the connection")
        data += piece
        if len(data) > 65536:
            raise ConnectionError("oversized camera response")
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    remaining = int(headers.get("content-length") or 0) - len(rest)
    while remaining > 0:
        piece = sock.recv(min(remaining, 4096))
        if not piece:
            break
        remaining -= len(piece)
    return status, headers


def _send_audio_put(sock, host: str, path: str, authorization: str | None = None) -> None:
    lines = [
        f"PUT {path} HTTP/1.1",
        f"Host: {host}",
        "Content-Type: application/octet-stream",
        "Content-Length: 0",
        "Connection: keep-alive",
    ]
    if authorization:
        lines.append(f"Authorization: {authorization}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))


def open_isapi_audio_upload(host: str, path: str, username: str, password: str, *, timeout: float = 5.0) -> _IsapiAudioUpload:
    """Start an ISAPI two-way-audio upload the way cameras accept it:
    PUT <path> with Content-Length: 0 (digest-authenticated; the one 401
    challenge is the normal digest handshake, not a failed login), then
    the caller writes raw audio on the returned connection. A camera
    that sends no immediate reply is still streamed to."""
    import socket as _socket

    from requests.auth import HTTPDigestAuth
    from requests.utils import parse_dict_header

    hostname, _, port = host.partition(":")
    address = (hostname, int(port or 80))
    sock = _socket.create_connection(address, timeout=timeout)
    try:
        _send_audio_put(sock, host, path)
        try:
            status, headers = _read_http_head(sock, timeout)
        except _socket.timeout:
            status, headers = None, {}
        if status == 401 and headers.get("www-authenticate", "").lower().startswith("digest"):
            if headers.get("connection", "").lower() == "close":
                sock.close()
                sock = _socket.create_connection(address, timeout=timeout)
            digest = HTTPDigestAuth(username, password)
            digest.init_per_thread_state()
            digest._thread_local.chal = parse_dict_header(headers["www-authenticate"].split(" ", 1)[1])
            _send_audio_put(sock, host, path, digest.build_digest_header("PUT", f"http://{host}{path}"))
            try:
                status, headers = _read_http_head(sock, timeout)
            except _socket.timeout:
                status = None
        if status is not None and not 200 <= status < 300:
            raise IsapiUploadRejected(status)
        sock.settimeout(timeout)
        return _IsapiAudioUpload(sock, status)
    except BaseException:
        sock.close()
        raise


_appliance_channels: dict[str, WebSocket] = {}  # appliance_id -> its single open control WebSocket


def appliance_talk_channel_connected(appliance_id: str | None) -> bool:
    """Is this appliance's talk channel open to this cloud process?"""
    return bool(appliance_id) and appliance_id in _appliance_channels
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


async def _end_relay(session_id: str, notify_appliance: bool, reason: str) -> None:
    """The single cleanup path for every way a relay can end -- explicit
    stop, idle timeout, max-duration timeout, appliance disconnect, or
    the customer socket simply dropping. Always removes the session
    from _active_relays (so a later frame for this session_id can never
    be forwarded again -- this is what makes "no audio sent after
    stop" true) and always marks the durable customer_talk_sessions row
    'stopped' if it was still 'requested'. Idempotent: calling this
    twice for the same session_id is a harmless no-op the second time.

    2026-09-23 fix: this function previously had no logging or audit
    trail at all -- every relay ending here (which is EVERY relay: the
    browser's own wireTalkMic() never calls the REST /stop route while
    a WebSocket is open, only when a session is abandoned before the
    socket ever connects) was completely silent, both operationally and
    in audit_logs. `reason` is now always recorded via logger.info, and
    audit()'d under the session's own original requester -- but only
    when THIS call is the one that actually performs the 'requested' ->
    'stopped' transition (checked via the UPDATE's own rowcount), so a
    session already stopped by talk_sessions.py's own REST route (see
    that module's stop_talk_session(), which now also calls this
    function to make an explicit "hang up" while active actually notify
    the appliance immediately rather than leaving the relay to expire
    on its own idle/max-duration timeout) is never double-audited --
    that route's own audit() call already covered the customer-facing
    "why", this one adds the "what actually happened to the live relay
    and when", which is a materially different fact worth its own
    log line even when the audit entry is skipped as a duplicate."""
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
        cursor = db.execute(
            "UPDATE customer_talk_sessions SET state='stopped',ended_at=? WHERE id=? AND state='requested'",
            (now, session_id),
        )
        updated = cursor.rowcount > 0
        session_row = db.execute(
            "SELECT customer_id,camera_id,requested_by,role FROM customer_talk_sessions WHERE id=?",
            (session_id,),
        ).fetchone() if updated else None

    logger.info(
        "talk_audio_relay.relay_ended session_id=%s reason=%s camera_id=%s customer_id=%s",
        session_id, reason, relay.get("camera_id"), relay.get("customer_id"),
    )
    if updated and session_row:
        audit(
            {"email": session_row["requested_by"], "role": session_row["role"]},
            "customer.talk_session_ended",
            "customer_talk_session",
            session_id,
            {"camera_id": session_row["camera_id"], "reason": reason},
        )


async def _handle_appliance_message(appliance_id: str, raw: str) -> None:
    try:
        message = json.loads(raw)
    except (TypeError, ValueError):
        return
    session_id = message.get("session_id") if isinstance(message, dict) else None
    relay = _active_relays.get(session_id) if isinstance(session_id, str) else None
    if relay is None or relay.get("appliance_id") != appliance_id:
        return  # never act on another appliance's session
    kind = message.get("type")
    if kind == "started":
        event = relay.get("started_event")
        if event is not None:
            event.set()
    elif kind == "error":
        reason = message.get("reason") if message.get("reason") in TALK_ERROR_MESSAGES else "camera_talk_unavailable"
        relay["error_reason"] = reason
        event = relay.get("started_event")
        if event is not None:
            event.set()
        customer_socket = relay.get("websocket")
        if customer_socket is not None:
            await _send_talk_error(customer_socket, reason)


async def _announce_when_appliance_ready(session_id: str, websocket: WebSocket) -> None:
    relay = _active_relays.get(session_id)
    event = relay.get("started_event") if relay else None
    if event is None:
        return
    try:
        await asyncio.wait_for(event.wait(), timeout=APPLIANCE_ACK_WAIT_SECONDS)
    except asyncio.TimeoutError:
        pass
    relay = _active_relays.get(session_id)
    if relay is None or relay.get("error_reason"):
        return  # ended, or the error path already told the browser
    try:
        await websocket.send_text(json.dumps({"type": "ready"}))
    except Exception:
        pass


async def _send_talk_error(websocket: WebSocket, reason: str) -> None:
    """Tells the browser why talk ended (JSON text frame + close reason)."""
    message = TALK_ERROR_MESSAGES.get(reason, TALK_ERROR_MESSAGES["camera_talk_unavailable"])
    try:
        await websocket.send_text(json.dumps({"type": "error", "reason": reason, "message": message}))
    except Exception:
        pass
    try:
        await websocket.close(code=4502, reason=reason)
    except Exception:
        pass


async def stop_active_relay_if_any(session_id: str) -> None:
    """2026-09-23 fix: talk_sessions.py's stop_talk_session() (the REST
    "hang up" route -- the shared interface AACO/AAC Voice Call are
    meant to call, not just Live's own press-and-hold button) previously
    only updated customer_talk_sessions' DB row. It never touched
    _active_relays at all, so an explicit stop WHILE a WebSocket was
    still actively relaying audio left that relay running -- audio kept
    reaching the camera speaker until it separately expired on its own
    IDLE_TIMEOUT_SECONDS/MAX_RELAY_SECONDS, up to several seconds later.
    For a customer/AACO/AAC Voice Call action that reads as "hang up
    now", that's a real, user-visible correctness gap, not just a
    missing log line.

    Public (no leading underscore) specifically so talk_sessions.py can
    import and call it -- a local import there, to avoid the circular
    import this module already has the other direction (talk_sessions
    -> _authorized_talk_camera). A no-op, by design, when no relay is
    active for this session_id (the overwhelmingly common case: most
    stop calls arrive for a session that either already ended or never
    had its WebSocket open yet -- see _end_relay()'s own idempotent-pop
    behavior)."""
    if session_id in _active_relays:
        await _end_relay(session_id, notify_appliance=True, reason="stopped_via_rest_while_active")



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
        self.error_reason = None  # one of TALK_ERROR_MESSAGES' keys when start() fails
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

        # Never contact a camera that just rejected its login (see
        # CAMERA_AUTH_COOLDOWN_SECONDS) -- fail fast instead.
        if camera_auth_cooldown_remaining(self.camera.get("id")):
            self.error = "camera_auth_cooldown"
            self.error_reason = "camera_auth_cooldown"
            return False
        if not self.target:
            self.error = "Camera credentials or host unavailable."
            self.error_reason = "no_camera_credentials"
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
            self.error_reason = self.error_reason or "camera_unreachable"
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

            if opened.status_code in (401, 403):
                note_camera_auth_failure(self.camera.get("id"))
                self.error_reason = "camera_auth_failed"
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

            # audioData transport (2026-09-27, found on the real Front Door
            # camera): the camera accepted /open and every byte of a
            # chunked-transfer requests upload, yet played nothing, while
            # the Videoloft app talked through the same camera fine. The
            # camera does not decode HTTP chunked bodies. Cameras expect
            # the ISAPI talk upload the way go2rtc sends it: PUT audioData
            # with Content-Length: 0, the camera answers 200, and the raw
            # G.711 bytes then follow on that same TCP connection until it
            # is closed. See open_isapi_audio_upload().
            logger.info(
                "talk_isapi_diagnostic session=%s camera_id=%s event=audiodata_starting",
                self.session_id, self.camera.get("id"),
            )
            audiodata_started = True
            try:
                upload = open_isapi_audio_upload(
                    host, f"/ISAPI/System/TwoWayAudio/channels/{channel_id}/audioData",
                    target["username"], target["password"],
                )
            except IsapiUploadRejected as rejected:
                if rejected.status_code in (401, 403):
                    note_camera_auth_failure(self.camera.get("id"))
                    self.error_reason = "camera_auth_failed"
                raise RuntimeError(f"camera audioData returned HTTP {rejected.status_code}")
            logger.info(
                "talk_isapi_diagnostic session=%s camera_id=%s event=audiodata_response status=%s",
                self.session_id, self.camera.get("id"), upload.status_code,
            )
            try:
                for chunk in audio_chunks():
                    upload.sendall(chunk)
                exit_reason = "audiodata_completed_normally"
            finally:
                upload.close()

        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            if not getattr(self, "error_reason", None):
                self.error_reason = "camera_talk_unavailable" if self.target and "HTTP" in self.error else "camera_unreachable"
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
            # A camera that just rejected the login must not get a second
            # digest attempt from /close -- that doubled the failed logins
            # per press and is how cameras end up locked out.
            if self.error_reason == "camera_auth_failed":
                close_status = "skipped_auth_failed"
            else:
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
                raw = await websocket.receive_text()
                # Talkdown completion pass (2026-09-26): the appliance now
                # acknowledges each session ("started") or reports why it
                # could not reach the camera ("error"), so a camera failure
                # is shown to the customer instead of silently eating audio.
                await _handle_appliance_message(appliance["id"], raw)
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
                    await _end_relay(session_id, notify_appliance=False, reason="appliance_disconnected")

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
                reason = getattr(local_relay, "error_reason", None) or "camera_talk_unavailable"
                message = TALK_ERROR_MESSAGES.get(reason, TALK_ERROR_MESSAGES["camera_talk_unavailable"])
                try:
                    await websocket.send_text(json.dumps({"type": "error", "reason": reason, "message": message}))
                except Exception:
                    pass
                await websocket.close(code=4503, reason=reason)
                # The session row used to stay 'requested' until its expiry
                # sweep; end it now, recording why (logged + audited).
                _active_relays[session_id] = {
                    "camera_id": camera["id"], "appliance_id": camera["appliance_id"],
                    "customer_id": identity["customer_id"], "created_at": time.monotonic(),
                }
                await _end_relay(session_id, notify_appliance=False, reason=reason)
                return

        now = time.monotonic()

        _active_relays[session_id] = {
            "camera_id": camera["id"],
            "appliance_id": camera["appliance_id"],
            "customer_id": identity["customer_id"],
            "created_at": now,
            "websocket": websocket,
            "started_event": asyncio.Event() if appliance_channel is not None else None,
        }
        if local_relay is not None:
            await websocket.send_text(json.dumps({"type": "ready"}))

        ack_task = None
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
            # The appliance's confirmation is awaited in the background so
            # audio keeps flowing (and a quick release still ends the
            # session immediately); an older appliance never acknowledges,
            # and after the wait the browser is told the session is live.
            ack_task = asyncio.create_task(_announce_when_appliance_ready(session_id, websocket))

        # 2026-09-23 fix: _end_relay() now records WHY a relay ended (see
        # its own docstring) -- this default covers the loop's two
        # relay-vanished-out-from-under-us breaks, which include the new
        # "stopped explicitly via the REST route while this WebSocket
        # was still active" case (talk_sessions.py's stop_talk_session()
        # now calls _end_relay() directly, removing this session from
        # _active_relays before this loop ever notices).
        end_reason = "session_ended_externally"

        try:
            while True:
                relay = _active_relays.get(session_id)

                if relay is None:
                    break

                if (
                    time.monotonic() - relay["created_at"]
                    > MAX_RELAY_SECONDS
                ):
                    end_reason = "max_duration_timeout"
                    break

                try:
                    frame = await asyncio.wait_for(
                        websocket.receive_bytes(),
                        timeout=IDLE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    end_reason = "idle_timeout"
                    break

                if local_relay is not None:
                    local_relay.send_pcm16(frame)
                    continue

                channel = _appliance_channels.get(
                    relay["appliance_id"]
                )

                if channel is None:
                    end_reason = "appliance_disconnected"
                    break

                await channel.send_text(
                    json.dumps({
                        "type": "audio",
                        "session_id": session_id,
                        "pcm_b64": base64.b64encode(frame).decode(),
                    })
                )

        except (WebSocketDisconnect, RuntimeError):
            # RuntimeError: the socket was already closed server-side (an
            # appliance "error" closes it via _send_talk_error()).
            end_reason = "customer_disconnected"

        finally:
            if ack_task is not None:
                ack_task.cancel()
            relay_now = _active_relays.get(session_id)
            if relay_now is not None and relay_now.get("error_reason"):
                end_reason = relay_now["error_reason"]
            if local_relay is not None:
                await asyncio.to_thread(local_relay.stop)

            await _end_relay(
                session_id,
                notify_appliance=(local_relay is None),
                reason=end_reason,
            )
