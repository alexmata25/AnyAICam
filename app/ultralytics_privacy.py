"""Load Ultralytics only with its outbound usage analytics disabled.

AnyAiCam uses Ultralytics for local inference, not cloud telemetry.  The
settings file must be prepared *before* importing Ultralytics because its
telemetry setup reads ``sync`` during package import.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any


class TelemetryOptOutError(ImportError):
    """Raised when the local telemetry opt-out cannot be verified."""


def _settings_path() -> Path:
    # This location is inside the installer-protected application config
    # root (ProgramData on Windows; persistent appliance config on Linux).
    from platform_paths import native_path

    return Path(native_path("/opt/anyaicam/data/config/ultralytics",
                            "config", "ultralytics"))


def _settings_defaults(directory: Path) -> dict[str, Any]:
    """Return the complete settings schema required by pinned Ultralytics 8.3.40.

    Ultralytics validates the full settings key set during import. If a
    partial file is supplied, it resets to defaults, including ``sync=True``.
    Prepare the complete schema before its telemetry singleton is initialized.
    """
    root = directory.resolve()
    return {
        "settings_version": "0.0.6",
        "datasets_dir": str(root / "datasets"),
        "weights_dir": str(root / "weights"),
        "runs_dir": str(root / "runs"),
        "uuid": hashlib.sha256(str(uuid.getnode()).encode()).hexdigest(),
        "sync": False,
        "api_key": "",
        "openai_api_key": "",
        "clearml": True,
        "comet": True,
        "dvc": True,
        "hub": True,
        "mlflow": True,
        "neptune": True,
        "raytune": True,
        "tensorboard": True,
        "wandb": False,
        "vscode_msg": True,
    }


def _prepare_settings_file(directory: Path, *, verify_acl: bool = True) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise TelemetryOptOutError("Ultralytics config directory is a link")

    # LocalSystem must not consume a settings file planted through a
    # writable ProgramData descendant, junction, or symlink.
    if verify_acl and os.name == "nt":
        from platform_paths import windows_write_problem

        problem = windows_write_problem(directory)
        if problem:
            raise TelemetryOptOutError("Ultralytics config directory ACL is unsafe")

    # Ultralytics 8.3.40 reads settings.json directly from YOLO_CONFIG_DIR.
    settings_file = directory / "settings.json"
    if settings_file.is_symlink():
        raise TelemetryOptOutError("Ultralytics settings file is a link")
    if verify_acl and os.name == "nt" and settings_file.exists():
        from platform_paths import windows_write_problem

        if windows_write_problem(settings_file):
            raise TelemetryOptOutError("Ultralytics settings file ACL is unsafe")
    if settings_file.exists():
        try:
            settings: Any = json.loads(settings_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TelemetryOptOutError("Ultralytics settings file is unreadable") from exc
        if not isinstance(settings, dict):
            raise TelemetryOptOutError("Ultralytics settings file is malformed")
    else:
        settings = {}
    defaults = _settings_defaults(directory)
    prepared = defaults.copy()
    for key, value in settings.items():
        if key in defaults and type(value) is type(defaults[key]):
            prepared[key] = value
    prepared["sync"] = False

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".settings-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(prepared, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, settings_file)
        if verify_acl and os.name == "nt":
            from platform_paths import windows_write_problem

            if windows_write_problem(settings_file):
                raise TelemetryOptOutError("Ultralytics settings file ACL is unsafe")
    except OSError as exc:
        raise TelemetryOptOutError("Ultralytics telemetry opt-out could not be persisted") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return settings_file


def load_yolo_class():
    """Return Ultralytics YOLO only after opt-out is persisted and verified.

    Failure is fail-closed: callers disable that optional AI model while the
    rest of VMS recording, playback, and live view remain available.
    """
    directory = _settings_path()
    _prepare_settings_file(directory)
    os.environ["YOLO_CONFIG_DIR"] = str(directory)

    # Import only after the complete settings file is in place: Ultralytics
    # configures telemetry during import from this setting.
    from ultralytics import YOLO, settings
    settings.update({"sync": False})
    if settings.get("sync") is not False:
        raise TelemetryOptOutError("Ultralytics telemetry setting did not remain disabled")
    return YOLO
