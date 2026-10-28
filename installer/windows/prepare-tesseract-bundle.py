"""Build vendor/tesseract-5.5.2-win64-anyaicam.zip from pinned conda-forge packages (Windows 1.3.4, LPR parity).

Run on the build PC before build.ps1 (Python 3.14+, or any Python 3 with the `zstandard` package):

    py -3.14 installer/windows/prepare-tesseract-bundle.py --cache <download cache folder>

Nothing is executed: the packages are downloaded (or read from the cache), each checked against the SHA-256 in
tesseract-bundle.lock.json, and only the files the lock names are taken out of them.
- The DLLs are exactly the import closure of tesseract.exe. It is recomputed here from the PE import tables and must
  equal the lock's list, so a package update cannot add or drop a library unnoticed.
- System DLLs and the Visual C++ runtime (installed by the setup's vc_redist) are not bundled.

The zip is deterministic (sorted entries, fixed timestamps), so build.ps1 pins its SHA-256 like every vendor input.
Layout:
  tesseract/tesseract.exe, tesseract/*.dll, tesseract/tessdata/eng.traineddata
  licenses/<package>/...   every package's own licence files (info/licenses in the conda package)
  BUNDLE-MANIFEST.json     per-file SHA-256 and size, plus the package list; install-runtime.ps1 verifies it
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import struct
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCK = HERE / "tesseract-bundle.lock.json"
OUTPUT = HERE / "vendor" / "tesseract-5.5.2-win64-anyaicam.zip"
FIXED_TIME = (2026, 10, 27, 0, 0, 0)
# Provided by Windows or by the Visual C++ 2015-2022 redistributable the setup installs.
SYSTEM_DLL = re.compile(
    r"^(api-ms-win-.*|kernel32|user32|gdi32|advapi32|ws2_32|bcrypt|crypt32|secur32|normaliz|wldap32|ole32|oleaut32|"
    r"shell32|rpcrt4|iphlpapi|dnsapi|userenv|ntdll|shlwapi|version|netapi32|mswsock|ucrtbase|vcruntime140(_1)?|"
    r"msvcp140(_1|_2|_atomic_wait|_codecvt_ids)?|concrt140|vcomp140)\.dll$"
)


def _zstd_decompress(data: bytes) -> bytes:
    try:
        from compression import zstd  # Python 3.14+
        return zstd.decompress(data)
    except ImportError:
        import zstandard  # noqa: PLC0415
        return zstandard.ZstdDecompressor().decompressobj().decompress(data)


def package_files(conda_path: Path):
    """(member name, bytes) for every regular file of a .conda package (info and payload)."""
    with zipfile.ZipFile(conda_path) as outer:
        for inner in sorted(outer.namelist()):
            if inner.endswith(".tar.zst"):
                with tarfile.open(fileobj=io.BytesIO(_zstd_decompress(outer.read(inner)))) as tar:
                    for member in tar.getmembers():
                        if member.isfile():
                            yield member.name, tar.extractfile(member).read()


def pe_imports(data: bytes) -> list[str]:
    """DLL names a PE file imports (normal and delay-load import tables)."""
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("not a PE file")
    sections = struct.unpack_from("<H", data, pe + 6)[0]
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    directories = optional + (112 if struct.unpack_from("<H", data, optional)[0] == 0x20B else 96)
    table = []
    for index in range(sections):
        header = optional + optional_size + 40 * index
        virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from("<IIII", data, header + 8)
        table.append((virtual_address, max(virtual_size, raw_size), raw_pointer))

    def offset(rva: int) -> int:
        for virtual_address, size, raw_pointer in table:
            if virtual_address <= rva < virtual_address + size:
                return rva - virtual_address + raw_pointer
        raise ValueError(f"RVA {rva:#x} outside every section")

    def c_string(rva: int) -> str:
        start = offset(rva)
        return data[start:data.index(b"\0", start)].decode("ascii")

    names = []
    for directory, entry_size, name_field in ((1, 20, 12), (13, 32, 4)):
        rva = struct.unpack_from("<I", data, directories + 8 * directory)[0]
        if not rva:
            continue
        cursor = offset(rva)
        while data[cursor:cursor + entry_size] != b"\0" * entry_size:
            name_rva = struct.unpack_from("<I", data, cursor + name_field)[0]
            if not name_rva:
                break
            names.append(c_string(name_rva))
            cursor += entry_size
    return names


def import_closure(root: bytes, providers: dict[str, bytes]) -> tuple[set[str], set[str]]:
    """(bundled DLLs needed by `root`, imports nobody provides), case-insensitive."""
    needed, missing, pending = set(), set(), [root]
    while pending:
        for name in pe_imports(pending.pop()):
            dll = name.lower()
            if SYSTEM_DLL.match(dll) or dll in needed:
                continue
            if dll not in providers:
                missing.add(dll)
                continue
            needed.add(dll)
            pending.append(providers[dll])
    return needed, missing


def fetch(package: dict, cache: Path) -> Path:
    path = cache / package["file"]
    if not path.exists():
        with urllib.request.urlopen(package["url"], timeout=600) as response, open(str(path) + ".part", "wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
        os.replace(str(path) + ".part", path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != package["sha256"]:
        raise SystemExit(f"SHA-256 mismatch for {package['file']}: {digest} (lock: {package['sha256']})")
    return path


def build(lock: dict, cache: Path) -> tuple[bytes, dict]:
    executable = None
    providers: dict[str, bytes] = {}
    origin: dict[str, str] = {}
    model = None
    licenses: dict[str, bytes] = {}
    for package in lock["packages"]:
        for name, blob in package_files(fetch(package, cache)):
            lower = name.lower()
            if lower.startswith("library/bin/") and lower.endswith(".dll"):
                providers[lower.rsplit("/", 1)[1]] = blob
                origin[lower.rsplit("/", 1)[1]] = package["name"]
            elif lower == "library/bin/tesseract.exe":
                executable = blob
            elif name == lock["model"]["member"] and package["name"] == lock["model"]["package"]:
                model = blob
            elif name.startswith("info/licenses/"):
                licenses[f"licenses/{package['name']}/{name[len('info/licenses/'):]}"] = blob
    if executable is None or model is None:
        raise SystemExit("tesseract.exe or the OCR model is missing from the locked packages")
    if hashlib.sha256(model).hexdigest() != lock["model"]["sha256"]:
        raise SystemExit("eng.traineddata does not match its pinned SHA-256")
    needed, missing = import_closure(executable, providers)
    if missing:
        raise SystemExit(f"tesseract.exe imports DLLs no locked package provides: {sorted(missing)}")
    locked = {entry["dll"].lower() for entry in lock["dlls"]}
    if needed != locked:
        raise SystemExit(f"import closure drifted from the lock: added {sorted(needed - locked)}, removed {sorted(locked - needed)}")
    for entry in lock["dlls"]:
        if origin[entry["dll"].lower()] != entry["package"]:
            raise SystemExit(f"{entry['dll']} comes from {origin[entry['dll'].lower()]}, the lock says {entry['package']}")
    files = {"tesseract/tesseract.exe": executable, "tesseract/tessdata/eng.traineddata": model}
    for entry in lock["dlls"]:
        files[f"tesseract/{entry['dll']}"] = providers[entry["dll"].lower()]
    files.update(licenses)
    for package in lock["packages"]:
        if not any(path.startswith(f"licenses/{package['name']}/") for path in files):
            raise SystemExit(f"package {package['name']} carries no licence file")
    manifest = {
        "bundle": lock["bundle"],
        "packages": [{key: package[key] for key in ("name", "version", "file", "sha256", "license")} for package in lock["packages"]],
        "files": {path: {"sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob)} for path, blob in sorted(files.items())},
    }
    files["BUNDLE-MANIFEST.json"] = (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(files):
            info = zipfile.ZipInfo(path, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, files[path])
    return buffer.getvalue(), manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", required=True, type=Path, help="download cache folder (reused between runs)")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    args.cache.mkdir(parents=True, exist_ok=True)
    data, manifest = build(lock, args.cache)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    print(f"{args.output.name}: {len(data)} bytes, {len(manifest['files'])} files, sha256 {digest}")
    if lock.get("bundle_sha256") and digest != lock["bundle_sha256"]:
        print(f"ERROR: the bundle is not the locked one ({lock['bundle_sha256']})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
