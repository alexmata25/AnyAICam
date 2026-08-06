#!/usr/bin/env python3
"""
AnyAiCam VMS v1.1.2 exact HLS latency patch.

Run from the AnyAiCam-VMS project root:
    python app/v1_1_2_hls_latency_patch.py
"""

from __future__ import annotations

import ast
import hashlib
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    target = Path("app/main.py").resolve()
    if not target.exists():
        print(f"ERROR: not found: {target}", file=sys.stderr)
        return 2

    raw = target.read_bytes()
    if b"\x00" in raw:
        print("ERROR: main.py contains null bytes.", file=sys.stderr)
        return 3

    source = raw.decode("utf-8")
    ast.parse(source)

    pattern = re.compile(
        r'(?P<indent>[ \t]*)"-c:v",\s*"libx264",\s*"-preset",\s*"veryfast",\s*'
        r'"-tune",\s*"zerolatency",\s*\n'
        r'(?P=indent)"-c:a",\s*"aac",\s*"-b:a",\s*"96k",\s*"-ac",\s*"1",\s*'
        r'"-ar",\s*"48000",\s*\n'
        r'(?P=indent)"-f",\s*"hls",\s*"-hls_time",\s*"2",\s*'
        r'"-hls_list_size",\s*"5",\s*\n'
        r'(?P=indent)"-hls_flags",\s*"delete_segments\+append_list",\s*output_file,',
        re.MULTILINE,
    )

    match = pattern.search(source)
    if not match:
        print("ERROR: exact HLS command block was not found.", file=sys.stderr)
        return 4

    indent = match.group("indent")
    replacement = (
        f'{indent}"-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",\n'
        f'{indent}"-g", "56", "-keyint_min", "28", "-sc_threshold", "0",\n'
        f'{indent}"-c:a", "aac", "-b:a", "96k", "-ac", "1", "-ar", "48000",\n'
        f'{indent}"-f", "hls", "-hls_time", "1", "-hls_list_size", "3",\n'
        f'{indent}"-hls_flags", '
        f'"delete_segments+append_list+omit_endlist+independent_segments",\n'
        f'{indent}output_file,'
    )

    updated, count = pattern.subn(replacement, source, count=1)
    if count != 1:
        print(f"ERROR: expected one replacement, got {count}.", file=sys.stderr)
        return 5

    ast.parse(updated)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = target.with_name(
        f"{target.stem}_before_v1_1_2_hls_{stamp}{target.suffix}"
    )
    shutil.copy2(target, backup)
    target.write_text(updated, encoding="utf-8", newline="\n")

    sums = target.with_name("SHA256SUMS_V1_1_2_HLS")
    sums.write_text(
        f"{sha256(target)}  {target.name}\n"
        f"{sha256(backup)}  {backup.name}\n",
        encoding="utf-8",
    )

    print("AnyAiCam VMS v1.1.2 HLS latency patch complete.")
    print(f"Updated: {target}")
    print(f"Backup: {backup}")
    print("HLS segment time: 1 second")
    print("HLS playlist size: 3 segments")
    print("H.264 GOP: 56 frames")
    print(f"SHA256SUMS: {sums}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
