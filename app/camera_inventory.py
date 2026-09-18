"""Persistent, credential-free camera inventory for edge discovery.

Stage 1 scope: this store holds discovery/inventory data only — identity,
name, IP address, manufacturer/model, ONVIF/RTSP indicators, sanitized
stream URLs, open ports, online/offline state, and scan timestamps. It has
no concept of provisioning, camera numbering, health, or recording state —
those belong to a later, separately-approved stage.

Defense in depth, applied on every write, including to records already on
disk (so a field or URL that should never have been there does not survive
forever on a camera that goes offline and is never rediscovered):

1. An explicit field allowlist (DISCOVERED_FIELDS for freshly discovered
   records, DISCOVERED_FIELDS + GENERATED_FIELDS for anything reloaded from
   disk) drops unknown fields outright, not just credential-shaped ones.
2. stream_urls/onvif_xaddrs values are sanitized through the same
   userinfo-stripping URL parsers edge_discovery.py uses; invalid or
   wrong-scheme entries are dropped rather than persisted.
3. _without_credentials() scrubs any remaining credential-shaped key, both
   per-record and again on the full object just before it is written.

The store never accepts or exposes an RTSP/ONVIF username or password, and
there is no camera-configuration/credential store in this module by design
— see docs/CLAUDE_HANDOFF.md's secure-video boundary.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from edge_discovery import _safe_onvif_url, _safe_rtsp_url


INVENTORY_VERSION = 1
SENSITIVE_KEY_PARTS = ("username", "password", "credential", "secret", "token")

# Fields a discovery probe is allowed to contribute. Anything else on the
# incoming dict (Stage 4/5 concepts like provisioning_status, camera_number,
# rtsp_valid, onvif_valid, health_state, recording, fps, bitrate_bps included)
# is dropped, not just scrubbed for credentials.
DISCOVERED_FIELDS = (
    "id",
    "name",
    "ip_address",
    "ip",  # accepted for compatibility with the simpler v1.1.0 TCP probe
    "manufacturer",
    "model",
    "onvif",
    "rtsp",
    "onvif_xaddrs",
    "open_ports",
    "stream_urls",
    "stream_url_source",
    "stream_urls_verified",
)

# Fields this store generates itself on every reconcile().
GENERATED_FIELDS = (
    "discovered_name",
    "first_seen",
    "last_seen",
    "last_scan",
    "discovery_network",
    "status",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _without_credentials(value):
    if isinstance(value, dict):
        return {
            key: _without_credentials(item)
            for key, item in value.items()
            if not any(part in key.lower() for part in SENSITIVE_KEY_PARTS)
        }
    if isinstance(value, list):
        return [_without_credentials(item) for item in value]
    return value


def _sanitize_stream_urls(values) -> list[str]:
    if not isinstance(values, list):
        return []
    return [
        safe for value in values
        if isinstance(value, str) and (safe := _safe_rtsp_url(value))
    ]


def _sanitize_onvif_xaddrs(values) -> list[str]:
    if not isinstance(values, list):
        return []
    return [
        safe for value in values
        if isinstance(value, str) and (safe := _safe_onvif_url(value))
    ]


def _sanitize_record(record: dict, allowed_keys: tuple[str, ...]) -> dict:
    """Allowlist fields, sanitize URL values, then scrub credential-shaped keys."""
    filtered = {key: record[key] for key in allowed_keys if key in record}
    if "stream_urls" in filtered:
        filtered["stream_urls"] = _sanitize_stream_urls(filtered["stream_urls"])
    if "onvif_xaddrs" in filtered:
        filtered["onvif_xaddrs"] = _sanitize_onvif_xaddrs(filtered["onvif_xaddrs"])
    return _without_credentials(filtered)


class CameraInventoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("cameras"), list):
                return value
        except (OSError, json.JSONDecodeError):
            pass
        return {
            "version": INVENTORY_VERSION,
            "updated_at": None,
            "last_scan": None,
            "cameras": [],
        }

    def reconcile(self, network: str, discovered: list[dict]) -> dict:
        """Upsert this scan's discovery-only fields; mark missing devices offline.

        Raises ValueError if any discovered record has no 'id' — a missing id
        means the caller (probe code) has a bug, not that the record is safe
        to silently drop.
        """
        with self._lock:
            inventory = self.load()
            now = utc_now()
            existing = {
                str(camera.get("id")): _sanitize_record(camera, DISCOVERED_FIELDS + GENERATED_FIELDS)
                for camera in inventory.get("cameras", [])
                if camera.get("id")
            }
            seen: set[str] = set()
            for item in discovered:
                camera_id = item.get("id")
                if not camera_id:
                    raise ValueError("Discovered camera record is missing an 'id'.")
                camera_id = str(camera_id)

                safe = _sanitize_record(item, DISCOVERED_FIELDS)
                safe["id"] = camera_id
                seen.add(camera_id)

                previous = existing.get(camera_id, {})
                safe["discovered_name"] = safe.get("name")
                safe["first_seen"] = previous.get("first_seen") or now
                safe["last_seen"] = now
                safe["last_scan"] = now
                safe["discovery_network"] = network
                safe["status"] = "online"
                existing[camera_id] = safe

            for camera_id, camera in existing.items():
                if camera.get("discovery_network") == network and camera_id not in seen:
                    camera["status"] = "offline"
                    camera["last_scan"] = now

            cameras = sorted(
                existing.values(),
                key=lambda camera: (
                    str(camera.get("ip_address") or camera.get("ip") or ""),
                    str(camera.get("id", "")),
                ),
            )
            inventory = {
                "version": INVENTORY_VERSION,
                "updated_at": now,
                "last_scan": {
                    "network": network,
                    "completed_at": now,
                    "discovered": len(discovered),
                },
                "cameras": cameras,
            }
            self._save(inventory)
            return inventory

    def _save(self, inventory: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(_without_credentials(inventory), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.path)
