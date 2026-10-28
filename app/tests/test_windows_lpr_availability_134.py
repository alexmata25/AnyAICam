"""Windows 1.3.4 LPR parity (2026-10-27): no silent LPR degradation.

With Tesseract bundled (1.3.4) and ANYAICAM_LPR_ENABLED defaulted like Linux, LPR still needs the licence-plate
detection model. It is NOT bundled while the model licence (LIC-1) is open. Without it lpr._get_plate_model() fails
and detect_plate() quietly falls back to a Haar cascade that finds almost nothing on real footage, while the
Analytics Control Center would have shown LPR as available and On. So on Windows the plate model is part of LPR's
availability rule, like the PPE model is for PPE. Linux is unchanged (its image bakes the model).
"""
from pathlib import Path

import pytest

import analytics_platform
import lpr


@pytest.fixture()
def windows_lpr_host(monkeypatch, tmp_path):
    monkeypatch.setenv("ANYAICAM_LPR_ENABLED", "true")
    monkeypatch.setattr(analytics_platform.shutil, "which",
                        lambda name: r"C:\Program Files\AnyAiCam\runtime\tools\tesseract\tesseract.exe")
    model = tmp_path / "models" / "lpr" / "license-plate-finetune-v1n.pt"
    monkeypatch.setattr(analytics_platform, "LPR_PLATE_MODEL_PATH", str(model))
    return model


def test_without_the_plate_model_lpr_is_reported_unavailable(windows_lpr_host):
    reason = analytics_platform.unavailable_reason("lpr", windows=True)
    assert reason is not None and "license plate model is not installed" in reason


def test_with_engine_model_and_switch_lpr_is_available(windows_lpr_host):
    windows_lpr_host.parent.mkdir(parents=True)
    windows_lpr_host.write_bytes(b"model")
    assert analytics_platform.unavailable_reason("lpr", windows=True) is None


def test_the_engine_is_reported_before_the_model(windows_lpr_host, monkeypatch):
    monkeypatch.setattr(analytics_platform.shutil, "which", lambda name: None)
    assert "Tesseract OCR engine is not installed" in analytics_platform.unavailable_reason("lpr", windows=True)


def test_linux_is_unchanged(windows_lpr_host):
    assert analytics_platform.unavailable_reason("lpr", windows=False) is None


def test_the_rule_checks_the_same_file_lpr_loads(monkeypatch):
    monkeypatch.delenv("ANYAICAM_LPR_PLATE_MODEL", raising=False)
    import importlib

    fresh = importlib.reload(analytics_platform)
    try:
        assert Path(fresh.LPR_PLATE_MODEL_PATH) == Path(lpr.LPR_PLATE_MODEL_PATH)
    finally:
        importlib.reload(analytics_platform)
