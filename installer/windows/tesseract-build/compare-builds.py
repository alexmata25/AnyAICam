"""Compare two clean same-path OCR builds without altering either artifact set."""
import argparse
import hashlib
import json
from pathlib import Path

INPUT_MANIFEST_SHA256 = "62873d3ac5b896615b31f1cda4a90d18dbc89939b21adf671024bc9ccbd6cdad"


def inventory(root):
    return {p.relative_to(root).as_posix(): {"bytes": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for folder in ("engine", "distribution-notices")
            for p in sorted((root / folder).rglob("*")) if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("first", "second", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Reproduction report exists; preserve prior evidence")
    first, second = inventory(args.first), inventory(args.second)
    if "engine/tesseract.exe" not in first or "engine/tesseract.exe" not in second:
        raise ValueError("A build has no executable")
    plans = [json.loads((p / "build-plan.json").read_text(encoding="utf-8-sig")) for p in (args.first, args.second)]
    audits = [json.loads((p / "dependency-audit.json").read_text()) for p in (args.first, args.second)]
    commands_identical = all(plans[0][key] == plans[1][key] for key in ("ConfigureArguments", "BuildArguments"))
    inputs_identical = all(a["input_manifest_sha256"] == INPUT_MANIFEST_SHA256 for a in audits)
    differences = [{"path": name, "first": first.get(name), "second": second.get(name)}
                   for name in sorted(first.keys() | second.keys()) if first.get(name) != second.get(name)]
    passed = not differences and commands_identical and inputs_identical and all(p["InputsVerified"] == 970 for p in plans)
    args.output.mkdir(parents=True)
    for name, records in (("first", first), ("second", second)):
        (args.output / (name + "-payload-SHA256.json")).write_text(json.dumps(records, indent=2, sort_keys=True) + "\n")
    report = {"reproducibility_passed": passed, "same_absolute_build_paths": commands_identical,
              "sealed_inputs_identical": inputs_identical, "inputs_verified_each_run": [p["InputsVerified"] for p in plans],
              "payload_file_count": len(second), "payload_bit_identical": not differences,
              "first_preserved_directory": str(args.first), "second_directory": str(args.second),
              "first_executable_sha256": first["engine/tesseract.exe"]["sha256"],
              "second_executable_sha256": second["engine/tesseract.exe"]["sha256"],
              "differences": differences, "scope": "runtime executable, DLLs, model and complete assembled notices; not temporary build files"}
    (args.output / "reproducibility.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
