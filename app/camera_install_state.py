"""Customer-facing "installed/configured" state for one camera.

/customer-account's own gate (and its camera list) used to treat
cameras.status == 'configured' as the *sole* proxy for "this camera is
actually installed and working" -- but that column is only ever written
by the customer's own manual "Save camera setup" step in
/customer/setup (see partner_workspace.py's save_customer_cameras()).
An installer-provisioned deployment -- a partner/technician sets up the
appliance and cameras directly, the customer never opens their own
self-service wizard -- never sets that column, so a customer with real,
online, recording cameras was redirected to /customer/setup
indefinitely and every one of their real cameras showed "Pending
installation", regardless of the appliance's own live heartbeat
(POST /api/appliance/cameras, written to appliance_camera_status)
already reporting them online and recording.

This module is the single, shared decision for "is this camera
installed" (and, if so, what label to show) -- treating real
appliance-reported state as an independent, equally-valid signal
alongside the two the gate already trusted (cameras.status='configured'
and a cloud recordings-catalog row existing), not a replacement for
either. A customer who *did* complete the self-service wizard keeps
working exactly as before; an installer-provisioned customer whose
appliance is actually online now also shows correctly, without anyone
manually flipping cameras.status.

Pure, DB/FastAPI-free decision logic (fully unit-testable); DB-touching
callers build the plain booleans/strings this takes from their own
already-scoped SQL, matching camera_access.py's established
dependency-light pattern in this codebase.
"""
from __future__ import annotations


def camera_is_installed(
    *,
    camera_status: str | None,
    appliance_reported_online: bool,
    appliance_reported_recording: bool,
    has_cloud_recording: bool,
) -> bool:
    """True the moment ANY independent signal says this camera is real
    and working -- never only whether the customer manually completed
    /customer/setup:
      - cameras.status == 'configured' (the original, customer-wizard-
        driven signal -- kept exactly as before for backward
        compatibility; a customer who did complete setup keeps working
        unchanged).
      - the appliance's own live heartbeat currently reports this
        camera online or recording -- an installer-provisioned camera
        that is actually running is real regardless of whether the
        customer ever opened the wizard.
      - a cloud recordings-catalog row already exists for it (the
        original gate's other existing signal, kept as-is).
    Fails closed (False) only when none of these signals exist -- the
    one case "Pending installation" should still legitimately mean: a
    camera that has truly never been provisioned or ever reported in."""
    if camera_status == "configured":
        return True
    if appliance_reported_online or appliance_reported_recording:
        return True
    if has_cloud_recording:
        return True
    return False


def camera_status_label(
    *,
    camera_status: str | None,
    appliance_reported_online: bool,
    appliance_reported_recording: bool,
    has_cloud_recording: bool,
) -> str:
    """The customer-facing label for one camera card -- always derived
    from the exact same real-time signals camera_is_installed() checks,
    so the two can never disagree (a camera counted as installed never
    displays "Pending installation", and vice versa). Prefers the most
    current, most specific signal: live appliance-reported state beats
    a possibly-stale cameras.status column, which beats "we've at least
    seen recorded footage before", which beats the honest fallback."""
    if appliance_reported_online and appliance_reported_recording:
        return "Online · Recording"
    if appliance_reported_online:
        return "Online"
    if appliance_reported_recording:
        return "Recording"
    if camera_status == "configured":
        return "Configured"
    if has_cloud_recording:
        return "Recorded footage available"
    return "Pending installation"
