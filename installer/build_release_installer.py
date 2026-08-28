#!/usr/bin/env python3
"""Build a self-contained AnyAiCam appliance installer from one exact VMS release.

The runtime installer never reads application files from a surrounding Git
checkout. This builder is the only place where a VMS Git commit or verified
release archive is accepted.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_RELEASE_PATHS = (
    "app",
    "requirements.txt",
    "Dockerfile",
    "Dockerfile.production",
    "docker-compose.yml",
)
OPTIONAL_RELEASE_PATHS = ("migrations", "static", "templates", "systemd")
INSTALLER_RUNTIME_FILES = (
    "install.sh",
    "01-preflight.sh",
    "02-storage-check.sh",
    "03-detect-install.sh",
    "04-docker-setup.sh",
    "05-provision-users-dirs.sh",
    "06-deploy-vms.sh",
    "07-install-agent.sh",
    "08-systemd-setup.sh",
    "09-identity.sh",
    "validate.sh",
    "uninstall.sh",
    "README.md",
)
DANGEROUS_NAME_PATTERNS = (
    re.compile(r"(^|/)\.env($|\.)", re.I),
    re.compile(r"(^|/)aws\.env$", re.I),
    re.compile(r"(^|/)(id_rsa|id_ed25519)$", re.I),
    re.compile(r"\.(pem|key|p12|pfx)$", re.I),
    re.compile(r"\.db$", re.I),
    re.compile(r"(\.pre-|_before_|/before-|\.bak$|~$)", re.I),
)
SECRET_CONTENT_PATTERNS = (
    ("private key", re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("GitHub token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("Stripe live secret", re.compile(rb"\bsk_live_[A-Za-z0-9]{16,}\b")),
)


def run_git(repo: Path, *args: str, capture: bool = True) -> bytes:
    cmd = ["git", "-C", str(repo), *args]
    result = subprocess.run(cmd, check=True, stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else b""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def validate_commit(value: str, label: str) -> str:
    if not COMMIT_RE.fullmatch(value):
        raise SystemExit(f"{label} must be an exact 40-character lowercase Git SHA-1 commit hash")
    return value


def validate_sha256(value: str, label: str) -> str:
    if not SHA256_RE.fullmatch(value):
        raise SystemExit(f"{label} must be a 64-character lowercase SHA-256")
    return value


def safe_tar_extract(data: bytes, destination: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        for member in tf.getmembers():
            p = PurePosixPath(member.name)
            if p.is_absolute() or ".." in p.parts or member.issym() or member.islnk():
                raise SystemExit(f"Unsafe release archive member: {member.name}")
        tf.extractall(destination, filter="data")


def safe_zip_extract(path: Path, destination: Path) -> None:
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            p = PurePosixPath(info.filename)
            if p.is_absolute() or ".." in p.parts:
                raise SystemExit(f"Unsafe release archive member: {info.filename}")
        zf.extractall(destination)


def extract_external_archive(path: Path, destination: Path) -> None:
    data = path.read_bytes()
    if zipfile.is_zipfile(path):
        safe_zip_extract(path, destination)
        return
    try:
        safe_tar_extract(data, destination)
    except tarfile.TarError as exc:
        raise SystemExit(f"Unsupported release archive format: {path}: {exc}") from exc


def locate_release_root(extracted: Path) -> Path:
    def looks_like_release(p: Path) -> bool:
        return all((p / rel).exists() for rel in REQUIRED_RELEASE_PATHS)

    if looks_like_release(extracted):
        return extracted
    children = [p for p in extracted.iterdir() if p.is_dir()]
    if len(children) == 1 and looks_like_release(children[0]):
        return children[0]
    raise SystemExit("Release archive does not contain the required VMS release layout")


def git_release_tree(repo: Path, commit: str, destination: Path) -> tuple[Path, str]:
    run_git(repo, "cat-file", "-e", f"{commit}^{{commit}}", capture=False)
    resolved = run_git(repo, "rev-parse", f"{commit}^{{commit}}").decode().strip()
    if resolved != commit:
        raise SystemExit(f"Git resolved VMS commit to {resolved}, expected {commit}")

    names = set(run_git(repo, "ls-tree", "-r", "--name-only", commit).decode().splitlines())
    dirs = {name.split("/", 1)[0] for name in names if "/" in name}
    requested = list(REQUIRED_RELEASE_PATHS)
    for optional in OPTIONAL_RELEASE_PATHS:
        if optional in dirs or optional in names:
            requested.append(optional)

    archive = run_git(repo, "archive", "--format=tar", commit, "--", *requested)
    safe_tar_extract(archive, destination)
    return destination, sha256_bytes(archive)


def reject_unsafe_release_files(release_root: Path) -> None:
    has_migrations = (release_root / "migrations").is_dir() or (release_root / "app/db_migrations.py").is_file()
    if not has_migrations:
        raise SystemExit("Release is missing migrations (expected migrations/ or app/db_migrations.py)")
    has_static = (release_root / "app/static").is_dir() or (release_root / "static").is_dir()
    if not has_static:
        raise SystemExit("Release is missing required static assets (expected app/static/ or static/)")

    for path in sorted(release_root.rglob("*")):
        if path.is_symlink():
            raise SystemExit(f"Symlinks are not allowed in the release payload: {path.relative_to(release_root)}")
        if not path.is_file():
            continue
        rel = path.relative_to(release_root).as_posix()
        for pattern in DANGEROUS_NAME_PATTERNS:
            if pattern.search(rel):
                raise SystemExit(f"Refusing release payload with secret/state/backup-like file: {rel}")
        data = path.read_bytes()
        for label, pattern in SECRET_CONTENT_PATTERNS:
            if pattern.search(data):
                raise SystemExit(f"Refusing release payload containing a likely {label}: {rel}")


def scan_payload_for_secrets(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise SystemExit(f"Symlinks are not allowed in packaged payload: {path.relative_to(root)}")
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        data = path.read_bytes()
        for label, pattern in SECRET_CONTENT_PATTERNS:
            if pattern.search(data):
                raise SystemExit(f"Refusing packaged payload containing a likely {label}: {rel}")


def copy_release(release_root: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for rel in REQUIRED_RELEASE_PATHS + OPTIONAL_RELEASE_PATHS:
        src = release_root / rel
        if not src.exists():
            continue
        target = dest / rel
        if src.is_dir():
            shutil.copytree(src, target, copy_function=shutil.copy2)
        else:
            shutil.copy2(src, target)


def git_export_paths(repo: Path, commit: str, paths: tuple[str, ...], destination: Path) -> None:
    archive = run_git(repo, "archive", "--format=tar", commit, "--", *paths)
    safe_tar_extract(archive, destination)


def ensure_lf_and_modes(package_root: Path) -> tuple[int, list[str]]:
    shell_files = sorted(package_root.glob("*.sh"))
    failures: list[str] = []
    for path in shell_files:
        data = path.read_bytes()
        if b"\r" in data:
            failures.append(f"CR byte found: {path.relative_to(package_root)}")
        os.chmod(path, 0o755)
    return len(shell_files), failures


def file_manifest(root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256_file(path),
                "mode": oct(stat.S_IMODE(path.stat().st_mode)),
                "size": path.stat().st_size,
            }
        )
    return rows


def write_deterministic_tar(source: Path, output: Path, mtime: int) -> None:
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=mtime) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tf:
                for path in sorted(source.rglob("*")):
                    rel = path.relative_to(source).as_posix()
                    info = tf.gettarinfo(str(path), arcname=rel)
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = mtime
                    if path.is_file():
                        with path.open("rb") as f:
                            tf.addfile(info, f)
                    else:
                        tf.addfile(info)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vms-commit", required=True, help="Exact approved 40-character VMS commit")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--vms-repo", help="Git repo containing the exact VMS commit")
    source.add_argument("--release-archive", help="Prebuilt VMS release archive")
    parser.add_argument("--release-sha256", help="Required with --release-archive")
    parser.add_argument("--env-template", help="Optional non-secret VMS environment template")
    parser.add_argument("--output-dir", default="dist")
    args = parser.parse_args()

    vms_commit = validate_commit(args.vms_commit, "--vms-commit")
    script_path = Path(__file__).resolve()
    repo_root = script_path.parents[1]

    installer_commit = run_git(repo_root, "rev-parse", "HEAD").decode().strip()
    validate_commit(installer_commit, "installer HEAD")
    subprocess.run(["git", "-C", str(repo_root), "diff", "--quiet", "--", "installer", "appliance-agent"], check=True)
    subprocess.run(["git", "-C", str(repo_root), "diff", "--cached", "--quiet", "--", "installer", "appliance-agent"], check=True)
    installer_mtime = int(run_git(repo_root, "show", "-s", "--format=%ct", installer_commit).decode().strip())

    with tempfile.TemporaryDirectory(prefix="anyaicam-release-build-") as td:
        temp = Path(td)
        release_extract = temp / "release"
        release_extract.mkdir()

        if args.vms_repo:
            release_root, release_sha = git_release_tree(Path(args.vms_repo).resolve(), vms_commit, release_extract)
            release_source = "git"
        else:
            if not args.release_sha256:
                raise SystemExit("--release-sha256 is required with --release-archive")
            expected = validate_sha256(args.release_sha256, "--release-sha256")
            archive_path = Path(args.release_archive).resolve()
            actual = sha256_file(archive_path)
            if actual != expected:
                raise SystemExit(f"Release archive SHA-256 mismatch: expected {expected}, got {actual}")
            extract_external_archive(archive_path, release_extract)
            release_root = locate_release_root(release_extract)
            release_sha = actual
            release_source = "archive"

        reject_unsafe_release_files(release_root)

        package = temp / "package"
        package.mkdir()

        git_export_paths(
            repo_root,
            installer_commit,
            tuple(f"installer/{p}" for p in INSTALLER_RUNTIME_FILES) + ("installer/runtime",),
            temp / "installer-export",
        )
        exported_installer = temp / "installer-export/installer"
        for rel in INSTALLER_RUNTIME_FILES:
            src = exported_installer / rel
            dst = package / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        shutil.copytree(exported_installer / "runtime", package / "runtime", copy_function=shutil.copy2)

        agent_export = temp / "agent-export"
        agent_paths = (
            "appliance-agent/pyproject.toml",
            "appliance-agent/anyaicam_agent",
            "appliance-agent/scripts",
            "appliance-agent/systemd",
        )
        git_export_paths(repo_root, installer_commit, agent_paths, agent_export)
        shutil.copytree(agent_export / "appliance-agent", package / "payload/agent", copy_function=shutil.copy2)

        copy_release(release_root, package / "payload/vms")

        release_unit = release_root / "systemd/anyaicam-vms.service"
        service_source = "installer"
        if release_unit.is_file():
            shutil.copy2(release_unit, package / "runtime/anyaicam-vms.service")
            service_source = "vms-release"

        env_template_sha = ""
        if args.env_template:
            env_src = Path(args.env_template).resolve()
            if not env_src.is_file():
                raise SystemExit(f"Environment template not found: {env_src}")
            env_data = env_src.read_bytes()
            if b"\r" in env_data:
                raise SystemExit("Environment template must use LF line endings")
            for label, pattern in SECRET_CONTENT_PATTERNS:
                if pattern.search(env_data):
                    raise SystemExit(f"Environment template appears to contain a {label}; only placeholders are allowed")
            env_dest = package / "payload/config/vms.env.template"
            env_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(env_src, env_dest)
            env_template_sha = sha256_file(env_dest)

        release_env = (
            f"VMS_RELEASE_COMMIT={vms_commit}\n"
            f"VMS_RELEASE_SHA256={release_sha}\n"
            f"INSTALLER_SOURCE_COMMIT={installer_commit}\n"
        )
        (package / "release.env").write_text(release_env, encoding="utf-8", newline="\n")

        scan_payload_for_secrets(package / "payload")
        shell_count, lf_failures = ensure_lf_and_modes(package)
        if lf_failures:
            raise SystemExit("Shell LF verification failed:\n" + "\n".join(lf_failures))

        manifest = {
            "schema": 1,
            "installer_version": "1.1.0",
            "installer_source_commit": installer_commit,
            "vms_release_commit": vms_commit,
            "vms_release_source": release_source,
            "vms_release_archive_sha256": release_sha,
            "environment_template_sha256": env_template_sha,
            "vms_service_source": service_source,
            "shell_script_count": shell_count,
        }
        (package / "release-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )

        (package / "artifact-files.json").write_text(
            json.dumps(file_manifest(package), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        outdir = Path(args.output_dir).resolve()
        outdir.mkdir(parents=True, exist_ok=True)
        filename = f"anyaicam-appliance-installer-1.1.0-vms-{vms_commit[:12]}.tar.gz"
        output = outdir / filename
        write_deterministic_tar(package, output, installer_mtime)
        digest = sha256_file(output)

        print(f"installer_source_commit={installer_commit}")
        print(f"vms_release_commit={vms_commit}")
        print(f"release_source_sha256={release_sha}")
        print(f"artifact={output}")
        print(f"artifact_sha256={digest}")
        print(f"shell_script_count={shell_count}")
        print("shell_lf=PASS")
        print("shell_executable=PASS")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
