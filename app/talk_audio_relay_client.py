"""Push-to-talk audio transport -- appliance relay client half.

Maintains a SINGLE persistent outbound WebSocket to the cloud's
/api/appliance/talk/channel (reconnecting on drop, matching this
project's own established worker-retry pattern), authenticated with
the same Bearer/nonce credential every other control-plane call this
appliance makes already uses -- duplicated helpers, per this project's
established convention (recording_uploader.py, analytics_sync.py,
talk_down_discovery.py all do the same).

Per talk session (identified by session_id, which this module never
invents -- it only ever comes from the cloud's own "start" message):
a dedicated ffmpeg subprocess transcodes the browser's raw PCM audio
(sample rate supplied by the cloud, itself read from the browser's own
AudioContext) into PCMU/G.711 mu-law 8kHz mono -- the format this
session's real ONVIF capability probe confirmed the lab cameras
expect -- and a TalkDownTransport (talk_down_transport.py) delivers
each transcoded chunk to the camera. Sessions are fully independent:
each gets its own subprocess and its own transport instance, keyed
only by session_id, so one camera's session can never affect another's
even if both happen to be active on this appliance at once.

This is audio-only, entirely separate from the existing per-camera
live/recording ffmpeg processes (main.py's start_live_stream()/
start_recording()) and from motion_detector()/ai_person_detector()'s
own frame grabs -- a new session's ffmpeg subprocess has no video
input, no video output, and shares no state, no executor, and no
file/socket with any of them. No SETUP/RECORD/RTP send is exercised
against a real camera by anything that runs automatically in this
commit -- see ANYAICAM_TALK_AUDIO_ENABLED's default below.
"""

import asyncio
import json
import logging
import os
import secrets
import subprocess
import time
from datetime import datetime, timezone

from talk_down_transport import OnvifBackchannelTransport

try:
    import websockets
except ImportError:
    websockets = None

logger = logging.getLogger("anyaicam.talk_audio_relay_client")

RUNTIME_ROLE = os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower()
# Deliberately its own flag, independent of ANYAICAM_TALK_DOWN_DISCOVERY_ENABLED:
# discovery only ever reads capability, never touches a camera's audio
# input at all. This flag gates something materially different -- an
# appliance that will actually attempt to deliver real audio once a
# customer presses a mic button -- so it gets its own explicit,
# default-false opt-in rather than inheriting discovery's.
TALK_AUDIO_ENABLED = os.environ.get("ANYAICAM_TALK_AUDIO_ENABLED", "false").strip().lower() == "true"
CLOUD_URL = os.environ.get("ANYAICAM_CLOUD_URL", "").strip().rstrip("/")
CREDENTIAL_FILE = os.environ.get("ANYAICAM_CREDENTIAL_FILE", f"{os.environ.get('ANYAICAM_STATE_DIR', '/var/lib/anyaicam')}/credential.json")
RECONNECT_DELAY_SECONDS = max(1, int(os.environ.get("ANYAICAM_TALK_AUDIO_RECONNECT_SECONDS", "5")))
ONVIF_PORT = int(os.environ.get("ANYAICAM_ONVIF_PORT", "80"))
PCMU_SAMPLE_RATE = 8000

talk_audio_relay_state: dict = {"channel_status": "disabled", "active_sessions": 0}

_sessions: dict[str, dict] = {}  # session_id -> {"process": Popen, "transport": TalkDownTransport, "camera_id": str}


def _load_appliance_identity() -> tuple[str, str] | None:
    try:
        with open(CREDENTIAL_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    appliance_id = str(data.get("appliance_id") or "").strip()
    credential = str(data.get("credential") or "").strip()
    if not appliance_id or not credential:
        return None
    return appliance_id, credential


def _control_plane_headers(appliance_id: str, credential: str) -> dict:
    return {
        "User-Agent": "AnyAiCam-TalkAudioRelay/0.1",
        "Authorization": f"Bearer {credential}",
        "X-Appliance-ID": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_urlsafe(18),
    }


def _channel_url() -> str | None:
    if not CLOUD_URL:
        return None
    scheme = "wss" if CLOUD_URL.startswith("https") else "ws"
    host_part = CLOUD_URL.split("://", 1)[-1]
    return f"{scheme}://{host_part}/api/appliance/talk/channel"


def _camera_credentials(camera_number_or_id: str) -> tuple[str, str, str] | None:
    """Resolves (host, username, password) purely from
    CAMERA{N}_HOST/USERNAME/PASSWORD env vars, exactly like
    talk_down_discovery.py's own resolver and camera_url() itself --
    duplicated here for the same reason those two modules already
    duplicate this pattern independently rather than sharing it."""
    try:
        host = os.environ[f"CAMERA{camera_number_or_id}_HOST"]
        username = os.environ[f"CAMERA{camera_number_or_id}_USERNAME"]
        password = os.environ[f"CAMERA{camera_number_or_id}_PASSWORD"]
    except KeyError:
        return None
    if not host or not username:
        return None
    return host, username, password


def _camera_number_for(camera_id: str, camera_map: dict[int, dict]) -> int | None:
    for camera_number, identity in camera_map.items():
        if identity.get("camera_id") == camera_id:
            return camera_number
    return None


def _build_transport(host: str, username: str, password: str, metadata: dict) -> OnvifBackchannelTransport:
    """The one, and currently only, concrete transport this milestone
    builds -- selected because the discovered metadata indicates ONVIF
    audio-output support (the only thing ever checked here; no camera
    number or model name is read anywhere in this function). A future
    vendor-specific fallback would be a second branch here, selected
    the same way -- by what the metadata says, never by which camera
    number this happens to be."""
    rtsp_path = metadata.get("rtsp_path") or "/"
    return OnvifBackchannelTransport(host=host, port=ONVIF_PORT, username=username, password=password, rtsp_path=rtsp_path)


def _terminate_process(process: subprocess.Popen) -> None:
    """Shared by _stop_session() and _start_session()'s own
    connect-failure cleanup path -- closing stdin first (so ffmpeg sees
    EOF and can exit on its own) then terminating, tolerating a process
    that's already gone."""
    try:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        process.terminate()
    except OSError:
        pass


def _start_session(session_id: str, camera_id: str, metadata: dict, sample_rate: int, camera_map: dict[int, dict]) -> bool:
    """Starts one session's ffmpeg transcode subprocess and transport,
    returning True only if the session is now actually live (added to
    _sessions) -- the caller uses this to decide whether it's safe to
    start draining transcoded audio at all. Never touches any OTHER
    session_id's state -- each call only ever reads/writes
    _sessions[session_id]."""
    if session_id in _sessions:
        return False  # a duplicate "start" for an already-running session -- ignore, never restart what's already live
    camera_number = _camera_number_for(camera_id, camera_map)
    if camera_number is None:
        logger.warning("talk_audio_relay_client.unknown_camera session_id=%s camera_id=%s", session_id, camera_id)
        return False
    credentials = _camera_credentials(camera_number)
    if not credentials:
        logger.warning("talk_audio_relay_client.no_camera_credentials session_id=%s camera_number=%s", session_id, camera_number)
        return False
    host, username, password = credentials

    process = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
         "-ar", str(PCMU_SAMPLE_RATE), "-ac", "1", "-f", "mulaw", "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    transport = _build_transport(host, username, password, metadata)

    # connect() must succeed before this session is ever considered
    # live: _drain_transcoded_audio() calls transport.send() as soon as
    # it sees a transcoded chunk, and send() before connect() is a
    # guaranteed failure (the exact production bug this fixes -- see
    # OnvifBackchannelTransport.send()'s own RuntimeError). A session
    # that never got a working RTP channel must never reach _sessions,
    # never get a drain task, and must leave nothing running behind.
    try:
        transport.connect()
    except Exception as error:
        logger.warning(
            "talk_audio_relay_client.transport_connect_failed session_id=%s camera_id=%s camera_number=%s error=%s",
            session_id, camera_id, camera_number, error,
        )
        _terminate_process(process)
        try:
            transport.close()
        except Exception:
            pass
        return False

    _sessions[session_id] = {"process": process, "transport": transport, "camera_id": camera_id, "reader_task": None}
    talk_audio_relay_state["active_sessions"] = len(_sessions)
    return True


def _feed_audio(session_id: str, pcm_bytes: bytes) -> None:
    session = _sessions.get(session_id)
    if session is None:
        return  # this session already ended (or was never started) -- never revives it, never writes to a closed pipe
    process = session["process"]
    if process.stdin is None or process.stdin.closed:
        return
    try:
        process.stdin.write(pcm_bytes)
        process.stdin.flush()
    except (BrokenPipeError, OSError):
        pass


def _stop_session(session_id: str) -> None:
    session = _sessions.pop(session_id, None)
    talk_audio_relay_state["active_sessions"] = len(_sessions)
    if session is None:
        return
    _terminate_process(session["process"])
    transport = session["transport"]
    try:
        transport.close()
    except Exception:
        pass


async def _drain_transcoded_audio(session_id: str) -> None:
    """Reads transcoded PCMU bytes from one session's ffmpeg stdout and
    hands each chunk to that session's own transport -- never any other
    session's. Exits cleanly (without touching _sessions itself, which
    is _stop_session()'s job) once the process's stdout closes."""
    session = _sessions.get(session_id)
    if session is None:
        return
    process = session["process"]
    transport = session["transport"]
    loop = asyncio.get_running_loop()
    if process.stdout is None:
        return
    try:
        while True:
            chunk = await loop.run_in_executor(None, process.stdout.read, 160)  # 160 bytes = 20ms of 8kHz mu-law
            if not chunk:
                break
            current = _sessions.get(session_id)
            if current is None or current["process"] is not process:
                break  # this session was stopped/replaced while we were reading -- stop forwarding immediately
            try:
                transport.send(chunk)
            except Exception as error:
                logger.warning("talk_audio_relay_client.transport_send_failed session_id=%s error=%s", session_id, error)
                break
    except Exception as error:
        logger.warning("talk_audio_relay_client.drain_failed session_id=%s error=%s", session_id, error)


async def _handle_message(raw_message: str, camera_map: dict[int, dict]) -> None:
    try:
        message = json.loads(raw_message)
    except json.JSONDecodeError:
        return
    message_type = message.get("type")
    session_id = message.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return

    if message_type == "start":
        camera_id = message.get("camera_id")
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        sample_rate = message.get("sample_rate") if isinstance(message.get("sample_rate"), int) else 48000
        if not isinstance(camera_id, str):
            return
        started = _start_session(session_id, camera_id, metadata, sample_rate, camera_map)
        if started:
            asyncio.create_task(_drain_transcoded_audio(session_id))
        # A duplicate start (already running), an unknown camera, missing
        # credentials, or a failed transport.connect() all return False --
        # in every one of those cases the session either doesn't exist or
        # already has its own drain task running, so no new one is needed.
    elif message_type == "audio":
        pcm_b64 = message.get("pcm_b64")
        if isinstance(pcm_b64, str):
            import base64
            try:
                pcm_bytes = base64.b64decode(pcm_b64)
            except Exception:
                return
            _feed_audio(session_id, pcm_bytes)
    elif message_type == "stop":
        _stop_session(session_id)


async def talk_audio_relay_client_worker() -> None:
    if RUNTIME_ROLE not in {"edge", "combined"} or not TALK_AUDIO_ENABLED:
        talk_audio_relay_state["channel_status"] = "disabled"
        while True:
            await asyncio.sleep(3600)
    if websockets is None:
        talk_audio_relay_state["channel_status"] = "dependency_missing"
        logger.warning("talk_audio_relay_client.websockets_not_installed")
        while True:
            await asyncio.sleep(3600)

    talk_audio_relay_state["channel_status"] = "connecting"
    while True:
        try:
            identity = _load_appliance_identity()
            channel_url = _channel_url()
            if not identity or not channel_url:
                talk_audio_relay_state["channel_status"] = "unconfigured"
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                continue
            appliance_id, credential = identity

            # main.py's own recording_uploader/analytics_sync camera-map
            # convention -- reused via a fresh fetch each reconnect
            # rather than kept fully in sync continuously, since a talk
            # session only ever starts after a fresh cloud round trip
            # anyway (the customer's own /talk/start call), so a
            # slightly-stale map only matters for the rare case of a
            # camera added mid-connection.
            camera_map = await asyncio.to_thread(_fetch_camera_map, appliance_id, credential)

            async with websockets.connect(channel_url, additional_headers=_control_plane_headers(appliance_id, credential)) as connection:
                talk_audio_relay_state["channel_status"] = "connected"
                logger.info("talk_audio_relay_client.channel_connected")
                async for raw_message in connection:
                    await _handle_message(raw_message, camera_map)
        except asyncio.CancelledError:
            for session_id in list(_sessions):
                _stop_session(session_id)
            raise
        except Exception as error:
            logger.warning("talk_audio_relay_client.channel_error error=%s", error)
        talk_audio_relay_state["channel_status"] = "reconnecting"
        for session_id in list(_sessions):
            _stop_session(session_id)  # the control channel dropped -- every in-flight session on it is now unreachable
        await asyncio.sleep(RECONNECT_DELAY_SECONDS)


def _fetch_camera_map(appliance_id: str, credential: str) -> dict[int, dict]:
    """Same GET /api/appliance/configuration every other edge worker
    already polls -- a plain, synchronous urllib call (dispatched via
    asyncio.to_thread by the caller), returning {} on any failure
    rather than raising, so a transient network blip never crashes the
    relay connection attempt."""
    import urllib.error
    import urllib.request

    if not CLOUD_URL:
        return {}
    headers = _control_plane_headers(appliance_id, credential)
    request = urllib.request.Request(CLOUD_URL + "/api/appliance/configuration", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode() or "{}")
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    cameras = data.get("cameras")
    if not isinstance(cameras, list):
        return {}
    mapping: dict[int, dict] = {}
    for item in cameras:
        if not isinstance(item, dict):
            continue
        camera_id = item.get("id")
        camera_number = item.get("camera_number")
        if isinstance(camera_id, str) and camera_id.strip() and isinstance(camera_number, int) and not isinstance(camera_number, bool):
            mapping[camera_number] = {"camera_id": camera_id}
    return mapping
