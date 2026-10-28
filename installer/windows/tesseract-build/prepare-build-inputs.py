"""Stage the inputs for the minimal Windows Tesseract build (1.3.4 LPR parity, no libarchive/libcurl/libiconv).

    py -3.14 installer/windows/tesseract-build/prepare-build-inputs.py --cache <download cache> --out <inputs folder>

Data only, nothing is executed:
  <out>/src/                   the pinned Tesseract 5.5.2 source tree (tarball SHA-256 checked)
  <out>/prefix/Library/...     include/, lib/ (import libraries, CMake config) and bin/ (DLLs) of the 12 pinned
                               leptonica-closure packages from tesseract-minimal.lock.json (SHA-256 checked)
  <out>/tessdata/eng.traineddata   tessdata_fast 4.1.0 English (SHA-256 checked)
  <out>/build-tesseract-in-sandbox.ps1   the in-Sandbox build script (copied next to its inputs)
  <out>/inputs-manifest.json   SHA-256 of every staged file (re-seal with --refresh-manifest after adding
                               ocr-tests/ (make-ocr-test-images.py) and conda-bundle/ (the current bundle, for comparison))
The folder is mapped READ-ONLY into Windows Sandbox (make-build-wsb.ps1), where the compiler lives.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCK = HERE / "tesseract-minimal.lock.json"


def _bundle_tools():
    spec = importlib.util.spec_from_file_location("prepare_tesseract_bundle", HERE.parent / "prepare-tesseract-bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _download(url: str, sha256: str, path: Path) -> Path:
    if not path.exists():
        with urllib.request.urlopen(url, timeout=600) as response, open(str(path) + ".part", "wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
        Path(str(path) + ".part").replace(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != sha256:
        raise SystemExit(f"SHA-256 mismatch for {path.name}: {digest}")
    return path


def stage(lock: dict, cache: Path, out: Path) -> dict:
    tools = _bundle_tools()
    if out.exists():
        shutil.rmtree(out)
    (out / "prefix").mkdir(parents=True)
    source = lock["source"]
    tarball = _download(source["url"], source["sha256"], cache / f"tesseract-{source['version']}.tar.gz")
    with tarfile.open(tarball) as archive:
        top = archive.getmembers()[0].name.split("/")[0]
        for member in archive.getmembers():
            if not member.isfile() or ".." in member.name.split("/"):
                continue
            target = out / "src" / member.name[len(top) + 1:]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
    for package in lock["packages"]:
        for name, blob in tools.package_files(tools.fetch(package, cache)):
            if name.startswith(("Library/include/", "Library/lib/", "Library/bin/")) and ".." not in name.split("/"):
                if name.startswith("Library/bin/") and not name.lower().endswith(".dll"):
                    continue  # the packages' command-line tools are not build inputs
                target = out / "prefix" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(blob)
            elif name.startswith("info/licenses/"):
                target = out / "licenses" / package["name"] / name[len("info/licenses/"):]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(blob)
    model = lock["model"]
    (out / "tessdata").mkdir()
    shutil.copyfile(_download(model["url"], model["sha256"], cache / "eng.traineddata.tessdata_fast-4.1.0"),
                    out / "tessdata" / "eng.traineddata")
    shutil.copyfile(HERE / "build-tesseract-in-sandbox.ps1", out / "build-tesseract-in-sandbox.ps1")
    shutil.copyfile(LOCK, out / "tesseract-minimal.lock.json")
    forbidden = {name.lower() for name in lock["forbidden_dlls"]}
    staged = sorted(p for p in out.rglob("*") if p.is_file())
    bad = [p.name for p in staged if p.name.lower() in forbidden]
    if bad:
        raise SystemExit(f"forbidden DLLs staged: {bad}")
    manifest = {p.relative_to(out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in staged}
    (out / "inputs-manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--refresh-manifest", action="store_true",
                        help="only re-seal inputs-manifest.json after adding ocr-tests/ or conda-bundle/ to --out")
    args = parser.parse_args(argv)
    if args.refresh_manifest:
        files = sorted(p for p in args.out.rglob("*") if p.is_file() and p.name != "inputs-manifest.json")
        manifest = {p.relative_to(args.out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        (args.out / "inputs-manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"re-sealed {len(manifest)} files")
        return 0
    args.cache.mkdir(parents=True, exist_ok=True)
    manifest = stage(json.loads(LOCK.read_text(encoding="utf-8")), args.cache, args.out)
    dlls = sorted(Path(p).name for p in manifest if p.startswith("prefix/Library/bin/"))
    print(f"staged {len(manifest)} files into {args.out}; DLLs: {', '.join(dlls)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
