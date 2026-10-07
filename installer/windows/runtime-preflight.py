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
        except Exception as exc:
            failures.append(f"ultralytics.YOLO: {type(exc).__name__}: {exc}")
    return failures


def main():
    # Keep ultralytics' first-import settings file out of the installing
    # user's profile.
    os.environ.setdefault("YOLO_CONFIG_DIR", tempfile.mkdtemp(prefix="anyaicam-preflight-"))
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
