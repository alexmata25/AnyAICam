"""AnyAiCam must opt out before Ultralytics snapshots telemetry settings."""
import ast
import builtins
import json
import os
import socket
import sys
import types
import urllib.request

import pytest

import ultralytics_privacy as privacy


def _fake_ultralytics(monkeypatch, settings_dir, outbound):
    settings_file = settings_dir / "settings.json"

    def read_settings():
        return json.loads(settings_file.read_text(encoding="utf-8"))

    def update_settings(values):
        settings = read_settings()
        settings.update(values)
        settings_file.write_text(json.dumps(settings), encoding="utf-8")

    class YOLO:
        def __init__(self, *_args, **_kwargs):
            # Model initialization follows the library's sync setting gate.
            if read_settings()["sync"]:
                outbound.append(True)
                urllib.request.urlopen("https://telemetry.invalid")

    ultralytics = types.ModuleType("ultralytics")
    ultralytics.__path__ = []
    ultralytics.YOLO = YOLO
    ultralytics.settings = types.SimpleNamespace(
        get=lambda key: read_settings().get(key),
        update=update_settings,
    )
    monkeypatch.setitem(sys.modules, "ultralytics", ultralytics)
    return ultralytics.settings


def test_opt_out_is_written_before_import_and_model_use_has_no_outbound_calls(tmp_path, monkeypatch):
    config_dir = tmp_path / "ultralytics"
    config_dir.mkdir()
    settings_file = config_dir / "settings.json"
    settings_file.write_text(json.dumps({"sync": True, "custom_setting": "keep"}), encoding="utf-8")
    monkeypatch.setattr(privacy, "_settings_path", lambda: config_dir)
    # This test uses a user-owned temp directory; production Windows paths are
    # checked against the real ProgramData ACL by platform_paths.
    import platform_paths
    monkeypatch.setattr(platform_paths, "windows_write_problem", lambda _path: "")
    outbound = []
    settings = _fake_ultralytics(monkeypatch, config_dir, outbound)
    real_import = builtins.__import__

    def checked_import(name, *args, **kwargs):
        if name == "ultralytics":
            # The complete file must exist at the path the pinned package
            # reads before its telemetry setup runs.
            assert os.environ["YOLO_CONFIG_DIR"] == str(config_dir)
            assert json.loads(settings_file.read_text(encoding="utf-8"))["sync"] is False
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked_import)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: outbound.append(True))
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: outbound.append(True))

    model = privacy.load_yolo_class()("fixture.pt")

    assert isinstance(model, object)
    persisted = json.loads(settings_file.read_text(encoding="utf-8"))
    assert persisted["sync"] is False
    assert persisted["settings_version"] == "0.0.6"
    assert len(persisted) == 18
    assert "custom_setting" not in persisted
    assert not (config_dir / "Ultralytics" / "settings.json").exists()
    assert settings.get("sync") is False
    assert outbound == []


def test_malformed_settings_fail_closed_before_ultralytics_import(tmp_path, monkeypatch):
    config_dir = tmp_path / "ultralytics"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(privacy, "_settings_path", lambda: config_dir)
    monkeypatch.setitem(sys.modules, "ultralytics", None)

    with pytest.raises(privacy.TelemetryOptOutError):
        privacy.load_yolo_class()


def test_sender_that_ignores_the_opt_out_fails_closed(tmp_path, monkeypatch):
    config_dir = tmp_path / "ultralytics"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(json.dumps({"sync": True}), encoding="utf-8")
    monkeypatch.setattr(privacy, "_settings_path", lambda: config_dir)
    import platform_paths
    monkeypatch.setattr(platform_paths, "windows_write_problem", lambda _path: "")
    _fake_ultralytics(monkeypatch, config_dir, [])
    settings = sys.modules["ultralytics"].settings
    monkeypatch.setattr(settings, "get", lambda _key: True)
    monkeypatch.setattr(settings, "update", lambda _values: None)

    with pytest.raises(privacy.TelemetryOptOutError, match="did not remain disabled"):
        privacy.load_yolo_class()


def test_inference_entry_points_use_guarded_loader():
    for relative in ("main.py", "ppe.py", "lpr.py"):
        source = (privacy.Path(__file__).parents[1] / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        direct_yolo_imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                               and node.module == "ultralytics" and any(alias.name == "YOLO" for alias in node.names)]
        assert not direct_yolo_imports, relative
        assert "load_yolo_class" in source, relative
