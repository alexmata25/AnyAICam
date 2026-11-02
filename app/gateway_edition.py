"""Edition defaults and video transport boundaries for the shared VMS.

Selecting an edition never changes recording, retention, analytics or licensing.
Existing installations without a selection retain Full VMS behavior.
"""
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


def edition_notice_html() -> str:
    edition = current_edition()
    if edition == "full_vms":
        return ""
    label = "Local Gateway" if edition == "local_gateway" else "Cloud Gateway"
    message = ("Video stays on this computer; cloud video uploads are disabled."
               if edition == "local_gateway" else
               "Event clip uploads require explicit configuration and an eligible plan. Continuous and live video uploads are disabled.")
    return f'<section class="panel" aria-label="Gateway edition"><strong>{label}</strong><p>{message}</p></section>'
