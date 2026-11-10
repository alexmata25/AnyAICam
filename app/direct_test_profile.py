"""Fail-closed data-root selection for the isolated direct-Windows test build.

This module is packaged only by the dedicated DirectDellTestCandidate setup.
Normal production packages do not include it. The profile is stored beneath
the protected test installation directory; callers cannot choose an arbitrary
path through an environment variable.
"""
from __future__ import annotations

import json
import os
import re
import stat
import uuid
from pathlib import Path

PROFILE_NAME = "direct-test-profile.json"
PROFILE_SCHEMA = 1
TEST_ROOT_NAME = "AnyAiCam-Test"
_PROFILE_KEYS = frozenset(("schema", "run_id", "data_root", "service_name", "http_port"))
_RUN_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_SERVICE_NAME = "AnyAiCamVmsDirectTest"
_HTTP_PORT = 18767


class TestProfileError(RuntimeError):
    """The isolated test profile is absent, malformed, or escapes its root."""


def _canonical(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path))).rstrip("\\/")


def _is_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _assert_plain_chain(path: Path, stop: Path) -> None:
    target, boundary = Path(os.path.abspath(path)), Path(os.path.abspath(stop))
    try:
        target.relative_to(boundary)
    except ValueError as exc:
        raise TestProfileError("test data root is outside the approved ProgramData subtree") from exc
    current = target
    while True:
        if _is_reparse(current):
            raise TestProfileError("reparse point in test data-root path")
        if _canonical(current) == _canonical(boundary):
            return
        parent = current.parent
        if parent == current:
            raise TestProfileError("test data-root boundary was not reached")
        current = parent


def expected_data_root(program_data: Path, run_id: str) -> Path:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise TestProfileError("invalid test run ID")
    return Path(program_data) / TEST_ROOT_NAME / run_id


def validate_profile_document(document: object, *, program_data: Path) -> Path:
    if not isinstance(document, dict) or set(document) != _PROFILE_KEYS:
        raise TestProfileError("unexpected test-profile fields")
    if type(document.get("schema")) is not int or document["schema"] != PROFILE_SCHEMA:
        raise TestProfileError("unsupported test-profile schema")
    run_id = document.get("run_id")
    expected = expected_data_root(program_data, run_id)
    supplied = document.get("data_root")
    if not isinstance(supplied, str) or _canonical(Path(supplied)) != _canonical(expected):
        raise TestProfileError("test data root does not match the run-bound ProgramData path")
    if document.get("service_name") != _SERVICE_NAME or type(document.get("http_port")) is not int or document["http_port"] != _HTTP_PORT:
        raise TestProfileError("test service or port differs from the reviewed profile")
    production = Path(program_data) / "AnyAiCam"
    if _canonical(expected) == _canonical(production):
        raise TestProfileError("production data root is forbidden in test mode")
    _assert_plain_chain(expected, Path(program_data) / TEST_ROOT_NAME)
    return expected


def _program_data() -> Path:
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                            ("Data4", ctypes.c_ubyte * 8)]

            folder = GUID(0x62AB5D82, 0xFDC1, 0x4DC3,
                          (ctypes.c_ubyte * 8)(0xA9, 0xDD, 0x07, 0x0D, 0x1D, 0x49, 0x5D, 0x97))
            found = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(found)) == 0:
                try:
                    return Path(found.value)
                finally:
                    ctypes.windll.ole32.CoTaskMemFree(found)
        except Exception as exc:
            raise TestProfileError("Windows Known Folder ProgramData resolution failed") from exc
        raise TestProfileError("Windows Known Folder ProgramData resolution failed")
    value = os.environ.get("ANYAICAM_TEST_PROGRAM_DATA")
    if not value:
        raise TestProfileError("test profile requires an explicit ProgramData test fixture")
    return Path(value)


def _program_files() -> Path:
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                            ("Data4", ctypes.c_ubyte * 8)]

            folder = GUID(0x905E63B6, 0xC1BF, 0x494E, (ctypes.c_ubyte * 8)(0xB2, 0x9C, 0x65, 0xB7, 0x32, 0xD3, 0xD2, 0x1A))
            found = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(found)) == 0:
                try:
                    return Path(found.value)
                finally:
                    ctypes.windll.ole32.CoTaskMemFree(found)
        except Exception as exc:
            raise TestProfileError("Windows Known Folder Program Files resolution failed") from exc
        raise TestProfileError("Windows Known Folder Program Files resolution failed")
    value = os.environ.get("ANYAICAM_TEST_PROGRAM_FILES")
    if not value:
        raise TestProfileError("test profile requires an explicit Program Files test fixture")
    return Path(value)


def resolve_installed_data_root(install_root: Path) -> Path:
    """Load the protected install profile; never consult a caller-supplied root."""
    install = Path(os.path.abspath(install_root))
    expected_install = _program_files() / "AnyAiCam VMS Direct Test"
    if _canonical(install) != _canonical(expected_install):
        raise TestProfileError("direct-test install root is not the fixed protected Program Files path")
    profile_path = install / PROFILE_NAME
    if _is_reparse(install) or _is_reparse(profile_path):
        raise TestProfileError("reparse point in test installation profile path")
    try:
        document = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TestProfileError("test profile is missing or unreadable") from exc
    configured_run_id = os.environ.get("ANYAICAM_TEST_RUN_ID")
    if configured_run_id is not None and document.get("run_id") != configured_run_id:
        raise TestProfileError("test run ID differs from the protected install profile")
    return validate_profile_document(document, program_data=_program_data())


def resolve_from_app_module(module_file: str | os.PathLike[str]) -> Path | None:
    """Select the test root only when the protected launcher explicitly marks this test build."""
    if os.environ.get("ANYAICAM_INTERNAL_TEST_BUILD") != "1":
        return None
    app_dir = Path(module_file).resolve().parent
    install_root = app_dir.parent
    return resolve_installed_data_root(install_root)
