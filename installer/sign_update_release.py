#!/usr/bin/env python3
"""Offline Software Update release signer (2026-10-03).

Run by the owner on a trusted machine that holds the release-signing PRIVATE
key -- never on an appliance, never on the cloud server, and the key file must
not be inside this repository. Input is an installer built by
build_release_installer.py (with --release-version and the matching
--update-signing-public-key). Output, in --out-dir:

  manifest.json   the release manifest (update_id, version, build_id, sha256,
                  size, target, platform, architecture, channel, issued_at,
                  migration_safety)
  manifest.sig    base64 RSA-PKCS1v15/SHA-256 signature over the manifest's
                  canonical JSON (the scheme appliance-agent/.../verify.py
                  and the root applier check)

Before signing it re-checks the installer exactly as an appliance will (safe
archive members, release.env, artifact-files.json hashes, release identity
file), compares database migrations against the previous release's installer
and refuses anything but additive changes, and refuses a key pair whose
public half differs from the one the installer provisions.

The cloud stores and serves manifest.json + manifest.sig + the installer
(app/publish_update_release.py); it never sees the private key.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "appliance-agent"))

from anyaicam_agent.updater import release_checks  # noqa: E402


def canonical_manifest_bytes(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _extract(installer: Path, into: Path) -> tuple[Path, dict]:
    root = release_checks.safe_extract(installer, into)
    env = release_checks.read_release_env(root / "release.env")
    return root, env


def build_manifest(*, installer: Path, previous_installer: Path | None, first_release: bool, target: str,
                   channel: str, platform: str, architecture: str, update_id: str | None, now: datetime) -> tuple[dict, Path]:
    with tempfile.TemporaryDirectory(prefix="anyaicam-sign-") as scratch:
        scratch = Path(scratch)
        root, env = _extract(installer, scratch / "new")
        version = env.get("RELEASE_VERSION", "")
        build_id = env.get("VMS_RELEASE_COMMIT", "")
        release_checks.parse_release_version(version)
        release_checks.validate_build_id(build_id)
        probe = SimpleNamespace(version=version, build_id=build_id)
        release_checks.verify_release_tree(root, probe)
        identity = json.loads((root / "payload/vms/app/static/release-identity.json").read_text(encoding="utf-8"))
        if identity.get("version") != version or identity.get("build_id") != build_id:
            raise SystemExit("release-identity.json does not match release.env")
        key_path = root / "payload/keys/update-signing-public-key.pem"
        embedded_key = key_path.read_bytes() if key_path.is_file() else b""
        if previous_installer is not None:
            previous_root, previous_env = _extract(previous_installer, scratch / "previous")
            previous_version = previous_env.get("RELEASE_VERSION") or "1.1.0"
            if not release_checks.is_newer(version, previous_version):
                raise SystemExit(f"{version} is not newer than the previous release {previous_version}")
            unsafe = release_checks.find_unsafe_migrations(previous_root / "payload/vms/app", root / "payload/vms/app")
            if unsafe:
                raise SystemExit("Refusing to sign: the release changes the database in a way the previous version "
                                 "cannot use (unsupported for launch updates):\n  " + "\n  ".join(unsafe))
        elif not first_release:
            raise SystemExit("Pass --previous-installer (the release appliances run now) for the migration check, "
                             "or --first-release for the very first signed release.")
        package_size = installer.stat().st_size
        manifest = {
            "update_id": update_id or f"{version}-{build_id[:12]}-{now.strftime('%Y%m%d%H%M%S')}",
            "version": version,
            "build_id": build_id,
            "sha256": release_checks.sha256_file(installer),
            "package_size_bytes": package_size,
            "target": target,
            "platform": platform,
            "architecture": architecture,
            "channel": channel,
            "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "migration_safety": "additive",
        }
        release_checks.validate_update_id(manifest["update_id"])
        return manifest, embedded_key


def sign(manifest: dict, private_key_path: Path, embedded_public_key: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    resolved = private_key_path.resolve()
    if REPO_ROOT in resolved.parents:
        raise SystemExit("Refusing a signing key stored inside the repository; keep it offline.")
    key = serialization.load_pem_private_key(resolved.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise SystemExit("The signing key must be an RSA private key.")
    public_pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    if embedded_public_key and embedded_public_key.strip() != public_pem.strip():
        raise SystemExit("This signing key does not match the public key the installer provisions; appliances would "
                         "refuse the release.")
    data = canonical_manifest_bytes(manifest)
    signature = key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    key.public_key().verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
    return signature


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--signing-key", required=True, type=Path, help="Offline RSA private key (PEM)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--previous-installer", type=Path)
    group.add_argument("--first-release", action="store_true")
    parser.add_argument("--target", default="anyaicam-appliance")
    parser.add_argument("--channel", default="stable")
    parser.add_argument("--platform", default="ubuntu")
    parser.add_argument("--architecture", default="x86_64", choices=("x86_64", "aarch64"))
    parser.add_argument("--update-id")
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        manifest, embedded = build_manifest(installer=args.installer, previous_installer=args.previous_installer,
                                            first_release=args.first_release, target=args.target, channel=args.channel,
                                            platform=args.platform, architecture=args.architecture,
                                            update_id=args.update_id, now=datetime.now(timezone.utc))
    except release_checks.ReleaseCheckError as error:
        raise SystemExit(f"Refusing to sign: {error.code}: {error}") from error
    signature = sign(manifest, args.signing_key, embedded)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.out_dir / "manifest.sig").write_bytes(base64.b64encode(signature) + b"\n")
    print(json.dumps({"update_id": manifest["update_id"], "version": manifest["version"],
                      "build_id": manifest["build_id"], "sha256": manifest["sha256"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
