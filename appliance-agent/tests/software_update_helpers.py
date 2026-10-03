"""Shared fixtures for the Software Update tests (2026-10-03).

Builds release packages with the exact layout installer/build_release_installer.py
produces (files at the archive root, artifact-files.json listing every file's
SHA-256, release.env, payload/vms/app/static/release-identity.json) and signs
manifests the way installer/sign_update_release.py does (RSA-PKCS1v15/SHA-256
over the canonical JSON). The private key exists only inside the test.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

BUILD_A = "a" * 40
BUILD_B = "b" * 40
BUILD_C = "c" * 40

# A minimal stand-in for app/db_migrations.py: what the migration gate reads.
MIGRATIONS_V1 = "MIGRATIONS=[('1','CREATE TABLE IF NOT EXISTS things(id TEXT PRIMARY KEY)')]\n"
MIGRATIONS_ADDITIVE = MIGRATIONS_V1 + "EXTRA='ALTER TABLE things ADD COLUMN note TEXT'\n"
MIGRATIONS_DESTRUCTIVE = MIGRATIONS_V1 + "EXTRA='ALTER TABLE things DROP COLUMN note'\n"


def generate_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo)
    return private_key, public_pem


def canonical(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign(private_key, manifest: dict) -> bytes:
    return private_key.sign(canonical(manifest), padding.PKCS1v15(), hashes.SHA256())


def verify_with(public_pem: bytes):
    """A verify_signature callable for the root Applier (the production one
    shells out to openssl)."""
    public_key = serialization.load_pem_public_key(public_pem)

    def verify(_key_path, data: bytes, signature: bytes) -> bool:
        try:
            public_key.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
            return True
        except Exception:  # noqa: BLE001
            return False
    return verify


def release_files(version: str, build_id: str, *, app_files: dict | None = None, migrations: str = MIGRATIONS_V1) -> dict:
    """{relative path: bytes} for an installer release package."""
    files = {
        "install.sh": b"#!/usr/bin/env bash\n",
        "validate.sh": b"#!/usr/bin/env bash\n",
        "rollback.sh": b"#!/usr/bin/env bash\n",
        "runtime/anyaicam-vms.service": b"[Service]\n",
        "payload/vms/Dockerfile": b"FROM python:3.12-slim\n",
        "payload/vms/docker-compose.yml": b"services: {}\n",
        "payload/vms/requirements.txt": b"",
        "payload/vms/app/main.py": f"RELEASE = {version!r}\n".encode(),
        "payload/vms/app/db_migrations.py": migrations.encode(),
        "payload/vms/app/static/release-identity.json": json.dumps(
            {"product": "AnyAiCam VMS", "version": version, "build_id": build_id}, sort_keys=True).encode(),
        "release.env": (f"VMS_RELEASE_COMMIT={build_id}\nVMS_RELEASE_SHA256={'d' * 64}\n"
                        f"INSTALLER_SOURCE_COMMIT={'e' * 40}\nMEDIAMTX_INCLUDED=false\nMEDIAMTX_SHA256=\n"
                        f"RELEASE_VERSION={version}\nUPDATE_SIGNING_KEY_SHA256=\n").encode(),
    }
    for relative, data in (app_files or {}).items():
        files[f"payload/vms/app/{relative}"] = data if isinstance(data, bytes) else data.encode()
    listing = [{"path": path, "sha256": hashlib.sha256(data).hexdigest(), "mode": "0o644", "size": len(data)}
               for path, data in sorted(files.items())]
    files["artifact-files.json"] = json.dumps(listing, indent=2, sort_keys=True).encode()
    return files


def write_tarball(path: Path, files: dict, *, extra_members=()) -> Path:
    """Writes a gzip tar with every file at the archive root (like
    write_deterministic_tar). extra_members: (TarInfo, bytes|None) pairs
    appended verbatim -- for malicious-archive tests."""
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        directories = set()
        for name in sorted(files):
            parts = name.split("/")[:-1]
            for depth in range(1, len(parts) + 1):
                directory = "/".join(parts[:depth])
                if directory not in directories:
                    directories.add(directory)
                    info = tarfile.TarInfo(directory)
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    archive.addfile(info)
            data = files[name]
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
        for info, data in extra_members:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    return path


def manifest_for(package: Path, *, version: str, build_id: str, update_id: str | None = None, platform="ubuntu",
                 architecture="x86_64", target="anyaicam-appliance", migration_safety="additive") -> dict:
    data = package.read_bytes()
    return {
        "update_id": update_id or f"{version}-{build_id[:12]}",
        "version": version,
        "build_id": build_id,
        "sha256": hashlib.sha256(data).hexdigest(),
        "package_size_bytes": len(data),
        "target": target,
        "platform": platform,
        "architecture": architecture,
        "channel": "stable",
        "issued_at": "2026-10-03T00:00:00Z",
        "migration_safety": migration_safety,
    }


def stage_release(staged_root: Path, manifest: dict, signature: bytes, package: Path, requested_by="owner@example.test") -> Path:
    """What the agent's OwnerUpdate.stage() leaves behind for the root applier."""
    directory = staged_root / manifest["update_id"]
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    (directory / "manifest.sig").write_bytes(base64.b64encode(signature))
    (directory / "package.tar.gz").write_bytes(package.read_bytes())
    (directory / "request.json").write_text(json.dumps({"update_id": manifest["update_id"], "requested_by": requested_by}),
                                            encoding="utf-8")
    return directory
