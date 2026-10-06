"""Shared setup for the installer tests.

The installer requires python3 (Ubuntu ships it; preflight and the root file
helpers use it). On a development host `python3` may not exist or may be a
placeholder (Windows), so the shell scripts these tests run find this test
interpreter first on PATH as `python3`. Under Git Bash on Windows the shim
also turns the POSIX paths in a file-helper request (path/template/source
lines) into Windows paths with cygpath; on Linux it changes nothing.
"""
import os
import sys
import tempfile
from pathlib import Path

_SHIM_DIR = Path(tempfile.mkdtemp(prefix="installer-tests-python3-"))


def _bash_path(path: Path) -> str:
    text = path.as_posix()
    return "/" + text[0].lower() + text[2:] if os.name == "nt" and text[1:2] == ":" else text


_PYTHON = _bash_path(Path(sys.executable))
_shim = _SHIM_DIR / "python3"
_shim.write_text(f"""#!/usr/bin/env bash
if [[ "$1" == "-c" ]] && command -v cygpath >/dev/null 2>&1; then
    exec "{_PYTHON}" "$@" < <(
        while IFS= read -r line; do
            case "$line" in
                "path "*|"template "*|"source "*) printf '%s %s\\n' "${{line%% *}}" "$(cygpath -w "${{line#* }}")" ;;
                content) printf '%s\\n' "$line"; cat; break ;;
                *) printf '%s\\n' "$line" ;;
            esac
        done)
fi
exec "{_PYTHON}" "$@"
""", encoding="utf-8", newline="\n")
_shim.chmod(0o755)
os.environ["PATH"] = f"{_SHIM_DIR}{os.pathsep}{os.environ.get('PATH', '')}"
