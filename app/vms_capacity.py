"""Licensed camera capacity on the appliance (2026-10-01).

Claiming an appliance is open to any customer account; what the VMS may
actually run is not. The cloud sends this appliance's usable capacity --
the smaller of the customer's camera plan and their VMS software license
(customer_entitlements.usable_camera_capacity()) -- with every configuration
sync, and it is kept here so it keeps applying offline (Local mode).

The recording/live pipeline asks camera_licensed() through camera_url(), the
one place every camera worker resolves its camera:

- capacity known: only the first N provisioned camera numbers run;
- never synced but activated (claimed before this existed, or claimed
  moments ago): unchanged behaviour until the first sync arrives, so an
  upgrade never interrupts working cameras;
- never activated (a copied installer that was never claimed, even with
  legacy CAMERA<n>_HOST settings): nothing runs.

A cloud process runs no camera pipeline and is never restricted here.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("anyaicam.vms_capacity")

STATE_FILE = Path(os.environ.get("ANYAICAM_VMS_CAPACITY_FILE", "/opt/anyaicam/data/config/vms_capacity.json"))


def persist_capacity(breakdown: dict | None) -> bool:
    """Store the capacity the cloud sent; returns True when it changed.
    Ignores a configuration from an older cloud that sends none."""
    if not isinstance(breakdown, dict) or "camera_slot_quantity" not in breakdown:
        return False
    try:
        quantity = max(0, int(breakdown["camera_slot_quantity"]))
    except (TypeError, ValueError):
        return False
    record = {"camera_slot_quantity": quantity,
              "plan_camera_slots": breakdown.get("plan_camera_slots"),
              "vms_license_capacity": breakdown.get("vms_license_capacity")}
    previous = load_capacity()
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = STATE_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(record), encoding="utf-8")
        temporary.replace(STATE_FILE)
    except OSError as error:
        logger.warning("vms_capacity.persist_failed error=%s", type(error).__name__)
        return False
    return previous != record


def load_capacity() -> dict | None:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        int(data["camera_slot_quantity"])
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _cloud_runtime() -> bool:
    return os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower() == "cloud"


def _activated() -> bool:
    try:
        import appliance_activation
        return appliance_activation.active_appliance_id() is not None
    except Exception:
        return False


def licensed_camera_numbers(provisioned: list[int]) -> list[int] | None:
    """The provisioned camera numbers this appliance may run, or None for
    "no limit applies" (cloud, or activated but never synced)."""
    if _cloud_runtime():
        return None
    capacity = load_capacity()
    if capacity is None:
        return None if _activated() else []
    return sorted(provisioned)[: int(capacity["camera_slot_quantity"])]


def camera_licensed(camera_number: int, provisioned: list[int]) -> bool:
    allowed = licensed_camera_numbers(provisioned)
    return allowed is None or camera_number in allowed
