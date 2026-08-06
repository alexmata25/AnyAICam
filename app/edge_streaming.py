"""Edge-local RTSP and HLS health primitives.

This module deliberately has no AWS integration. Private camera connectivity and
media generation belong to the edge runtime; cloud deployments consume only the
metadata and artifacts explicitly relayed by the edge appliance.
"""

from __future__ import annotations

import socket
import time
from enum import Enum
from pathlib import Path


HLS_URL_PREFIX = "/static/hls"


class StreamStatus(str, Enum):
    CONNECTING = "connecting"
    LIVE = "live"
    OFFLINE = "offline"
    STALE = "stale"
    ERROR = "error"


def canonical_hls_url(camera_number: int) -> str:
    return f"{HLS_URL_PREFIX}/camera{camera_number}.m3u8"


def rtsp_endpoint_ready(host: str, port: int = 554, timeout: float = 2.0) -> tuple[bool, str]:
    """Return whether the edge can establish a TCP connection to the RTSP port."""
    if not host:
        return False, "camera host is not configured"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "rtsp endpoint accepted a connection"
    except OSError as error:
        return False, f"rtsp endpoint unavailable: {error}"


def playlist_snapshot(
    hls_folder: Path,
    camera_number: int,
    freshness_seconds: int,
    now: float | None = None,
) -> dict:
    """Inspect the manifest and newest referenced segment without reading media."""
    checked_at = time.time() if now is None else now
    manifest = hls_folder / f"camera{camera_number}.m3u8"
    snapshot = {
        "exists": False,
        "fresh": False,
        "manifest_age_seconds": None,
        "segment_age_seconds": None,
        "latest_segment": None,
    }
    if not manifest.exists():
        return snapshot

    try:
        manifest_age = max(0, round(checked_at - manifest.stat().st_mtime))
        snapshot.update(exists=True, manifest_age_seconds=manifest_age)
        segment_names = [
            line.strip()
            for line in manifest.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if segment_names:
            segment_name = Path(segment_names[-1]).name
            segment = hls_folder / segment_name
            snapshot["latest_segment"] = segment_name
            if segment.exists():
                snapshot["segment_age_seconds"] = max(
                    0, round(checked_at - segment.stat().st_mtime)
                )
        snapshot["fresh"] = (
            snapshot["manifest_age_seconds"] is not None
            and snapshot["segment_age_seconds"] is not None
            and snapshot["manifest_age_seconds"] <= freshness_seconds
            and snapshot["segment_age_seconds"] <= freshness_seconds
        )
    except OSError:
        snapshot["fresh"] = False
    return snapshot


def classify_stream(snapshot: dict, worker_state: str) -> StreamStatus:
    """Classify user-visible state; only fresh HLS output can be Live."""
    if snapshot.get("fresh"):
        return StreamStatus.LIVE
    if snapshot.get("exists"):
        return StreamStatus.STALE
    if worker_state in {"starting", "connecting", "running"}:
        return StreamStatus.CONNECTING
    if worker_state in {"offline", "retrying"}:
        return StreamStatus.OFFLINE
    return StreamStatus.ERROR


def remove_startup_manifests(hls_folder: Path) -> list[str]:
    """Remove manifests left by a previous appliance process.

    Segments are intentionally retained here. FFmpeg's normal delete-segments
    policy owns segment cleanup, while removing the manifests prevents clients
    from replaying an old playlist during appliance startup.
    """
    removed: list[str] = []
    for manifest in hls_folder.glob("camera*.m3u8"):
        try:
            manifest.unlink()
            removed.append(manifest.name)
        except OSError:
            continue
    return removed
