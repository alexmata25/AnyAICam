
#!/usr/bin/env python3
"""Safely apply AnyAiCam Phase 6F to a Phase 6E-enabled main.py."""

from pathlib import Path
import argparse
import ast
import hashlib
import shutil
import sys
from datetime import datetime

REQUIRED_MARKER = "Phase 6E — Post-Launch Stabilization and Operational Assurance"
PHASE_MARKER = "Phase 6F — Controlled Production Validation and Customer Pilot"

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", nargs="?", default="main.py")
    parser.add_argument("--extension", default="phase6f_extension.py.txt")
    args = parser.parse_args()

    target = Path(args.target).resolve()
    extension_path = Path(args.extension).resolve()
    if not target.exists() or not extension_path.exists():
        print("ERROR: target or extension file is missing.", file=sys.stderr)
        return 2

    original = target.read_text(encoding="utf-8")
    if PHASE_MARKER in original:
        print("Phase 6F is already present; no changes made.")
        return 0
    if REQUIRED_MARKER not in original:
        print("ERROR: Phase 6E marker not found. Apply Phase 6E first.", file=sys.stderr)
        return 3

    ast.parse(original)
    extension = extension_path.read_text(encoding="utf-8")
    combined = original.rstrip() + "\n\n" + extension.lstrip()
    ast.parse(combined)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = target.with_name(f"{target.stem}_pre_phase6f_{stamp}{target.suffix}")
    shutil.copy2(target, backup)
    target.write_text(combined, encoding="utf-8")

    print(f"Applied Phase 6F to: {target}")
    print(f"Backup: {backup}")
    print(f"Backup SHA-256: {sha256(backup)}")
    print(f"Updated SHA-256: {sha256(target)}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
