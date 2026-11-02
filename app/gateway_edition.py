"""Edition defaults and video transport boundaries for the shared VMS.

Selecting an edition never changes recording, retention, analytics or licensing.
Existing installations without a selection retain Full VMS behavior.
"""
import math
import os

EDITION_ENV = "ANYAICAM_GATEWAY_EDITION"
EDITIONS = ("full_vms", "local_gateway", "cloud_gateway")


def current_edition() -> str:
    value = os.environ.get(EDITION_ENV, "").strip().lower()
    if not value:
        return "full_vms"
    if value not in EDITIONS:
        raise ValueError(f"Invalid {EDITION_ENV}: expected one of {', '.join(EDITIONS)}")
    return value


def default_product_mode() -> str:
    """Lowest-priority mode default; cloud configuration can still upgrade it."""
    return {"full_vms": "", "local_gateway": "local", "cloud_gateway": "hybrid"}[current_edition()]


VIDEO_FLAGS = {
    "ANYAICAM_RECORDING_UPLOAD_ENABLED": "recording",
    "ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED": "event",
    "ANYAICAM_LIVE_RELAY_ENABLED": "live",
    "ANYAICAM_CLOUD_BACKUP_ENABLED": "backup",
    "ANYAICAM_CLOUD_UPLOAD_ENABLED": "backup",
}


def video_transport_allowed(kind: str) -> bool:
    edition = current_edition()
    if edition == "full_vms":
        return True
    if edition == "local_gateway" or kind != "event":
        return False
    # Edition selection alone must never start uploads. Camera entitlement,
    # credentials, region and server policy remain enforced by the uploader.
    return os.environ.get("ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED", "").strip().lower() == "true"


def event_camera_authorized(camera_number: int, identity: dict, provisioned: list[int]) -> bool:
    """Gateway uploads need the existing accepted camera licence as well as cloud eligibility."""
    if current_edition() == "full_vms":
        return True  # retain the existing Full VMS authorization path
    if not video_transport_allowed("event") or identity.get("cloud_recording_mode") != "motion":
        return False
    try:
        from vms_capacity import licensed_camera_numbers
        licensed = licensed_camera_numbers(provisioned)
        # None means a cloud-control-plane runtime, not an edge licence.
        return isinstance(licensed, list) and camera_number in licensed
    except Exception:
        return False


def event_clip_reference_allowed(event_id: str, clip_url: str) -> bool:
    """Gateway clips must be outputs of the existing build_motion_event_clip producer."""
    if current_edition() == "full_vms":
        return True
    if not isinstance(event_id, str) or not event_id or any(char in event_id for char in '/\\?#'):
        return False
    return clip_url == f"/recordings/clips/motion/motion_{event_id}.mp4"


def event_clip_duration_allowed(duration: float | None) -> bool:
    if current_edition() == "full_vms":
        return True
    # Reuse the existing event-media sanity limit, never a new quota or
    # retention policy. Unknown/unprobeable media must not upload first.
    from event_media_policy import _MAX_SINGLE_CLIP_SECONDS
    return (type(duration) in (int, float) and 0 < duration <= _MAX_SINGLE_CLIP_SECONDS
            and math.isfinite(duration))


def event_thumbnail_allowed(path) -> bool:
    if current_edition() == "full_vms" or path is None:
        return True
    try:
        if path.suffix.lower() not in {".jpg", ".jpeg"}:
            return False
        with path.open("rb") as handle:
            return handle.read(3) == b"\xff\xd8\xff"
    except OSError:
        return False


def _capacity_status() -> dict:
    from vms_capacity import capacity_status
    return capacity_status()


def cloud_recording_page_html() -> str:
    """Gateway explanation replaces controls for the unavailable bulk uploader."""
    if current_edition() == "full_vms":
        return ""
    detail = ("Video stays on this computer. Cloud video uploads are disabled in Local Gateway."
              if current_edition() == "local_gateway" else
              "Cloud Gateway supports authorized event clips and their JPEG thumbnails. Continuous recording backups and live video uploads are unavailable. Event upload stays disabled until explicitly configured and entitled.")
    return ('<header class="topbar"><h1>Cloud video</h1></header><section class="panel">'
            f'<p>{detail}</p><p>Local recording, playback and retention keep their existing settings.</p>'
            '<a href="/analytics">View events</a></section>')


def edition_notice_html() -> str:
    edition = current_edition()
    if edition == "full_vms":
        return ""
    label = "Local Gateway" if edition == "local_gateway" else "Cloud Gateway"
    message = ("Video stays on this computer; cloud video uploads are disabled."
               if edition == "local_gateway" else
               "Event clip uploads require explicit configuration and an eligible plan. Continuous and live video uploads are disabled.")
    status = _capacity_status()
    quantity = status.get("camera_slot_quantity")
    licensed = status.get("state") == "licensed" and type(quantity) is int and quantity > 0
    activation = (f"Camera licence: {quantity} camera slots. Accepted capacity remains available offline."
                  if licensed else "Camera licence required. Link this computer online once, then verify its accepted camera capacity.")
    if edition == "cloud_gateway":
        message += (" Event upload is configured; each clip still requires camera authorization."
                    if video_transport_allowed("event") else " Event upload is currently disabled.")
    from product_mode import remote_access_authorized
    remote = ("Remote access is allowed by the last received cloud configuration. "
              "Subscription changes cannot be checked while offline."
              if remote_access_authorized() else
              "Remote access is unavailable under the current configuration.")
    if licensed:
        activation += (" Local recording, playback and Basic Motion notifications use this "
                       "licence and your permissions independently of remote access.")
    return (f'<section class="panel" aria-label="Gateway edition"><strong>{label}</strong>'
            f'<p>{message}</p><p>{activation}</p><p role="status">{remote}</p></section>')
