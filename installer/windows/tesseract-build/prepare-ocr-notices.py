"""Assemble pinned notices for the actual minimal DLL closure; no release archive."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

DLL_PACKAGES = {
    "leptonica-1.87.0.dll": "leptonica", "deflate.dll": "libdeflate",
    "gif-7.dll": "giflib", "jpeg8.dll": "libjpeg-turbo", "lerc.dll": "lerc",
    "liblzma.dll": "liblzma", "libpng16.dll": "libpng",
    "libsharpyuv.dll": "libwebp-base", "libwebp.dll": "libwebp-base",
    "libwebpmux.dll": "libwebp-base", "openjp2.dll": "openjpeg",
    "tiff.dll": "libtiff", "zlib.dll": "libzlib", "zstd.dll": "zstd",
}
IJG_ACK = "This software is based in part on the work of the Independent JPEG Group."


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "supplement", "extra-notices", "audit", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Notice output exists; preserve previous evidence")
    manifest = json.loads((args.inputs / "inputs-manifest.json").read_text(encoding="utf-8-sig"))
    lock = json.loads((args.inputs / "tesseract-minimal.lock.json").read_text(encoding="utf-8-sig"))
    audit = json.loads(args.audit.read_text(encoding="utf-8-sig"))
    if not audit["normal_and_delay_imports_checked"]:
        raise ValueError("Dependency audit is incomplete")
    files, selected = {}, set()
    for dll in audit["bundled_dlls"]:
        name = dll["name"].lower()
        if name not in DLL_PACKAGES:
            raise ValueError(f"DLL has no reviewed notice/package mapping: {name}")
        if manifest.get(dll["source"]) != dll["sha256"]:
            raise ValueError(f"DLL provenance mismatch: {name}")
        selected.add(DLL_PACKAGES[name])

    def sealed(source: str, destination: str):
        data = (args.inputs / source).read_bytes()
        if digest(data) != manifest.get(source):
            raise ValueError(f"Unpinned notice: {source}")
        files[destination] = data

    packages = {p["name"]: p for p in lock["packages"]}
    for name in sorted(selected):
        if name not in packages:
            raise ValueError(f"Missing locked package: {name}")
        notices = sorted(p for p in (args.inputs / "licenses" / name).rglob("*") if p.is_file())
        if not notices:
            raise ValueError(f"No notices for {name}")
        for path in notices:
            sealed(path.relative_to(args.inputs).as_posix(), path.relative_to(args.inputs / "licenses").as_posix())
    sealed("src/LICENSE", "tesseract/LICENSE")
    sealed("src/AUTHORS", "tesseract/AUTHORS")

    def supplement(root: Path, filename: str, destination: str, commit: str):
        records = json.loads((root / "provenance-pinned.json").read_text(encoding="utf-8-sig"))
        record = next(r for r in records if r["File"] == filename)
        data = (root / filename).read_bytes()
        if digest(data).upper() != record["SHA256"].upper() or record["Commit"] != commit or not record["CommitContentVerified"]:
            raise ValueError(f"Supplement pin mismatch: {filename}")
        files[destination] = data
        return record

    sources = []
    if "libjpeg-turbo" in selected:
        sources.append(supplement(args.supplement, "libjpeg-turbo-3.2.0-README.ijg",
                                  "libjpeg-turbo/README.ijg", "c85e6b905bf237038faa936dab160ebfc5da0344"))
    if "lerc" in selected:
        sources.append(supplement(args.extra_notices, "lerc-4.2.0-NOTICE", "lerc/NOTICE",
                                  "e534980e11992cc5f11864b0dcff1af6a27cee0d"))
    if "libwebp-base" in selected:
        sources.append(supplement(args.extra_notices, "libwebp-1.6.0-PATENTS", "libwebp-base/PATENTS",
                                  "4fa21912338357f89e4fd51cf2368325b59e9bd9"))
    for name in ("LICENSE", "README.md"):
        sources.append(supplement(args.supplement, "tessdata_fast-4.1.0-" + name,
                                  "tessdata_fast/" + name, "65727574dfcd264acbb0c3e07860e4e9e9b22185"))
    model_hash = digest((args.inputs / "tessdata/eng.traineddata").read_bytes())
    if model_hash != lock["model"]["sha256"]:
        raise ValueError("Model provenance mismatch")
    model_provenance = {"url": lock["model"]["url"], "sha256": model_hash,
                        "release_commit": "65727574dfcd264acbb0c3e07860e4e9e9b22185",
                        "license": "Apache-2.0"}
    files["tessdata_fast/PROVENANCE.json"] = (json.dumps(model_provenance, indent=2) + "\n").encode()
    lines = ["# Third-party notices", "", IJG_ACK, "",
             "Tesseract 5.5.2: Apache-2.0. English model: tessdata_fast 4.1.0, Apache-2.0.",
             "Full original notices are included in the component directories.",
             "Windows and VC runtime imports are external prerequisites; no runtime DLLs are copied here.", ""]
    lines.extend(f"- {name} {packages[name]['version']}: {packages[name]['license']}" for name in sorted(selected))
    files["THIRD-PARTY-NOTICES.md"] = ("\n".join(lines) + "\n").encode()
    args.output.mkdir(parents=True)
    for relative, data in files.items():
        destination = args.output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    report = {"technical_notice_gate_passed": True, "remaining_known_notice_gaps": [],
              "production_release_approved": False, "packages": [packages[n] for n in sorted(selected)],
              "dll_package_map": {r["name"]: DLL_PACKAGES[r["name"].lower()] for r in audit["bundled_dlls"]},
              "supplement_provenance": sources, "model": model_provenance,
              "ijg_acknowledgment": IJG_ACK,
              "files": [{"path": n, "bytes": len(data), "sha256": digest(data)} for n, data in sorted(files.items())]}
    (args.output / "NOTICE-MANIFEST.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"packages": len(selected), "notice_files": len(files), "technical_notice_gate_passed": True}))


if __name__ == "__main__":
    main()
