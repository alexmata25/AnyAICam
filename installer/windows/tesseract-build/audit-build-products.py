"""Audit all EXE/DLL products, including CMake auxiliaries; run dumpbin in guest."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
from pathlib import Path

spec = importlib.util.spec_from_file_location("engine_audit", Path(__file__).with_name("audit-minimal-engine.py"))
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("build-root", "dumpbin", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--debug-runtime-dir", type=Path, action="append", default=[])
    parser.add_argument("--runtime-root", type=Path, help="Verified runtime used to audit a configure-only attempt")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Audit output exists; preserve previous evidence")
    root = args.build_root
    runtime_root = args.runtime_root or root
    dependency_report = json.loads((runtime_root / "dependency-audit.json").read_text())
    providers = {p.name.lower(): p.read_bytes() for p in (runtime_root / "engine").glob("*.dll")}
    pinned = {r["name"].lower(): r["sha256"] for r in dependency_report["bundled_dlls"]}
    if set(providers) != set(pinned) or any(sha(data) != pinned[name] for name, data in providers.items()):
        raise ValueError("Staged DLL set/hash differs from the pinned dependency audit")
    if sha((runtime_root / "engine/tesseract.exe").read_bytes()) != dependency_report["executable_sha256"]:
        raise ValueError("Staged executable changed")
    debug_names = {"vcruntime140d.dll", "vcruntime140_1d.dll", "msvcp140d.dll", "msvcp140_1d.dll",
                   "msvcp140_2d.dll", "msvcp140d_atomic_wait.dll", "msvcp140d_codecvt_ids.dll",
                   "concrt140d.dll", "ucrtbased.dll"}
    debug_providers, debug_paths = {}, {}
    for directory in args.debug_runtime_dir:
        for path in directory.rglob("*.dll"):
            name = path.name.lower()
            if name in debug_names:
                data = path.read_bytes()
                if name in debug_providers and data != debug_providers[name]:
                    raise ValueError(f"Ambiguous debug CRT provider: {name}")
                debug_providers[name], debug_paths[name] = data, path
    binaries = sorted(p for directory in (root / "engine", root / "work/build")
                      for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in (".exe", ".dll"))
    args.output.mkdir(parents=True)
    results = []
    for index, binary in enumerate(binaries, 1):
        data = binary.read_bytes()
        imports = {name.lower() for name in audit.imports_x64(data)}
        relative = binary.relative_to(root).as_posix()
        configure_probe = bool(re.match(
            r"^work/build/CMakeFiles/(?:[^/]+/CompilerId(?:C|CXX)/|_CMakeLTOTest-(?:C|CXX)/)", relative))
        # Compiler IDs/LTO probes are non-distributed build auxiliaries. Debug CRT providers
        # are NEVER permitted for the release engine or its DLLs.
        allowed_providers = {**providers, **debug_providers} if configure_probe else providers
        needed, system = audit.closure(data, allowed_providers)
        result = subprocess.run([str(args.dumpbin), "/nologo", "/dependents", str(binary)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        log = f"{index:03}-{binary.name}.dumpbin.log"
        (args.output / log).write_text(result.stdout + result.stderr)
        if result.returncode:
            raise ValueError(f"dumpbin failed: {binary}")
        dumpbin_imports = {name.lower() for name in re.findall(r"^\s+([\w.\-]+\.dll)\s*$", result.stdout, re.I | re.M)}
        if imports != dumpbin_imports:
            raise ValueError(f"PE/dumpbin dependency disagreement: {binary}: {imports ^ dumpbin_imports}")
        literals = []
        for name in ("iconv.dll", "charset.dll", "archive.dll", "libcurl.dll", "libxml2.dll"):
            if name.encode() in data.lower() or name.encode("utf-16le") in data.lower():
                literals.append(name)
        results.append({"path": relative, "bytes": len(data), "sha256": sha(data),
                        "scope": "non-distributed CMake configuration probe" if configure_probe else "release product",
                        "machine": "AMD64", "imports": sorted(imports), "transitive_bundled": sorted(needed & providers.keys()),
                        "build_only_nonredistributable_debug_runtime": sorted(needed & debug_providers.keys()),
                        "external_system_or_vc_runtime": sorted(system), "dumpbin_matches_pe": True,
                        "forbidden_dll_literal_hits": literals, "dumpbin_log": log})
    other = [{"path": p.relative_to(root).as_posix(), "bytes": p.stat().st_size, "sha256": sha(p.read_bytes())}
             for p in sorted((root / "work/build").rglob("*"))
             if p.is_file() and p.suffix.lower() in (".lib", ".pdb")]
    report = {"passed": True, "binaries_audited": len(results), "binary_results": results,
              "runtime_provenance_root": str(runtime_root),
              "other_build_artifacts": other, "normal_and_delay_imports_checked": True,
              "build_only_debug_runtime_providers": [{"name": n, "path": str(debug_paths[n]), "sha256": sha(data)}
                                                     for n, data in sorted(debug_providers.items())],
              "unresolved_or_forbidden_imports": [], "production_release_approved": False}
    (args.output / "all-binary-audit.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": True, "binaries_audited": len(results), "other_build_artifacts": len(other)}))


if __name__ == "__main__":
    main()
