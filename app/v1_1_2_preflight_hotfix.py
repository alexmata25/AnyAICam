#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
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


def read_python(path: Path) -> str:
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise RuntimeError(f"{path} contains null bytes.")
    source = raw.decode("utf-8")
    ast.parse(source)
    return source


def make_backup(path: Path, label: str, stamp: str) -> Path:
    backup = path.with_name(f"{path.stem}_before_{label}_{stamp}{path.suffix}")
    shutil.copy2(path, backup)
    return backup


def ensure_cloud_import(path: Path, stamp: str):
    source = read_python(path)
    marker = "from cloud_config import settings as cloud_settings"
    if marker in source:
        return False, None

    lines = source.splitlines(keepends=True)
    insert_at = 0
    if lines and lines[0].startswith("#!"):
        insert_at = 1

    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, (ast.Str, ast.Constant)):
            insert_at = max(insert_at, node.end_lineno or insert_at)
        elif isinstance(node, ast.ImportFrom) and node.module == "__future__":
            insert_at = max(insert_at, node.end_lineno or insert_at)
        else:
            break

    lines.insert(insert_at, marker + "\n")
    updated = "".join(lines)
    ast.parse(updated)
    backup = make_backup(path, "cloud_settings_fix", stamp)
    path.write_text(updated, encoding="utf-8", newline="\n")
    return True, backup


def patch_main(path: Path, stamp: str):
    source = read_python(path)
    original = source

    # Move the page-specific scripts after the global fetch wrapper.
    start_marker = "{scripts}<script>const nativeFetch=window.fetch.bind(window);"
    if start_marker in source:
        source = source.replace(
            start_marker,
            "<script>const nativeFetch=window.fetch.bind(window);",
            1,
        )
        end_marker = "}}}});</script></body></html>\"\"\""
        if end_marker not in source:
            raise RuntimeError("Could not find page_shell closing marker.")
        source = source.replace(
            end_marker,
            "}}}});</script>{scripts}</body></html>\"\"\"",
            1,
        )
        csrf_fixed = True
    else:
        csrf_fixed = False

    # Lower live HLS latency.
    old_hls = (
        '        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",\n'
        '        "-c:a", "aac", "-b:a", "96k", "-ac", "1", "-ar", "48000",\n'
        '        "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",\n'
        '        "-hls_flags", "delete_segments+append_list", output_file,'
    )
    new_hls = (
        '        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",\n'
        '        "-g", "56", "-keyint_min", "28", "-sc_threshold", "0",\n'
        '        "-c:a", "aac", "-b:a", "96k", "-ac", "1", "-ar", "48000",\n'
        '        "-f", "hls", "-hls_time", "1", "-hls_list_size", "3",\n'
        '        "-hls_flags", "delete_segments+append_list+omit_endlist+independent_segments",\n'
        '        output_file,'
    )
    if old_hls in source:
        source = source.replace(old_hls, new_hls, 1)
        hls_fixed = True
    else:
        hls_fixed = False

    ast.parse(source)
    backup = make_backup(path, "v1_1_2_preflight", stamp)
    path.write_text(source, encoding="utf-8", newline="\n")
    return csrf_fixed, hls_fixed, backup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_root", nargs="?", default=".")
    args = parser.parse_args()

    root = Path(args.project_root).expanduser().resolve()
    app_dir = root / "app"
    main_path = app_dir / "main.py"
    partner_path = app_dir / "partner_workspace.py"
    appliance_path = app_dir / "appliance_cloud.py"

    for path in (main_path, partner_path, appliance_path):
        if not path.exists():
            print(f"ERROR: missing {path}", file=sys.stderr)
            return 2

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csrf_fixed, hls_fixed, main_backup = patch_main(main_path, stamp)
    partner_fixed, partner_backup = ensure_cloud_import(partner_path, stamp)
    appliance_fixed, appliance_backup = ensure_cloud_import(appliance_path, stamp)

    for path in (main_path, partner_path, appliance_path):
        ast.parse(path.read_text(encoding="utf-8"))

    sums = app_dir / "SHA256SUMS_V1_1_2_PREFLIGHT"
    sums.write_text(
        f"{sha256(main_path)}  {main_path.name}\n"
        f"{sha256(partner_path)}  {partner_path.name}\n"
        f"{sha256(appliance_path)}  {appliance_path.name}\n",
        encoding="utf-8",
    )

    print("AnyAiCam VMS v1.1.2 preflight hotfix complete.")
    print(f"main backup: {main_backup}")
    if partner_backup:
        print(f"partner backup: {partner_backup}")
    if appliance_backup:
        print(f"appliance backup: {appliance_backup}")
    print(f"CSRF script ordering fixed: {csrf_fixed}")
    print(f"HLS latency settings fixed: {hls_fixed}")
    print(f"partner cloud_settings import added: {partner_fixed}")
    print(f"appliance cloud_settings import added: {appliance_fixed}")
    print(f"SHA256SUMS: {sums}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
