"""Local vs Hybrid product mode: one authoritative source for every
edge-side flag that decides whether this appliance talks to the cloud
for a given feature, replacing manual per-flag configuration with a
single mode selection while preserving each flag as an explicit
override where one is genuinely needed.

Design goal (2026-09-21): Local should disable cloud-dependent services
by default while preserving local recording, playback, analytics, live
view, and local licensing -- none of which read any flag this module
governs, so they are unaffected by construction, not by special-casing
here. Hybrid should enable cloud identity/services, remote access,
notifications, event-media upload, analytics sync, and relay/P2P.

Resolution order for the mode itself (current_mode()):
1. ANYAICAM_PRODUCT_MODE env var -- an explicit installer/manual
   selection. This is also the only mechanism for a genuinely
   air-gapped Local install that never reaches the cloud at all to
   learn a mode any other way.
2. The persisted state file -- the mode this appliance last learned
   from the cloud's own GET /api/appliance/configuration response
   (see appliance_cloud.appliance_configuration()'s `product_mode`
   field and customer_entitlements.product_mode_for_customer()),
   written by edge_camera_sync.py. Survives restarts and momentary
   cloud unreachability, and is what makes an upgrade purchase (Local
   -> Hybrid) or a subscription cancellation (Hybrid -> Local) apply
   automatically the next time this appliance polls its configuration,
   with no manual re-flagging.
3. "" (unset/legacy) -- no mode has ever been configured for this
   appliance. Every governed flag then falls back to its own
   pre-product-mode literal default (see resolve_cloud_flag()'s
   legacy_default), so an appliance that has never opted into product
   mode behaves exactly as it did before this module existed. This is
   what keeps introducing this module a zero-behavior-change event for
   every already-deployed appliance, including the current Ryzen
   pilot unit, until it is explicitly migrated.

Deliberately NOT covered by this module (by design, not oversight):
- Local recording, playback, live view (LAN), and on-device analytics
  processing itself -- these have no cloud-dependency flag at all
  today; they simply run. Product mode has nothing to gate here.
- Local storage management (ANYAICAM_LOCAL_STORAGE_MANAGEMENT_ENABLED/
  ANYAICAM_LOCAL_STORAGE_AUTO_DELETE_ENABLED) -- purely local disk
  housekeeping, not a cloud dependency in either direction.
- Per-analytic feature entitlement (ANYAICAM_LPR_ENABLED,
  ANYAICAM_FACIAL_RECOGNITION_ENABLED, ANYAICAM_FACIAL_ACCESS_CONTROL_
  ENABLED) -- these gate WHICH paid analytics/add-ons this customer's
  entitlements include (see analytics_entitlements.py), independent of
  Local vs Hybrid camera-slot tier. A Local customer can and should be
  able to buy Face Access without that purchase implying Hybrid.
- Cloud-side platform kill-switches -- appliance_cloud.py's own module-
  level flags of the same names gate whether the CLOUD ACCEPTS a given
  kind of request from ANY appliance (a platform-wide setting for a
  multi-tenant cloud deployment), not one customer's product mode.
  Those are intentionally left reading the raw env var directly.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("anyaicam.product_mode")

PRODUCT_MODE_ENV = "ANYAICAM_PRODUCT_MODE"
VALID_MODES = ("local", "hybrid")

STATE_FILE = Path(os.environ.get(
    "ANYAICAM_PRODUCT_MODE_STATE_FILE",
    "/opt/anyaicam/data/config/product_mode.json",
))


def _read_persisted_mode() -> str:
    try:
        data = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return ""
    mode = str(data.get("mode") or "").strip().lower()
    return mode if mode in VALID_MODES else ""


def current_mode() -> str:
    """See module docstring for the full 3-step resolution order."""
    env_mode = os.environ.get(PRODUCT_MODE_ENV, "").strip().lower()
    if env_mode in VALID_MODES:
        return env_mode
    return _read_persisted_mode()


def is_local() -> bool:
    return current_mode() == "local"


def is_hybrid() -> bool:
    return current_mode() == "hybrid"


def persist_mode(mode: str) -> bool:
    """Called by edge_camera_sync.py when the cloud's own
    GET /api/appliance/configuration response reports a product_mode
    different from what this appliance already has persisted -- e.g.
    right after a Hybrid upgrade purchase completes, or after a Hybrid
    subscription is cancelled. Returns True only when the persisted
    mode actually changed, so the caller can log/surface a "restart
    required" signal: every flag resolve_cloud_flag() governs is still
    read once at process start, exactly as each was before this module
    existed, so a mode change only takes visible effect on the next
    restart of this process -- this function never restarts anything
    itself.

    Never meaningfully overrides an explicit ANYAICAM_PRODUCT_MODE env
    var -- current_mode() above always prefers that env var first, so
    persisting a cloud-reported mode underneath it has no visible
    effect until the env var override is removed. This is deliberate:
    an explicit install-time or air-gapped choice always wins over
    whatever the cloud thinks this customer's entitlement implies.
    """
    mode = (mode or "").strip().lower()
    if mode not in VALID_MODES:
        return False
    previous = _read_persisted_mode()
    if previous == mode:
        return False
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"mode": mode}))
    logger.warning(
        "product_mode.changed previous=%s new=%s restart_required=true",
        previous or "unset", mode,
    )
    return True


def resolve_cloud_flag(env_var: str, *, legacy_default: bool = False) -> bool:
    """The one function every cloud-dependent edge-worker flag this
    module governs (see FLAG_REGISTRY below) reads instead of a raw
    os.environ lookup. An explicitly-set env var always wins, exactly
    reproducing this codebase's behavior before product modes existed
    -- this only supplies a DEFAULT for a flag nobody set explicitly.
    Local mode defaults every governed flag OFF (no required cloud
    dependency for normal operation); Hybrid mode defaults every
    governed flag ON. No mode configured falls back to legacy_default,
    this flag's own original hardcoded literal (every one of them was
    "false" before this module existed)."""
    raw = os.environ.get(env_var)
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() == "true"
    mode = current_mode()
    if mode == "local":
        return False
    if mode == "hybrid":
        return True
    return legacy_default


# Documents every flag resolve_cloud_flag() governs -- for introspection
# (an admin-facing status page, this feature's own migration doc) only;
# resolve_cloud_flag() itself never consults this, it only ever needs
# the one env_var name passed to it at each call site.
#
# hot_reloadable is False for all six, uniformly, for the same
# structural reason: each is read into a plain module-level constant
# exactly ONCE at import time (e.g. analytics_sync.ANALYTICS_SYNC_
# ENABLED = product_mode.resolve_cloud_flag(...)), and main.py's own
# app-startup event decides whether to asyncio.create_task() that
# module's worker AT ALL based on the constant's value at THAT moment
# -- there is no code path anywhere in these six modules that re-reads
# the flag, or re-evaluates whether to keep running, after process
# start. A value change (whether from an explicit env var edit or a
# persisted product-mode transition) is structurally invisible to an
# already-running process; the only way to apply it is a restart of
# the anyaicam-vms process (see apply_needs_restart() / restart_vms
# below), never a lighter in-process reload. This is a genuine
# constraint of the current implementation, not a design choice this
# module made -- making any of these six truly hot-reloadable would
# mean changing each worker's own loop to re-check the flag on every
# iteration instead of once at task-creation time, which is out of
# this module's scope.
FLAG_REGISTRY: dict[str, dict] = {
    "ANYAICAM_ANALYTICS_SYNC_ENABLED": {
        "module": "analytics_sync",
        "description": "Sync local YOLO/motion-detection events to the cloud.",
        "hot_reloadable": False,
    },
    "ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED": {
        "module": "event_media_uploader",
        "description": "Upload motion-event thumbnail/clip media to the cloud.",
        "hot_reloadable": False,
    },
    "ANYAICAM_FACIAL_EMBEDDING_SYNC_ENABLED": {
        "module": "facial_embedding_sync",
        "description": "Sync facial-recognition embeddings with the cloud directory.",
        "hot_reloadable": False,
    },
    "ANYAICAM_LIVE_RELAY_ENABLED": {
        "module": "live_relay_uploader",
        "description": "Relay live HLS segments through cloud S3/CloudFront for remote viewing.",
        "hot_reloadable": False,
    },
    "ANYAICAM_RECORDING_UPLOAD_ENABLED": {
        "module": "recording_uploader",
        "description": "Upload the continuous/bulk recording archive to the cloud.",
        "hot_reloadable": False,
    },
    "ANYAICAM_LIVE_P2P_ENABLED": {
        "module": "live_view_p2p / webrtc_publisher",
        "description": "Cloud-brokered WebRTC signaling for remote peer-to-peer live view -- signaling only requires the cloud; once negotiated, media itself flows directly.",
        "hot_reloadable": False,
    },
}


def _default_for_mode(mode: str) -> bool:
    """Every governed flag's default under a REAL (non-empty) mode,
    ignoring any per-appliance env var override the cloud can never see
    -- used only by describe_transition() below to log/decide what a
    mode change would affect by default. "" (no mode / legacy) behaves
    identically to "local" for every flag registered above (both
    resolve_cloud_flag() branches return legacy_default=False, same as
    "local"'s own False) -- this is what makes a customer's first-ever
    purchase transitioning "" -> "local" correctly report no changed
    flags below, while "" -> "hybrid" correctly reports all six."""
    return mode == "hybrid"


def describe_transition(old_mode: str, new_mode: str) -> list[dict]:
    """Which governed flags would flip their DEFAULT between old_mode
    and new_mode, assuming neither has an explicit per-appliance env
    var override (the cloud has no visibility into an individual
    appliance's own environment, so this is what changed BY DEFAULT,
    not a guarantee of what will actually change on any specific real
    appliance -- an explicit override there always wins regardless, see
    resolve_cloud_flag()). Returns one dict per flag that actually
    flips: {"env_var", "module", "before", "after"}. Empty old_mode or
    new_mode is treated the same as "local" (see _default_for_mode()),
    so "" -> "local" (a customer's first Local purchase) correctly
    returns []. Used by appliance_cloud.appliance_configuration() to
    decide whether a reported product_mode change is worth logging and
    queuing a restart_vms command for -- an empty return means nothing
    would actually change, so no restart is queued."""
    changed = []
    before_all = _default_for_mode(old_mode)
    after_all = _default_for_mode(new_mode)
    if before_all == after_all:
        return changed
    for env_var, meta in FLAG_REGISTRY.items():
        changed.append({
            "env_var": env_var,
            "module": meta["module"],
            "before": before_all,
            "after": after_all,
        })
    return changed
