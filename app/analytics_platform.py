"""Which analytics this computer can actually run (Windows 1.3.2, Blue).

The Linux appliance is unchanged: unavailable_reason() is None for every analytic there, and each analytic's own
appliance-wide switch and engine checks apply exactly as before.

A native Windows install bundles the YOLO detector (app/yolov8n.pt), OpenCV and torch, but not the Tesseract OCR
engine or the PPE model, and its appliance-wide switches come from vms.env (install-runtime.ps1). On Windows an
analytic whose engine or switch is missing is reported unavailable with the reason: the Analytics Control Center
shows it as not available on this computer and refuses to turn it on, and main._analytic_globally_enabled never
runs it -- instead of a switch that shows "On" while nothing is processed.

Model locations are platform_paths.native_path() defaults: on Windows %ProgramData%\\AnyAiCam\\models\\..., which
only SYSTEM and Administrators can write, so an administrator who installs a model there makes the analytic
available, and a standard user cannot plant one.
"""
from __future__ import annotations

import os
import shutil

import platform_paths

PPE_MODEL_PATH = os.environ.get(
    "ANYAICAM_PPE_MODEL",
    platform_paths.native_path("/opt/anyaicam-ppe-model/yolov8n-ppe.pt", "models", "ppe", "yolov8n-ppe.pt"))

RULE_ANALYTICS = ("line_crossing", "intrusion", "loitering")


def _switch(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() == "true"


def ppe_switch_default() -> bool:
    """ANYAICAM_PPE_ENABLED when set; unset means on only when the PPE model is installed. The Linux image always
    bakes the model, so it stays on there; a Windows install has none, so PPE is off instead of trying to load a
    missing model on every frame."""
    value = os.environ.get("ANYAICAM_PPE_ENABLED", "").strip().lower()
    return value == "true" if value else os.path.isfile(PPE_MODEL_PATH)


def _tesseract_installed() -> bool:
    return shutil.which("tesseract") is not None


def unavailable_reason(analytic_key: str, *, windows: bool | None = None) -> str | None:
    """Why this analytic cannot run on this computer, or None when it can (always None on Linux)."""
    if not (platform_paths.WINDOWS if windows is None else windows):
        return None
    if analytic_key in ("smart_motion", *RULE_ANALYTICS) and not _switch("ANYAICAM_SMART_MOTION_ENABLED", "true"):
        return "Smart Motion is turned off on this computer."
    if analytic_key in RULE_ANALYTICS and not _switch("CUSTOMER_ANALYTICS_RULES_ENABLED", "false"):
        return "Rule analytics are turned off on this computer."
    if analytic_key == "people_counting" and not _switch("PEOPLE_COUNTING_ENABLED", "false"):
        return "People Counting is turned off on this computer."
    if analytic_key == "lpr":
        if not _tesseract_installed():
            return "License plate reading is not available on Windows: the Tesseract OCR engine is not installed."
        if not _switch("ANYAICAM_LPR_ENABLED", "false"):
            return "License plate reading is turned off on this computer."
    if analytic_key == "ppe":
        if not os.path.isfile(PPE_MODEL_PATH):
            return "PPE detection is not available on Windows: the PPE model is not installed."
        if not ppe_switch_default():
            return "PPE detection is turned off on this computer."
    if analytic_key == "facial_recognition" and not _switch("ANYAICAM_FACIAL_RECOGNITION_ENABLED", "false"):
        return "Facial recognition is off on this computer; an administrator must turn it on."
    return None


def runtime_supported(analytic_key: str) -> bool:
    return unavailable_reason(analytic_key) is None
