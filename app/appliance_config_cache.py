"""Shared, in-process copy of this appliance's latest GET /api/appliance/
configuration response (2026-09-24).

Why: every edge worker in the VMS process used to fetch the full
configuration independently -- edge_camera_sync (60s), webrtc_publisher
(60s), local_storage_manager (its policy on EVERY 60s scan), analytics_
sync / recording_uploader / talk_down_discovery (300s each), and
event_media_uploader on EVERY event it uploads (via recording_uploader.
_refresh_camera_map()). Measured on staging: ~15,600 authenticated
configuration requests/day from one appliance, each paying the cloud's
full appliance authentication (a PBKDF2 credential check) for a payload
that changes rarely.

How: edge_camera_sync.sync_provisioned_cameras() -- the worker that
already owns cloud->edge configuration delivery and fetches every
SYNC_INTERVAL_SECONDS -- publish()es each successful response here. Every
other worker asks fresh() first and only falls back to its own fetch when
nothing recent enough has been published (edge sync not running yet, not
activated, cloud unreachable, ...). Nothing about offline behavior
changes: a worker whose own fallback fetch fails still keeps whatever
state it had, exactly as before; this module never invents or extends a
response, it only lets a recent real one be reused.

Deliberately NOT a general HTTP cache: only edge_camera_sync publishes,
and readers never write, so a worker's own fetch can never overwrite
the authoritative copy and there is exactly one source of freshness.
"""
from __future__ import annotations

import copy
import os
import threading
import time

# How old a published response may be and still be reused. A little over
# edge_camera_sync's own 60s cadence, so a reader normally never falls
# back while edge sync is healthy, and a stalled edge sync is noticed
# (readers resume their own fetches) within ~1.5 intervals.
MAX_AGE_SECONDS = max(0.0, float(os.environ.get("ANYAICAM_APPLIANCE_CONFIG_CACHE_SECONDS", "90")))

_lock = threading.Lock()
_latest: dict | None = None
_published_at: float | None = None
_stats = {"published": 0, "hits": 0, "misses": 0}


def publish(configuration: dict, *, now: float | None = None) -> None:
    """Record a successful, well-formed configuration response."""
    global _latest, _published_at
    if not isinstance(configuration, dict):
        return
    with _lock:
        _latest = copy.deepcopy(configuration)
        _published_at = time.monotonic() if now is None else now
        _stats["published"] += 1


def fresh(*, max_age: float | None = None, now: float | None = None) -> dict | None:
    """A private copy of the latest published response if it is at most
    max_age (default MAX_AGE_SECONDS) old, else None."""
    limit = MAX_AGE_SECONDS if max_age is None else max_age
    current = time.monotonic() if now is None else now
    with _lock:
        if _latest is None or _published_at is None or limit <= 0 or current - _published_at > limit:
            _stats["misses"] += 1
            return None
        _stats["hits"] += 1
        return copy.deepcopy(_latest)


def get_or_fetch(fetch) -> dict | None:
    """fresh() if available, otherwise the caller's own fetch() result
    (which is NOT published -- see module docstring)."""
    cached = fresh()
    if cached is not None:
        return cached
    return fetch()


def stats() -> dict:
    with _lock:
        return dict(_stats, age_seconds=(None if _published_at is None else round(time.monotonic() - _published_at, 1)))


def reset() -> None:
    """Test/ops helper: forget the published copy."""
    global _latest, _published_at
    with _lock:
        _latest = None
        _published_at = None
        for key in _stats:
            _stats[key] = 0
