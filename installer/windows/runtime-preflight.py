"""AnyAiCam Windows runtime preflight (2026-10-07).

Run with the bundled Python after its packages are installed and before the
AnyAiCam service is installed (install-runtime.ps1), and again by the Sandbox
validation. It imports what app/main.py loads at startup -- including
cv2/ultralytics/torch, whose native DLLs need the Microsoft Visual C++
runtime -- and runs one torch operation. Exit 0 when everything loads; exit 1
naming every module that failed, so setup stops visibly instead of installing a
service that crashes on every start.
"""
import importlib
import os
import sys
import tempfile
from pathlib import Path

REQUIRED_MODULES = (
    "fastapi",
    "uvicorn",
    "numpy",
    "cv2",
    "torch",
    "torchvision",
    "ultralytics",
)


def check(modules=None):
    if modules is None:
        modules = REQUIRED_MODULES
    failures = []
    loaded = {}
    for name in modules:
        try:
            loaded[name] = importlib.import_module(name)
        except Exception as exc:  # OSError (DLL load) as well as ImportError
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    if "torch" in loaded:
        try:
            torch = loaded["torch"]
            if float(torch.ones(4).sum()) != 4.0:
                failures.append("torch: tensor check returned a wrong result")
        except Exception as exc:
            failures.append(f"torch: tensor check failed: {type(exc).__name__}: {exc}")
    if "ultralytics" in loaded:
        try:
            from ultralytics import YOLO  # noqa: F401  (the name app/main.py imports)
            from ultralytics import settings
            if settings.get("sync") is not False:
                failures.append("ultralytics: outbound telemetry is not disabled")
        except Exception as exc:
            failures.append(f"ultralytics.YOLO: {type(exc).__name__}: {exc}")
    return failures


def prepare_ultralytics_config(config_root):
    """Use the same fail-closed settings preparation as the application.

    Ultralytics 8.3.40 reads ``<YOLO_CONFIG_DIR>/settings.json`` and validates
    the full settings schema before initializing telemetry. The installer
    preflight must prepare that exact file before importing the package.
    """
    # This file is installed at ``{app}\installer\runtime-preflight.py``;
    # the application helper it imports is in the sibling ``app`` directory.
    # Add that packaged application directory explicitly so the clean embedded
    # interpreter does not depend on its current working directory or a
    # developer/test PYTHONPATH.
    app_root = Path(__file__).resolve().parents[1] / "app"
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))
    from ultralytics_privacy import _prepare_settings_file

    _prepare_settings_file(Path(config_root), verify_acl=False)
    os.environ["YOLO_CONFIG_DIR"] = str(config_root)


def main():
    # Keep ultralytics' first-import settings file out of the installing
    # user's profile and write a complete, valid opt-out before importing it.
    config_root = Path(tempfile.mkdtemp(prefix="anyaicam-preflight-"))
    prepare_ultralytics_config(config_root)
    failures = check()
    if failures:
        # stdout only: the setup log records it, and Windows PowerShell 5.1
        # can turn a native command's redirected stderr into a terminating error.
        print("AnyAiCam runtime preflight FAILED:")
        for failure in failures:
            print("  " + failure)
        return 1
    print("AnyAiCam runtime preflight passed: " + ", ".join(REQUIRED_MODULES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
