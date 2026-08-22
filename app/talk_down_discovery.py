"""Automatic talk-down (push-to-talk speaker) capability discovery.

Runs on the appliance (RUNTIME_ROLE in {"edge","combined"}) as a
periodic background worker, mirroring recording_uploader.py's/
analytics_sync.py's own established architecture: duplicated
control-plane auth helpers, its own camera_number -> camera_id map
refreshed via the existing GET /api/appliance/configuration, and a
worker loop dispatched through asyncio.to_thread() from main.py's
lifespan so a slow/hanging ONVIF probe never blocks the event loop.

Discovery is entirely ONVIF-driven -- GetProfiles, then
GetAudioOutputConfigurations, both standard ONVIF SOAP calls, using
WS-Security UsernameToken digest auth (the same mechanism this
session's proven read-only backchannel-capability probe scripts
already validated against the real lab cameras). Nothing here reads
or branches on a camera_number or a camera model string at all --
every camera this appliance knows about (via the same camera_number ->
{camera_id, host, username, password} resolution camera_url() already
uses for RTSP) is probed identically, using only the ONVIF response
content to decide the outcome. See test_talk_down_discovery.py's
structural check for the concrete proof of this.

Three-way outcome, not a boolean -- this is the module's central
design point:
  - "supported": GetProfiles succeeded, at least one profile carries a
    real AudioOutputConfiguration/AudioOutputToken reference. Reported
    to the cloud as talk_down: {supported: true, metadata: {...}}.
  - "unsupported": GetProfiles succeeded, NO profile has any audio
    output configuration at all -- a completed, conclusive check that
    found nothing. Reported as talk_down: {supported: false}.
  - "error": the probe could not complete at all (timeout, connection
    refused, auth failure, malformed/unexpected response). NEVER
    reported as talk_down: {supported: false} -- that would silently
    convert "we don't know" into "confirmed unsupported", exactly the
    failure mode the milestone's requirements explicitly forbid. An
    "error" outcome causes this camera to be skipped from this cycle's
    POST /api/appliance/cameras payload entirely; the cloud route
    already treats a camera with no talk_down key in the payload as
    "leave the existing value untouched" (see appliance_cloud.py's
    cameras() route), so an error here correctly preserves whatever
    was last known (including "never yet verified", if this is the
    first attempt) rather than downgrading it.

"Camera rescan must refresh the capability": this worker's own
periodic loop IS the rescan mechanism -- every cycle re-probes every
known camera and reports a fresh result (superseding the previous one
on the cloud side, which the extended cameras() route already
supports via a plain UPDATE). Deliberately NOT triggered by Live View
page loads or any other customer-facing request -- see
DISCOVERY_INTERVAL_SECONDS's own default, chosen to be a background
cadence, not a per-page-load probe.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

logger = logging.getLogger("anyaicam.talk_down_discovery")

RUNTIME_ROLE = os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower()
TALK_DOWN_DISCOVERY_ENABLED = os.environ.get("ANYAICAM_TALK_DOWN_DISCOVERY_ENABLED", "false").strip().lower() == "true"
CLOUD_URL = os.environ.get("ANYAICAM_CLOUD_URL", "").strip().rstrip("/")
STATE_DIR_DEFAULT = "/var/lib/anyaicam"
CREDENTIAL_FILE = os.environ.get("ANYAICAM_CREDENTIAL_FILE", f"{os.environ.get('ANYAICAM_STATE_DIR', STATE_DIR_DEFAULT)}/credential.json")
ONVIF_PORT = int(os.environ.get("ANYAICAM_ONVIF_PORT", "80"))
ONVIF_TIMEOUT_SECONDS = max(1, int(os.environ.get("ANYAICAM_ONVIF_TIMEOUT_SECONDS", "8")))
# A background rescan cadence, not a per-page-load probe -- default 1
# hour is deliberately much less frequent than the live detection
# pipeline's ~5s cycle; capability rarely changes, and each probe is a
# real authenticated network call against customer camera hardware.
DISCOVERY_INTERVAL_SECONDS = max(300, int(os.environ.get("ANYAICAM_TALK_DOWN_DISCOVERY_INTERVAL_SECONDS", "3600")))
CONFIG_REFRESH_SECONDS = max(60.0, float(os.environ.get("ANYAICAM_TALK_DOWN_CONFIG_REFRESH_SECONDS", "300.0")))

talk_down_discovery_state: dict = {"worker_status": "disabled", "last_scan_at": None, "last_results": {}}

_lock = None  # set lazily in _get_lock() -- keeps this module import-safe without requiring threading at module scope for pure-logic tests


def _get_lock():
    global _lock
    if _lock is None:
        import threading
        _lock = threading.Lock()
    return _lock


_camera_map: dict[int, dict] = {}  # camera_number -> {"camera_id":..., "site_id":...}


# ---------------------------------------------------------------- control plane (duplicated per this project's established convention)

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
        "User-Agent": "AnyAiCam-TalkDownDiscovery/0.1",
        "Authorization": f"Bearer {credential}",
        "X-Appliance-ID": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_urlsafe(18),
    }


def _control_plane_post(path: str, payload: dict) -> dict | None:
    identity = _load_appliance_identity()
    if not identity or not CLOUD_URL:
        return None
    appliance_id, credential = identity
    headers = {"Content-Type": "application/json", **_control_plane_headers(appliance_id, credential)}
    request = urllib.request.Request(CLOUD_URL + path, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        logger.warning("talk_down_discovery.control_plane_http_error path=%s status=%s", path, error.code)
        return None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        logger.warning("talk_down_discovery.control_plane_unreachable path=%s error=%s", path, error)
        return None


def _control_plane_get(path: str) -> dict | None:
    identity = _load_appliance_identity()
    if not identity or not CLOUD_URL:
        return None
    appliance_id, credential = identity
    request = urllib.request.Request(CLOUD_URL + path, headers=_control_plane_headers(appliance_id, credential), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        logger.warning("talk_down_discovery.control_plane_http_error path=%s status=%s", path, error.code)
        return None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        logger.warning("talk_down_discovery.control_plane_unreachable path=%s error=%s", path, error)
        return None


def _refresh_camera_map() -> None:
    """Same GET /api/appliance/configuration this appliance already
    polls for its other workers -- never writes anything, and a
    failed/unreachable poll just leaves the previous mapping in place."""
    response = _control_plane_get("/api/appliance/configuration")
    if not isinstance(response, dict):
        return
    cameras = response.get("cameras")
    if not isinstance(cameras, list):
        return
    mapping: dict[int, dict] = {}
    for item in cameras:
        if not isinstance(item, dict):
            continue
        camera_id = item.get("id")
        camera_number = item.get("camera_number")
        site_id = item.get("site_id")
        if not isinstance(camera_id, str) or not camera_id.strip():
            continue
        if isinstance(camera_number, bool) or not isinstance(camera_number, int):
            continue
        if not isinstance(site_id, str) or not site_id.strip():
            continue
        mapping[camera_number] = {"camera_id": camera_id, "site_id": site_id}
    with _get_lock():
        _camera_map.clear()
        _camera_map.update(mapping)


def _known_camera_numbers() -> list[int]:
    with _get_lock():
        return sorted(_camera_map)


def _camera_identity(camera_number: int) -> dict | None:
    with _get_lock():
        return _camera_map.get(camera_number)


def _camera_credentials(camera_number: int) -> tuple[str, str, str] | None:
    """Resolves (host, username, password) for a configured camera slot
    purely from its own CAMERA{N}_HOST/USERNAME/PASSWORD env vars --
    the exact same convention camera_url() already uses for RTSP.
    No camera number is ever special-cased here beyond selecting which
    env var prefix to read; a missing/incomplete env var set for a
    given slot is read as "not configured", never guessed at."""
    try:
        host = os.environ[f"CAMERA{camera_number}_HOST"]
        username = os.environ[f"CAMERA{camera_number}_USERNAME"]
        password = os.environ[f"CAMERA{camera_number}_PASSWORD"]
    except KeyError:
        return None
    if not host or not username:
        return None
    return host, username, password


# ---------------------------------------------------------------- ONVIF SOAP (WS-Security digest, proven this session against real cameras)

_MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"


def _ws_security_header(username: str, password: str) -> str:
    nonce = secrets.token_bytes(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    nonce_b64 = base64.b64encode(nonce).decode()
    return (
        '<s:Header><wsse:Security s:mustUnderstand="1" '
        'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
        'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f'<wsse:UsernameToken><wsse:Username>{username}</wsse:Username>'
        '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-username-token-profile-1.0#PasswordDigest">'
        f'{digest}</wsse:Password>'
        '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
        f'{nonce_b64}</wsse:Nonce><wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header>'
    )


def _soap_call(host: str, username: str, password: str, body: str, action: str) -> ET.Element:
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        f"{_ws_security_header(username, password)}<s:Body>{body}</s:Body></s:Envelope>"
    )
    request = urllib.request.Request(
        f"http://{host}:{ONVIF_PORT}/onvif/media_service",
        data=envelope.encode(), method="POST",
        headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'},
    )
    with urllib.request.urlopen(request, timeout=ONVIF_TIMEOUT_SECONDS) as response:
        return ET.fromstring(response.read())


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_by_local_name(element: ET.Element, name: str) -> ET.Element | None:
    """Depth-first search for the first descendant whose tag's local
    name (namespace stripped) matches, regardless of which namespace
    prefix a particular camera vendor happened to bind it to. Real
    ONVIF implementations are inconsistent about whether an element
    like AudioOutputConfiguration/SendPrimacy is emitted under the
    media-wsdl namespace or the schema namespace -- matching on local
    name only is the robust, standards-correct way to handle that
    variance, not a testing convenience."""
    for child in element.iter():
        if _local_name(child.tag) == name:
            return child
    return None


def _findall_by_local_name(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element.iter() if _local_name(child.tag) == name]


def _parse_profiles(root: ET.Element) -> list[dict]:
    """Pure parsing, no network -- takes an already-fetched GetProfiles
    response and extracts {profile_token, audio_output_token} per
    profile. audio_output_token is None when a profile carries no
    AudioOutputConfiguration reference at all."""
    profiles = []
    for profile in _findall_by_local_name(root, "Profiles"):
        token = profile.get("token")
        audio_output_config = _find_by_local_name(profile, "AudioOutputConfiguration")
        audio_output_token = audio_output_config.get("token") if audio_output_config is not None else None
        profiles.append({"profile_token": token, "audio_output_token": audio_output_token})
    return profiles


def _parse_audio_output_configurations(root: ET.Element) -> dict[str, dict]:
    """Pure parsing -- takes an already-fetched
    GetAudioOutputConfigurations response and returns
    {config_token: {send_primacy, output_token, name}}. Any of these
    fields may be absent depending on the camera's own response; this
    never raises for a missing optional field, only captures what's
    actually present."""
    results = {}
    for config in _findall_by_local_name(root, "Configurations"):
        token = config.get("token")
        if not token:
            continue
        send_primacy_el = _find_by_local_name(config, "SendPrimacy")
        output_token_el = _find_by_local_name(config, "OutputToken")
        name_el = _find_by_local_name(config, "Name")
        results[token] = {
            "send_primacy": send_primacy_el.text if send_primacy_el is not None else None,
            "output_token": output_token_el.text if output_token_el is not None else None,
            "name": name_el.text if name_el is not None else None,
        }
    return results


def probe_talk_down_capability(host: str, username: str, password: str) -> dict:
    """The one real network-touching function in this module. Returns
    exactly one of three outcomes -- see module docstring:
      {"outcome": "supported", "metadata": {...}}
      {"outcome": "unsupported", "metadata": None}
      {"outcome": "error", "metadata": None, "error_reason": "..."}

    Never raises. Never invents a model-specific code path -- the only
    inputs are host/username/password and the ONVIF responses they
    produce."""
    try:
        profiles_root = _soap_call(
            host, username, password,
            f'<trt:GetProfiles xmlns:trt="{_MEDIA_NS}"/>',
            f"{_MEDIA_NS}/GetProfiles",
        )
    except Exception as error:
        return {"outcome": "error", "metadata": None, "error_reason": f"GetProfiles failed: {type(error).__name__}"}

    try:
        profiles = _parse_profiles(profiles_root)
    except Exception as error:
        return {"outcome": "error", "metadata": None, "error_reason": f"GetProfiles response unparseable: {type(error).__name__}"}

    audio_profiles = [profile for profile in profiles if profile["audio_output_token"]]
    if not audio_profiles:
        return {"outcome": "unsupported", "metadata": None}

    # At least one profile has an audio output reference -- this alone
    # already satisfies "detect AudioOutputConfiguration / output
    # token". Best-effort enrichment with GetAudioOutputConfigurations
    # (SendPrimacy, name) below: a failure here does NOT downgrade the
    # already-confirmed "supported" outcome -- it only means less
    # metadata is captured this cycle, which is captured on a later
    # rescan instead.
    chosen = audio_profiles[0]
    metadata = {"profile_token": chosen["profile_token"], "audio_output_token": chosen["audio_output_token"]}
    try:
        configs_root = _soap_call(
            host, username, password,
            f'<trt:GetAudioOutputConfigurations xmlns:trt="{_MEDIA_NS}"/>',
            f"{_MEDIA_NS}/GetAudioOutputConfigurations",
        )
        configs = _parse_audio_output_configurations(configs_root)
        config = configs.get(chosen["audio_output_token"])
        if config:
            metadata.update({key: value for key, value in config.items() if value is not None})
    except Exception as error:
        logger.info("talk_down_discovery.audio_output_config_enrichment_failed host_redacted error=%s", type(error).__name__)

    return {"outcome": "supported", "metadata": metadata}


# ---------------------------------------------------------------- reporting + worker

def _report_capability(camera_id: str, probe_result: dict) -> bool:
    """Reports one camera's probe outcome to the cloud via the existing
    authenticated POST /api/appliance/cameras route. An "error" outcome
    is never reported at all -- see module docstring for why omitting
    the talk_down key entirely (rather than sending supported:false)
    is what correctly preserves an existing/unverified result."""
    outcome = probe_result["outcome"]
    if outcome == "error":
        return False
    payload = {"id": camera_id, "talk_down": {"supported": outcome == "supported"}}
    if probe_result.get("metadata"):
        payload["talk_down"]["metadata"] = probe_result["metadata"]
    response = _control_plane_post("/api/appliance/cameras", {"cameras": [payload]})
    return isinstance(response, dict) and response.get("status") == "accepted"


def _run_discovery_cycle() -> dict:
    """One full cycle: probes every currently-known camera and reports
    each definitive result. Returns a summary dict for logging/state.
    Dispatched via asyncio.to_thread() from the worker loop below, so a
    slow/hanging ONVIF call never blocks the event loop -- the same
    precedent recording_uploader.py's per-camera dispatch already
    established for this project."""
    results = {}
    for camera_number in _known_camera_numbers():
        identity = _camera_identity(camera_number)
        if not identity:
            continue
        credentials = _camera_credentials(camera_number)
        if not credentials:
            results[camera_number] = "not_configured"
            continue
        host, username, password = credentials
        probe_result = probe_talk_down_capability(host, username, password)
        results[camera_number] = probe_result["outcome"]
        if probe_result["outcome"] != "error":
            _report_capability(identity["camera_id"], probe_result)
        else:
            logger.warning("talk_down_discovery.probe_error camera_number=%s reason=%s", camera_number, probe_result.get("error_reason"))
    return results


async def talk_down_discovery_worker() -> None:
    if RUNTIME_ROLE not in {"edge", "combined"} or not TALK_DOWN_DISCOVERY_ENABLED:
        talk_down_discovery_state["worker_status"] = "disabled"
        while True:
            await asyncio.sleep(3600)
    talk_down_discovery_state["worker_status"] = "running"
    logger.info("talk_down_discovery.worker_started")
    last_config_refresh = 0.0
    while True:
        try:
            now = time.monotonic()
            if now - last_config_refresh >= CONFIG_REFRESH_SECONDS:
                await asyncio.to_thread(_refresh_camera_map)
                last_config_refresh = now
            results = await asyncio.to_thread(_run_discovery_cycle)
            talk_down_discovery_state["last_scan_at"] = datetime.now().isoformat()
            talk_down_discovery_state["last_results"] = results
            await asyncio.sleep(DISCOVERY_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("talk_down_discovery.worker_iteration_failed error=%s", error)
            await asyncio.sleep(DISCOVERY_INTERVAL_SECONDS)
