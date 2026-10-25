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

TRUST-1 (Linux 1.2.7.1): the protected entitlement trust REGISTRY

    python -m entitlement_keyset --trust-registry                       # generation 1: every active cloud key
    python -m entitlement_keyset --trust-registry --previous gen-N.json \
        [--add ID=BASE64] [--retire ID] [--revoke ID]                   # generation N+1

prints the canonical registry and `sha256 <digest>`; the operator passes both to the release builder
(--entitlement-trust-registry / --entitlement-trust-registry-sha256). The tool never drops a key the previous
generation carried (absent = revoked on appliances; explicit --revoke only), never moves a key backwards
(revoked -> anything, retired -> active), never rebinds a key id, and refuses lab/test key ids and other
environments in a production registry. Cloud keys no longer active in the cloud keep their previous state until
the operator retires or revokes them explicitly.
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


_LAB_KEY_ID = re.compile(r"^(lab|test)[._-]", re.IGNORECASE)


def trust_registry(*, previous: dict | None, cloud_keys: dict, add: dict, retire: list, revoke: list,
                   environment: str = "production", issued_at: str | None = None) -> dict:
    """The next registry generation (a document; canonical via appliance_entitlements.canonical_trust_registry).
    SystemExit on any refused change."""
    import datetime
    import appliance_entitlements as trust
    keys, generation = {}, 1
    if previous is not None:
        try:
            parsed = trust.parse_trust_registry(previous)
        except trust.TrustError as error:
            raise SystemExit(f"--previous is not a valid trust registry ({error})") from error
        if parsed["environment"] != environment:
            raise SystemExit(f"--previous is a {parsed['environment']!r} registry, not {environment!r}")
        generation = parsed["generation"] + 1
        keys = {key_id: dict(entry) for key_id, entry in previous["keys"].items()}
    for key_id, value in list(cloud_keys.items()) + list(add.items()):
        _public_key(key_id, value)
        if environment == "production" and _LAB_KEY_ID.match(key_id):
            raise SystemExit(f"key id {key_id!r} is a lab/test key id; it never enters a production registry")
        if key_id in keys:
            if keys[key_id]["public_key"] != value:
                raise SystemExit(f"key id {key_id!r} is already bound to other key material (no rebinding)")
            continue
        keys[key_id] = {"public_key": value, "state": "active", "purposes": list(trust.TRUST_PURPOSES),
                        "environment": environment, "changed_generation": generation}
    for key_id, target in [(key_id, "retired") for key_id in retire] + [(key_id, "revoked") for key_id in revoke]:
        if key_id not in keys:
            raise SystemExit(f"--{'retire' if target == 'retired' else 'revoke'} {key_id!r}: no such key")
        if keys[key_id]["state"] != target:
            keys[key_id]["state"], keys[key_id]["changed_generation"] = target, generation
    document = {"type": trust.TRUST_REGISTRY_TYPE, "version": trust.TRUST_REGISTRY_VERSION,
                "environment": environment, "generation": generation,
                "issued_at": issued_at or datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
                "keys": dict(sorted(keys.items()))}
    try:
        trust.check_trust_registry_transition(previous, document)
        parsed = trust.parse_trust_registry(document)
    except trust.TrustError as error:
        raise SystemExit(f"the next trust registry is refused: {error}") from error
    if not any(entry["state"] == "active" and "capacity" in entry["purposes"] for entry in parsed["keys"].values()):
        raise SystemExit("the next trust registry would have no active capacity key")
    return document


def cloud_public_keys() -> dict:
    import appliance_identity
    from partner_db import connection
    with connection() as db:
        return appliance_identity.active_public_keys(db)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m entitlement_keyset", description=__doc__.split("\n\n")[0])
    parser.add_argument("--add", action="append", default=[], metavar="KEY_ID=BASE64",
                        help="also include this PUBLIC key (a pre-generated next signing key)")
    parser.add_argument("--trust-registry", action="store_true",
                        help="print the next protected trust registry generation instead of a flat keyset (TRUST-1)")
    parser.add_argument("--previous", metavar="REGISTRY.json",
                        help="with --trust-registry: the currently released registry (omit for generation 1)")
    parser.add_argument("--retire", action="append", default=[], metavar="KEY_ID",
                        help="with --trust-registry: mark this key retired (verifies already accepted grants only)")
    parser.add_argument("--revoke", action="append", default=[], metavar="KEY_ID",
                        help="with --trust-registry: mark this key revoked (nothing it signed is trusted)")
    parser.add_argument("--environment", default="production",
                        help="with --trust-registry: the registry environment (default production)")
    args = parser.parse_args(argv)
    if not args.trust_registry and (args.previous or args.retire or args.revoke):
        parser.error("--previous, --retire and --revoke need --trust-registry")
    if args.trust_registry:
        import appliance_entitlements
        add = {}
        for item in args.add:
            key_id, _, value = item.partition("=")
            key_id, value = _public_key(key_id.strip(), value.strip())
            add[key_id] = value
        previous = None
        if args.previous:
            with open(args.previous, encoding="ascii") as handle:
                previous = json.load(handle)
        document = trust_registry(previous=previous, cloud_keys=dict(cloud_public_keys()), add=add,
                                  retire=args.retire, revoke=args.revoke, environment=args.environment)
        data = appliance_entitlements.canonical_trust_registry(document)
        sys.stdout.write(data.decode("ascii"))
        print(f"sha256 {hashlib.sha256(data).hexdigest()}")
        return 0
    keys = dict(cloud_public_keys())
    for item in args.add:
        key_id, _, value = item.partition("=")
        key_id, value = _public_key(key_id.strip(), value.strip())
        if key_id in keys and keys[key_id] != value:
            raise SystemExit(f"key id {key_id!r} is already bound to other key material (no rebinding)")
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
