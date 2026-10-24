"""Load Ultralytics only with its outbound usage analytics disabled.

AnyAiCam uses Ultralytics for local inference, not cloud telemetry.  The
settings file must be prepared *before* importing Ultralytics because its
``Events`` singleton snapshots the ``sync`` setting during package import.
"""
from __future__ import annotations

import json
import os
import tempfile
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


def _prepare_settings_file(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise TelemetryOptOutError("Ultralytics config directory is a link")

    # LocalSystem must not consume a settings file planted through a
    # writable ProgramData descendant, junction, or symlink.
    if os.name == "nt":
        from platform_paths import windows_write_problem

        problem = windows_write_problem(directory)
        if problem:
            raise TelemetryOptOutError("Ultralytics config directory ACL is unsafe")

    # Ultralytics treats YOLO_CONFIG_DIR as a parent and appends its own
    # platform-independent "Ultralytics" subdirectory.
    settings_dir = directory / "Ultralytics"
    settings_dir.mkdir(parents=True, exist_ok=True)
    if settings_dir.is_symlink():
        raise TelemetryOptOutError("Ultralytics settings directory is a link")
    if os.name == "nt":
        from platform_paths import windows_write_problem

        if windows_write_problem(settings_dir):
            raise TelemetryOptOutError("Ultralytics settings directory ACL is unsafe")

    settings_file = settings_dir / "settings.json"
    if settings_file.is_symlink():
        raise TelemetryOptOutError("Ultralytics settings file is a link")
    if os.name == "nt" and settings_file.exists():
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
    settings["sync"] = False

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=settings_dir,
                                         prefix=".settings-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(settings, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, settings_file)
        if os.name == "nt":
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

    # Import only after the settings file is in place: Ultralytics creates its
    # telemetry singleton during import and otherwise snapshots sync=True.
    from ultralytics import YOLO, settings
    settings.update({"sync": False})
    if settings.get("sync") is not False:
        raise TelemetryOptOutError("Ultralytics telemetry setting did not remain disabled")
    from ultralytics.utils.events import events
    if getattr(events, "enabled", None) is not False:
        raise TelemetryOptOutError("Ultralytics telemetry sender is not disabled")
    return YOLO
