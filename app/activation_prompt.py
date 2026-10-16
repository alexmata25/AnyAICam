"""Installed-VMS "Activate Your System" prompt (VMS 1.2.7, owner-approved flow 2026-10-16).

The approved purchase order for a customer who installs on their own PC is:
download -> install -> link this appliance (AnyAiCam Setup) -> Activate Your System -> Build Your System checkout
-> return to the already-installed VMS. The cloud refuses every other first purchase (per_camera_billing
.require_activated), so the installed VMS must offer the way forward itself.

The prompt shows only on an edge appliance that is linked to an account whose cloud authorization says it has no
camera plan and no VMS licence yet. "Unknown" (no fresh cloud configuration and no accepted signed capacity, e.g. right
after linking or while offline) never shows it: a paying customer must not be asked to buy again just because the
appliance has not heard from the cloud yet. Nothing here grants or changes capacity; it only reads.
"""
from __future__ import annotations

import logging
import os
from html import escape

logger = logging.getLogger("anyaicam.activation_prompt")

# The website Build Your System page. No account, appliance or customer identifier is ever put in the URL: the
# cloud re-derives everything from the signed-in session (contract with Green, PURCHASE-ACTIVATION-REVIEW §F).
ACTIVATE_URL = "https://anyaicam.com/build-your-system.html?source=installed"

NEEDS_ACTIVATION = "needs_activation"
ACTIVE = "active"
UNKNOWN = "unknown"
NOT_LINKED = "not_linked"
NOT_EDGE = "not_edge"


def _runtime_role() -> str:
    return os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower()


def _quantities(capacity) -> tuple[int, int] | None:
    if not isinstance(capacity, dict):
        return None
    try:
        plan = int(capacity.get("plan_camera_slots") or 0)
        licence = int(capacity.get("vms_license_capacity") or 0)
    except (TypeError, ValueError):
        return None
    return max(0, plan), max(0, licence)


def _state_from(capacity) -> str:
    quantities = _quantities(capacity)
    if quantities is None:
        return UNKNOWN
    plan, licence = quantities
    return NEEDS_ACTIVATION if plan == 0 and licence == 0 else ACTIVE


def activation_state() -> str:
    """not_edge | not_linked | needs_activation | active | unknown. Never raises."""
    try:
        if _runtime_role() not in {"edge", "combined"}:
            return NOT_EDGE
        import appliance_activation
        if appliance_activation.load_persisted_identity() is None:
            return NOT_LINKED
        # 1. The last authenticated configuration poll (at most 90 s old): the cloud's own current numbers.
        import appliance_config_cache
        configuration = appliance_config_cache.fresh()
        if isinstance(configuration, dict) and isinstance(configuration.get("usable_camera_capacity"), dict):
            return _state_from(configuration["usable_camera_capacity"])
        # 2. Otherwise the signed capacity this appliance last accepted (survives restarts and offline periods).
        import vms_capacity
        return _state_from(vms_capacity.load_capacity())
    except Exception:
        logger.exception("activation_state_failed")
        return UNKNOWN


def needs_activation() -> bool:
    return activation_state() == NEEDS_ACTIVATION


def banner_html(role: str) -> str:
    """The shared-shell banner. Owners get the button; other signed-in users are told who can activate."""
    if not needs_activation():
        return ""
    if str(role or "").strip().lower() == "customer_owner":
        action = (f'<a class="activate-system-button" id="activate-system-button" href="{escape(ACTIVATE_URL, quote=True)}" '
                  'target="_blank" rel="noopener">Activate Your System</a>')
        text = ("Your AnyAiCam VMS is installed and linked to your account. Choose your camera plan to start live "
                "view, recording and analytics. After checkout, return here: your cameras start automatically within "
                "a few minutes.")
    else:
        action = ""
        text = ("This AnyAiCam VMS is installed and linked, but the account has no camera plan yet. "
                "Ask the account owner to activate the system.")
    return (f'<style>{BANNER_CSS}</style>'
            '<div class="activate-system-banner" id="activate-system-banner" role="status">'
            f'<div><strong>Activate your system</strong><p>{escape(text)}</p></div>{action}</div>')


BANNER_CSS = (
    ".activate-system-banner{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px 18px;"
    "margin:0 0 16px;padding:14px 16px;border:1px solid rgba(56,189,248,.45);border-radius:12px;"
    "background:rgba(14,116,144,.18);color:#e0f2fe;font-size:13px;line-height:1.5}"
    ".activate-system-banner strong{display:block;font-size:15px;color:#fff;margin-bottom:2px}"
    ".activate-system-banner p{margin:0;max-width:62ch}"
    ".activate-system-button{display:inline-block;padding:10px 18px;border-radius:999px;background:#16778b;color:#fff;"
    "font-weight:800;text-decoration:none;white-space:nowrap}"
    ".activate-system-button:hover,.activate-system-button:focus-visible{background:#1b91a9;outline:2px solid #bae6fd;"
    "outline-offset:2px}"
)
