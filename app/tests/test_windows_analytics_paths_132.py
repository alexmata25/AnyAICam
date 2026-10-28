"""Windows 1.3.2 (Blue): protected state/model locations and platform-aware analytics availability.

1. Every state or model file default that was a Linux absolute path (/opt/..., /etc/..., /app/models) meant
   C:\\opt\\... on Windows, where any signed-in user may create the folder; the VMS runs as LocalSystem and unpickles
   the PPE/LPR model files (ultralytics YOLO). platform_paths.native_path() keeps the Linux path unchanged and puts the
   Windows default under %ProgramData%\\AnyAiCam (SYSTEM/Administrators only).
2. analytics_platform reports an analytic this Windows computer cannot run (Tesseract, PPE model, switches); the
   Analytics Control Center never shows it as on, refuses to turn it on or assign a seat to it, and the runtime gate
   in main never runs it. On Linux every analytic stays available (unchanged).
"""
import ast
import os
from pathlib import Path

import pytest

import analytics_control_center as acc
import analytics_platform
import platform_paths
from test_analytics_control_center import _call, _cookie, _module_db_path, client, db_path  # noqa: F401 -- fixtures

APP = Path(__file__).resolve().parents[1]
PREFIXES = ("/opt/anyaicam", "/etc/anyaicam", "/app/models")
# Literal Linux paths that stay outside native_path on purpose, each with its reason.
ALLOWED = {
    ("platform_paths.py", "/etc/anyaicam-update"): "the Linux trust directory itself (Windows: UPDATE_DIR branch)",
    ("platform_paths.py", "/opt/anyaicam/data/config"): "the Linux config directory itself (Windows: CONFIG_DIR branch)",
    ("webrtc_publisher.py", "/opt/anyaicam/mediamtx/mediamtx"): "service-launcher.ps1 always sets ANYAICAM_MEDIAMTX_BINARY",
}


# ---------------------------------------------------------------- native_path
def test_native_path_keeps_linux_unchanged_and_roots_windows_in_the_protected_data_folder(tmp_path):
    assert platform_paths.native_path("/opt/anyaicam-ppe-model/x.pt", "models", "ppe", "x.pt", windows=False) == "/opt/anyaicam-ppe-model/x.pt"
    root = tmp_path / "ProgramData" / "AnyAiCam"
    assert Path(platform_paths.native_path("/opt/anyaicam-ppe-model/x.pt", "models", "ppe", "x.pt", windows=True, root=root)) \
        == root / "models" / "ppe" / "x.pt"


def _linux_path_literals():
    """(file, literal, inside native_path) for every string literal starting with a Linux state/model prefix."""
    found = []
    for path in sorted(APP.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith(PREFIXES)):
                continue
            parent = parents.get(node)
            if isinstance(parent, ast.Expr):
                continue  # a docstring
            wrapped = (isinstance(parent, ast.Call) and parent.args and parent.args[0] is node
                       and isinstance(parent.func, ast.Attribute) and parent.func.attr == "native_path")
            found.append((path.name, node.value, wrapped))
    return found


def test_every_linux_state_or_model_default_goes_through_native_path():
    unwrapped = [(name, value) for name, value, wrapped in _linux_path_literals()
                 if not wrapped and (name, value) not in ALLOWED]
    assert unwrapped == [], "a Linux absolute path default would mean C:\\... on Windows; use platform_paths.native_path"


def test_the_known_state_and_model_files_are_routed():
    routed = {(name, value) for name, value, wrapped in _linux_path_literals() if wrapped}
    for item in [("analytics_sync.py", "/opt/anyaicam/data/config/analytics_sync_state.json"),
                 ("appliance_entitlements.py", "/opt/anyaicam/data/config/feature_entitlements.json"),
                 ("event_media_outbox.py", "/opt/anyaicam/data/config/event_media_outbox.json"),
                 ("appliance_media_fetch.py", "/etc/anyaicam/media_fetch_secret.json"),
                 ("analytics_platform.py", "/opt/anyaicam-ppe-model/yolov8n-ppe.pt"),
                 ("lpr.py", "/opt/anyaicam-lpr-model/license-plate-finetune-v1n.pt"),
                 ("aac_voice_call_listen.py", "/opt/anyaicam-stt-model/vosk-model-small-en-us-0.15"),
                 ("facial_engine_onnx.py", "/opt/anyaicam-aac-models"),
                 ("facial_engine_onnx.py", "/app/models/aac")]:
        assert item in routed


@pytest.mark.skipif(os.name != "nt", reason="needs Windows")
def test_windows_defaults_live_under_programdata_anyaicam(monkeypatch):
    import importlib

    import event_media_outbox
    monkeypatch.delenv("ANYAICAM_EVENT_MEDIA_OUTBOX_FILE", raising=False)
    monkeypatch.delenv("ANYAICAM_PPE_MODEL", raising=False)
    root = platform_paths._windows_program_data() / "AnyAiCam"
    try:
        assert importlib.reload(event_media_outbox).OUTBOX_FILE == root / "config" / "event_media_outbox.json"
        assert Path(importlib.reload(analytics_platform).PPE_MODEL_PATH) == root / "models" / "ppe" / "yolov8n-ppe.pt"
    finally:
        importlib.reload(event_media_outbox)
        importlib.reload(analytics_platform)


# ---------------------------------------------------------------- analytics_platform
ALL = tuple(acc.ANALYTICS)


def test_linux_reports_every_analytic_available(monkeypatch):
    for name in ("ANYAICAM_SMART_MOTION_ENABLED", "PEOPLE_COUNTING_ENABLED", "CUSTOMER_ANALYTICS_RULES_ENABLED",
                 "ANYAICAM_LPR_ENABLED", "ANYAICAM_PPE_ENABLED", "ANYAICAM_FACIAL_RECOGNITION_ENABLED"):
        monkeypatch.setenv(name, "false")
    assert [analytics_platform.unavailable_reason(key, windows=False) for key in ALL] == [None] * len(ALL)


@pytest.fixture()
def windows_host(monkeypatch, tmp_path):
    """A Windows install as 1.3.2 sets it up: installer analytics defaults, no Tesseract, no PPE model."""
    for name in ("ANYAICAM_SMART_MOTION_ENABLED", "ANYAICAM_LPR_ENABLED", "ANYAICAM_PPE_ENABLED",
                 "ANYAICAM_FACIAL_RECOGNITION_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PEOPLE_COUNTING_ENABLED", "true")
    monkeypatch.setenv("CUSTOMER_ANALYTICS_RULES_ENABLED", "true")
    monkeypatch.setattr(analytics_platform.shutil, "which", lambda name: None)
    monkeypatch.setattr(analytics_platform, "PPE_MODEL_PATH", str(tmp_path / "models" / "ppe" / "yolov8n-ppe.pt"))
    monkeypatch.setattr(analytics_platform, "LPR_PLATE_MODEL_PATH", str(tmp_path / "models" / "lpr" / "plate.pt"))
    return tmp_path


def _reasons():
    return {key: analytics_platform.unavailable_reason(key, windows=True) for key in ALL}


def test_windows_runs_the_bundled_yolo_analytics_and_reports_the_rest(windows_host):
    reasons = _reasons()
    for key in ("smart_motion", "people_counting", "line_crossing", "intrusion", "loitering"):
        assert reasons[key] is None, key
    assert "Tesseract OCR engine is not installed" in reasons["lpr"]
    assert "PPE model is not installed" in reasons["ppe"]
    assert "administrator must turn it on" in reasons["facial_recognition"]


def test_windows_switches_and_installed_engines_are_honoured(windows_host, monkeypatch):
    monkeypatch.setenv("PEOPLE_COUNTING_ENABLED", "false")
    monkeypatch.setenv("CUSTOMER_ANALYTICS_RULES_ENABLED", "false")
    reasons = _reasons()
    assert "People Counting is turned off" in reasons["people_counting"]
    assert all("Rule analytics are turned off" in reasons[key] for key in ("line_crossing", "intrusion", "loitering"))
    monkeypatch.setenv("ANYAICAM_SMART_MOTION_ENABLED", "false")
    assert "Smart Motion is turned off" in analytics_platform.unavailable_reason("intrusion", windows=True)
    # An administrator installs Tesseract and turns LPR on; installs the PPE model (PPE then defaults on); opts in to
    # facial recognition.
    monkeypatch.setattr(analytics_platform.shutil, "which", lambda name: r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    assert "license plate model is not installed" in analytics_platform.unavailable_reason("lpr", windows=True)
    plate = Path(analytics_platform.LPR_PLATE_MODEL_PATH)  # 1.3.4: LPR also needs its plate model
    plate.parent.mkdir(parents=True)
    plate.write_bytes(b"model")
    assert "turned off" in analytics_platform.unavailable_reason("lpr", windows=True)
    monkeypatch.setenv("ANYAICAM_LPR_ENABLED", "true")
    model = Path(analytics_platform.PPE_MODEL_PATH)
    model.parent.mkdir(parents=True)
    model.write_bytes(b"model")
    monkeypatch.setenv("ANYAICAM_FACIAL_RECOGNITION_ENABLED", "true")
    assert [analytics_platform.unavailable_reason(key, windows=True) for key in ("lpr", "ppe", "facial_recognition")] == [None] * 3
    monkeypatch.setenv("ANYAICAM_PPE_ENABLED", "false")
    assert "PPE detection is turned off" in analytics_platform.unavailable_reason("ppe", windows=True)


def test_ppe_defaults_off_without_its_model_and_an_explicit_switch_wins(windows_host, monkeypatch):
    assert analytics_platform.ppe_switch_default() is False             # Windows: no model shipped
    Path(analytics_platform.PPE_MODEL_PATH).parent.mkdir(parents=True)
    Path(analytics_platform.PPE_MODEL_PATH).write_bytes(b"model")
    assert analytics_platform.ppe_switch_default() is True              # the Linux image always bakes the model
    monkeypatch.setenv("ANYAICAM_PPE_ENABLED", "false")
    assert analytics_platform.ppe_switch_default() is False
    monkeypatch.setenv("ANYAICAM_PPE_ENABLED", "true")
    Path(analytics_platform.PPE_MODEL_PATH).unlink()
    assert analytics_platform.ppe_switch_default() is True              # explicit true is never second-guessed


# ---------------------------------------------------------------- Analytics Control Center
@pytest.fixture()
def lpr_unavailable(monkeypatch):
    reason = "License plate reading is not available on Windows: the Tesseract OCR engine is not installed."
    monkeypatch.setattr(acc.analytics_platform, "unavailable_reason", lambda key: reason if key in ("lpr", "ppe") else None)
    return reason


def _controls(app):
    response = _call(app, "GET", "/api/customer/analytics-control/cameras/cam-1", cookies=_cookie())
    assert response.status_code == 200
    return {item["key"]: item for item in response.json()["analytics"]}


def test_linux_controls_report_every_analytic_available(client):
    if platform_paths.WINDOWS:
        pytest.skip("Linux layout")
    assert all(item["runtime_available"] and item["runtime_unavailable_reason"] is None for item in _controls(client).values())


def test_an_unavailable_analytic_is_never_shown_on_and_cannot_be_turned_on_or_assigned(client, lpr_unavailable):
    controls = _controls(client)
    lpr = controls["lpr"]
    assert lpr["assigned"] is True                                      # the purchased seat is kept
    assert (lpr["enabled"], lpr["can_enable"], lpr["runtime_available"]) == (False, False, False)
    assert (lpr["operational_status"], lpr["runtime_unavailable_reason"]) == ("unavailable", lpr_unavailable)
    assert controls["ppe"]["can_assign"] is False                       # no new seat for an analytic that cannot run
    assert controls["smart_motion"]["runtime_available"] is True
    on = _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-1/lpr", cookies=_cookie(), payload={"enabled": True})
    assert (on.status_code, on.json()["detail"]) == (409, lpr_unavailable)
    off = _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-1/lpr", cookies=_cookie(), payload={"enabled": False})
    assert off.status_code == 200                                       # turning it off is always allowed
    assign = _call(client, "PUT", "/api/customer/analytics-control/cameras/cam-1/ppe/assignment", cookies=_cookie(),
                   payload={"assigned": True})
    assert (assign.status_code, assign.json()["detail"]) == (409, lpr_unavailable)


def test_the_page_says_not_available_on_this_computer(client):
    page = _call(client, "GET", "/analytics-control-center", cookies=_cookie())
    assert "a.runtime_available===false?'Not available on this computer: '+esc(a.runtime_unavailable_reason)" in page.text


# ---------------------------------------------------------------- runtime gate (main)
def test_the_runtime_gate_never_runs_an_unavailable_analytic(monkeypatch):
    pytest.importorskip("ultralytics")
    import main
    monkeypatch.setattr(main.smart_motion, "SMART_MOTION_ENABLED", True)
    assert main._analytic_globally_enabled("smart_motion_enabled", 1) is True
    monkeypatch.setattr(main.analytics_platform, "unavailable_reason", lambda key: "missing" if key == "smart_motion" else None)
    assert main._analytic_globally_enabled("smart_motion_enabled", 1) is False
    assert main.effective_analytics({"smart_motion_enabled": 1}, 1) == []
