"""The entitlement-signing PUBLIC keyset and its digest, computed on the cloud
host (2026-10-05, Codex review of 5c1556e: the release builder must verify
the keyset against an independently obtained digest).

Run inside the cloud container, over the operator's authenticated admin
channel (SSH/SSM), never through the public API:

    python -m entitlement_keyset                     # the cloud's active public keys
    python -m entitlement_keyset --add ID=BASE64     # plus a pre-generated next key (rotation)

It prints the canonical keyset and `sha256 <digest>`. The digest is what the
release operator passes to installer/build_release_installer.py
(--entitlement-signing-keys-sha256); the builder refuses a keyset whose
canonical SHA-256 differs. Reads public keys only (appliance_identity.
active_public_keys); never reads, prints or writes private key material.

canonical() must stay byte-identical to installer/build_release_installer.py
and appliance-agent/anyaicam_agent/updater/release_checks.py (tests compare).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys

_KEY_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def canonical(keys: dict) -> bytes:
    return (json.dumps({"keys": dict(sorted(keys.items()))}, sort_keys=True, indent=2) + "\n").encode("ascii")


def digest(keys: dict) -> str:
    return hashlib.sha256(canonical(keys)).hexdigest()


def _public_key(key_id: str, value: str) -> tuple[str, str]:
    if not _KEY_ID.match(key_id):
        raise SystemExit(f"malformed key id {key_id!r}")
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise SystemExit(f"key {key_id!r} is not base64") from error
    if len(raw) != 32:
        raise SystemExit(f"key {key_id!r} is not a 32-byte Ed25519 public key")
    return key_id, value


def cloud_public_keys() -> dict:
    import appliance_identity
    from partner_db import connection
    with connection() as db:
        return appliance_identity.active_public_keys(db)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m entitlement_keyset", description=__doc__.split("\n\n")[0])
    parser.add_argument("--add", action="append", default=[], metavar="KEY_ID=BASE64",
                        help="also include this PUBLIC key (a pre-generated next signing key)")
    args = parser.parse_args(argv)
    keys = dict(cloud_public_keys())
    for item in args.add:
        key_id, _, value = item.partition("=")
        key_id, value = _public_key(key_id.strip(), value.strip())
        keys[key_id] = value
    if not keys:
        print("This cloud has no active entitlement-signing key.", file=sys.stderr)
        return 1
    for key_id, value in keys.items():
        _public_key(key_id, value)
    sys.stdout.write(canonical(keys).decode("ascii"))
    print(f"sha256 {digest(keys)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
