"""Bounded raw OCR acceptance against unchanged fixtures and a sealed legacy report."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

LEGACY_SHA256 = "f1677bc657b9f0b7f8398a7b2d4a983b3d2e7c2466eb3f83a62ea3119363f642"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def invoke(command: list[str], env: dict[str, str], timeout: int) -> dict:
    started = time.monotonic()
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", env=env, timeout=timeout, check=False)
        record = {"exit_code": result.returncode, "stdout": result.stdout,
                  "stderr": result.stderr, "timed_out": False}
    except subprocess.TimeoutExpired as error:
        def text(value):
            return value.decode("utf-8", "replace") if isinstance(value, bytes) else value or ""
        record = {"exit_code": None, "stdout": text(error.stdout),
                  "stderr": text(error.stderr), "timed_out": True}
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=30, choices=range(1, 61))
    parser.add_argument("--repeats", type=int, default=2, choices=range(1, 4))
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Output exists; preserve previous results")
    baseline_hash = digest(args.baseline)
    if baseline_hash != LEGACY_SHA256:
        raise ValueError("Legacy baseline does not match the preserved evidence")
    baseline = json.loads(args.baseline.read_text(encoding="utf-8-sig"))
    legacy = {row["image"]: row for row in baseline["ocr_results"]}
    manifest = json.loads((args.inputs / "inputs-manifest.json").read_text(encoding="utf-8-sig"))
    lock = json.loads((args.inputs / "tesseract-minimal.lock.json").read_text(encoding="utf-8-sig"))
    images = sorted((args.inputs / "ocr-tests").glob("*.png"))
    if len(images) != 5 or set(legacy) != {p.name for p in images}:
        raise ValueError("Expected exactly the original five fixtures")
    for image in images:
        for file in (image, image.with_suffix(".txt")):
            if digest(file) != manifest[file.relative_to(args.inputs).as_posix()]:
                raise ValueError(f"Fixture changed: {file.name}")
        if image.with_suffix(".txt").read_text().strip() != legacy[image.name]["expected"]:
            raise ValueError("Legacy and fixture expectations disagree")
    model = args.engine / "tessdata/eng.traineddata"
    if digest(model) != lock["model"]["sha256"]:
        raise ValueError("Candidate model does not match the pinned release")
    args.output.mkdir(parents=True)
    env = os.environ.copy()
    windows = Path(env.get("SystemRoot", r"C:\Windows"))
    env.update(TESSDATA_PREFIX=str(args.engine / "tessdata"), OMP_THREAD_LIMIT="1",
               PATH=os.pathsep.join(map(str, [args.engine, windows / "System32", windows])))
    exe = args.engine / "tesseract.exe"
    version = invoke([str(exe), "--version"], env, args.timeout)
    version_ok = version["exit_code"] == 0 and bool(re.search(
        r"(?m)^tesseract 5\.5\.2(?:\s|$)", version["stdout"] + version["stderr"]))
    rows = []
    if version_ok:
        for repeat in range(1, args.repeats + 1):
            for image in images:
                result = invoke([str(exe), str(image), "stdout", "--psm", "7", "-c",
                                 "tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"],
                                env, args.timeout)
                expected = image.with_suffix(".txt").read_text().strip()
                actual = "".join(result["stdout"].split())
                result.update(image=image.name, repeat=repeat, expected=expected, actual=actual,
                              legacy_actual=legacy[image.name]["actual"],
                              same_as_legacy=actual == legacy[image.name]["actual"],
                              match=result["exit_code"] == 0 and actual == expected)
                rows.append(result)
                for stream in ("stdout", "stderr"):
                    (args.output / f"run{repeat}-{image.stem}.{stream}.log").write_text(result[stream])
    passed = version_ok and len(rows) == 5 * args.repeats and all(row["match"] for row in rows)
    report = {"engine_sha256": digest(exe), "model_sha256": digest(model),
              "baseline_sha256": baseline_hash, "version": version,
              "version_passed": version_ok, "timeout_seconds_per_process": args.timeout,
              "repeats": args.repeats, "maximum_process_time_budget_seconds": args.timeout * (1 + 5 * args.repeats),
              "passed": sum(row["match"] for row in rows), "total": 5 * args.repeats,
              "acceptance_passed": passed, "ocr_results": rows,
              "expectations_modified": False, "production_lpr_modified": False}
    (args.output / "ocr-acceptance.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key not in ("version", "ocr_results")}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
