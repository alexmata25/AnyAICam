
#!/usr/bin/env python3
"""Apply AnyAiCam Phase 6E to a Phase 6D main.py safely."""

from __future__ import annotations
from pathlib import Path
import argparse
import ast
import hashlib
import shutil
import sys
from datetime import datetime

MARKER = "# Phase 6E — Post-Launch Stabilization and Operational Assurance"

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", nargs="?", default="main.py")
    parser.add_argument("--extension", default="phase6e_extension.py.txt")
    args = parser.parse_args()

    target = Path(args.target).resolve()
    extension_path = Path(args.extension).resolve()
    if not target.exists():
        print(f"ERROR: target not found: {target}", file=sys.stderr)
        return 2
    if not extension_path.exists():
        print(f"ERROR: extension not found: {extension_path}", file=sys.stderr)
        return 2

    original = target.read_text(encoding="utf-8")
    if MARKER in original:
        print("Phase 6E is already present; no changes made.")
        return 0

    ast.parse(original)
    extension = extension_path.read_text(encoding="utf-8")
    combined = original.rstrip() + "\n\n" + extension.lstrip()
    ast.parse(combined)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = target.with_name(f"{target.stem}_pre_phase6e_{stamp}{target.suffix}")
    shutil.copy2(target, backup)
    target.write_text(combined, encoding="utf-8")

    print(f"Applied Phase 6E to: {target}")
    print(f"Backup: {backup}")
    print(f"Backup SHA-256: {sha256(backup)}")
    print(f"Updated SHA-256: {sha256(target)}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
