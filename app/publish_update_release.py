"""Publish one offline-signed Software Update release (2026-10-03).

Runs inside the cloud portal container, which holds the release-signing
PUBLIC key only (ANYAICAM_UPDATE_SIGNING_PUBLIC_KEY_FILE). The files are the
offline signer's output plus the installer it signed:

    python publish_update_release.py --manifest manifest.json --signature manifest.sig \\
        --package anyaicam-appliance-installer-1.2.0-vms-<build12>.tar.gz [--verify-only]

Before anything is stored, updates_storage.verify_release() re-checks the
signature against the public key and the package's SHA-256, size, target and
channel; any mismatch exits non-zero with nothing published. --verify-only
runs exactly those checks and stops. Prints one JSON line and never prints
key material.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys
from pathlib import Path

import updates_storage


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--signature", required=True, type=Path, help="manifest.sig (base64) from the offline signer")
    parser.add_argument("--package", required=True, type=Path, help="the installer tarball the manifest names")
    parser.add_argument("--verify-only", action="store_true", help="run every publish check, store nothing")
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        signature = base64.b64decode(args.signature.read_bytes().strip(), validate=True)
        package = args.package.read_bytes()
    except (OSError, ValueError, binascii.Error) as error:
        print(json.dumps({"status": "refused", "reason": f"unreadable input: {error}"}))
        return 2
    if not isinstance(manifest, dict):
        print(json.dumps({"status": "refused", "reason": "manifest.json is not a JSON object"}))
        return 2
    target, channel = str(manifest.get("target", "")), str(manifest.get("channel", ""))
    try:
        if args.verify_only:
            updates_storage.verify_release(target, channel, manifest=manifest, package_bytes=package, signature=signature)
        else:
            updates_storage.publish_release(target, channel, manifest=manifest, package_bytes=package, signature=signature)
    except (updates_storage.PublishError, ValueError) as error:
        print(json.dumps({"status": "refused", "reason": str(error)}))
        return 1
    print(json.dumps({"status": "verified" if args.verify_only else "published", "target": target, "channel": channel,
                      "update_id": manifest.get("update_id"), "version": manifest.get("version"),
                      "build_id": manifest.get("build_id"), "sha256": manifest.get("sha256")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
