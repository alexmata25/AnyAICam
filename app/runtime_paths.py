"""Where the VMS keeps its files (2026-10-07, Windows native runtime).

The Linux appliance runs the VMS in a container whose layout is fixed:
/app/static (the code's own static assets) and /app/recordings (the data
volume: recordings, the SQLite database, JSON stores). Those stay the
defaults, byte for byte -- nothing on Linux sets the variables below, so
nothing there changes.

A native Windows install has no /app: Path("/app/recordings") would mean
C:\\app\\recordings on whatever drive the service starts on. Its service
launcher sets:
  ANYAICAM_RECORDINGS_FOLDER  the data root (e.g. C:\\ProgramData\\AnyAiCam\\data)
  ANYAICAM_STATIC_FOLDER      the installed static assets

Every module that used to hard-code one of those two roots reads it from
here instead. Read once at import, like the constants they replace (tests
that monkeypatch a module's own constant keep working unchanged).
"""
from __future__ import annotations

import os
from pathlib import Path

LINUX_RECORDINGS_ROOT = '/app/recordings'
LINUX_STATIC_ROOT = '/app/static'


def _root(variable: str, default: str) -> Path:
    value = (os.environ.get(variable) or '').strip()
    return Path(value) if value else Path(default)


RECORDINGS_ROOT = _root('ANYAICAM_RECORDINGS_FOLDER', LINUX_RECORDINGS_ROOT)
STATIC_ROOT = _root('ANYAICAM_STATIC_FOLDER', LINUX_STATIC_ROOT)


def recordings_path(*parts: str) -> Path:
    return RECORDINGS_ROOT.joinpath(*parts)


def recordings_default(*parts: str) -> str:
    """The same location as a string, for os.getenv(..., default) calls.
    Unset -> exactly the old literal (forward slashes), so string
    comparisons and log lines on Linux are unchanged."""
    if RECORDINGS_ROOT == Path(LINUX_RECORDINGS_ROOT):
        return '/'.join((LINUX_RECORDINGS_ROOT, *parts))
    return str(RECORDINGS_ROOT.joinpath(*parts))
