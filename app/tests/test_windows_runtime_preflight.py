"""Windows installer preflight must prepare the pinned Ultralytics settings path."""
import importlib.util
import json
import os
from pathlib import Path
from zipfile import ZipFile


def test_runtime_preflight_uses_full_settings_file_at_yolo_config_dir(tmp_path, monkeypatch):
    repo = Path(__file__).parents[2]
    preflight = repo / "installer" / "windows" / "runtime-preflight.py"
    spec = importlib.util.spec_from_file_location("anyaicam_runtime_preflight", preflight)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    wheel = repo / "installer" / "windows" / "vendor" / "wheels" / "ultralytics-8.3.40-py3-none-any.whl"
    with ZipFile(wheel) as archive:
        files = set(archive.namelist())
        library = archive.read("ultralytics/utils/__init__.py").decode("utf-8")
    assert "ultralytics/utils/events.py" not in files
    assert 'SETTINGS_FILE = USER_CONFIG_DIR / "settings.json"' in library
    assert "ultralytics.utils.events" not in Path(preflight).read_text(encoding="utf-8")

    config_root = tmp_path / "ultralytics-config"
    old_config = os.environ.get("YOLO_CONFIG_DIR")
    try:
        module.prepare_ultralytics_config(config_root)
        assert os.environ["YOLO_CONFIG_DIR"] == str(config_root)
        settings_file = config_root / "settings.json"
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
        assert settings["sync"] is False
        assert settings["settings_version"] == "0.0.6"
        assert len(settings) == 18
        assert not (config_root / "Ultralytics" / "settings.json").exists()
    finally:
        if old_config is None:
            os.environ.pop("YOLO_CONFIG_DIR", None)
        else:
            os.environ["YOLO_CONFIG_DIR"] = old_config
