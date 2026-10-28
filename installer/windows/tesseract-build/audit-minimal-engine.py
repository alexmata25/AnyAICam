"""Offline PE closure gate; creates a provisional engine, never a release archive.

Normal and delay imports use the existing bundler's parser. Windows/VC runtime
imports are external prerequisites, not bundled providers or licensing clearance.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import struct
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("pe_bundle", HERE.parent / "prepare-tesseract-bundle.py")
pe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pe)
FORBIDDEN = re.compile(r"archive|iconv|charset|libxml|libcurl|icu(?:uc|dt|in)|libcrypto|libssl|libssh|psl|lzo", re.I)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def imports_x64(data: bytes) -> list[str]:
    if data[:2] != b"MZ":
        raise ValueError("Not an MZ executable")
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[offset:offset + 4] != b"PE\0\0" or struct.unpack_from("<H", data, offset + 4)[0] != 0x8664:
        raise ValueError("Expected an AMD64 PE executable")
    return pe.pe_imports(data)


def closure(root: bytes, providers: dict[str, bytes]) -> tuple[set[str], set[str]]:
    """Fail closed on forbidden, unresolved, malformed or wrong-architecture imports."""
    providers = {name.lower(): data for name, data in providers.items()}
    needed, system, pending = set(), set(), [root]
    while pending:
        for name in imports_x64(pending.pop()):
            name = name.lower()
            if FORBIDDEN.search(name):
                raise ValueError(f"Forbidden dependency: {name}")
            if pe.SYSTEM_DLL.fullmatch(name):
                system.add(name)
            elif name not in needed:
                if name not in providers:
                    raise ValueError(f"Unresolved dependency: {name}")
                needed.add(name)
                pending.append(providers[name])
    return needed, system


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Output already exists; preserve prior evidence")
    manifest = json.loads((args.inputs / "inputs-manifest.json").read_text(encoding="utf-8-sig"))
    providers, paths = {}, {}
    for path in (args.inputs / "prefix/Library/bin").glob("*.dll"):
        relative = path.relative_to(args.inputs).as_posix()
        data = path.read_bytes()
        if sha(data) != manifest.get(relative):
            raise ValueError(f"Unpinned or modified provider: {relative}")
        name = path.name.lower()
        if name in providers:
            raise ValueError(f"Ambiguous DLL provider: {name}")
        providers[name], paths[name] = data, path
    executable = args.exe.read_bytes()
    needed, system = closure(executable, providers)
    lock = json.loads((args.inputs / "tesseract-minimal.lock.json").read_text(encoding="utf-8-sig"))
    model = args.inputs / "tessdata/eng.traineddata"
    if sha(model.read_bytes()) != lock["model"]["sha256"]:
        raise ValueError("Model hash mismatch")
    args.output.mkdir(parents=True)
    shutil.copy2(args.exe, args.output / "tesseract.exe")
    for name in sorted(needed):
        shutil.copy2(paths[name], args.output / paths[name].name)
    (args.output / "tessdata").mkdir()
    shutil.copy2(model, args.output / "tessdata/eng.traineddata")
    report = {
        "executable_sha256": sha(executable),
        "bundled_dlls": [{"name": name, "sha256": sha(providers[name]),
                          "source": paths[name].relative_to(args.inputs).as_posix()} for name in sorted(needed)],
        "external_system_or_vc_runtime_imports": sorted(system),
        "normal_and_delay_imports_checked": True,
        "licensing_cleared": False,
        "release_archive_created": False,
    }
    (args.output.parent / "dependency-audit.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
