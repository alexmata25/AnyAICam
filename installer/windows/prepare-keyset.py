"""Build step (Windows): validate the cloud's entitlement-signing PUBLIC keyset and trust registry, and write both for Setup.

The same rules as the Linux release build (installer/build_release_installer.read_entitlement_signing_keys):
public keys only, {"keys": {key_id: base64 32-byte Ed25519 key}}, and the canonical bytes must hash to the SHA-256
pinned out of band (docs/entitlement-signing-keys.md). The appliance trusts nothing else, so a Windows install that
ships without it licenses 0 cameras.

LIC-W1: the same trust registry as Linux (build_release_installer.read_entitlement_trust_registry: never generated for
Windows). A first install has no history, so a registry missing the live key would only show up on a customer PC as
unknown_key and 0 cameras. So every key in the pinned keyset must be in the registry, with the same key material, and
active; and the keyset must be exactly the registry's projection, which is what Setup installs and checks.
A rotation build keeps the old key for verification: its id goes in <retired-key-ids>, comma-separated, and each one
named must be pinned and retired in the registry. Without that a retired key stops the build, so the key the cloud is
signing with can't become unusable for new grants by accident. A revoked key is never allowed: it isn't in the projection.
A production-signed build requires the production registry. The distinct internal-test Azure signing mode requires a
synthetic registry. Synthetic registry material is never accepted by production signing mode.

Usage: prepare-keyset.py <keyset.json> <expected-sha256> <output.json>
                         <registry.json> <expected-registry-sha256> <registry-output.json> <production|any|internal-test>
                         [<retired-key-ids>]
"""
import base64
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # installer/
from build_release_installer import (  # noqa: E402
    _release_checks_module, read_entitlement_signing_keys, read_entitlement_trust_registry)


def check_registry_covers_keyset(canonical, registry_bytes, projection, required_environment, retired=()):
    """Stops the build unless every pinned key is in the registry, fingerprint-matched and active, or retired as named."""
    registry = _release_checks_module().validate_entitlement_trust_registry(registry_bytes)
    if required_environment == "production" and registry["environment"] != "production":
        raise SystemExit(f"--entitlement-trust-registry is for environment {registry['environment']!r}; "
                         "a signed build needs the production registry.")
    if required_environment == "internal-test" and registry["environment"] != "synthetic":
        raise SystemExit(f"--entitlement-trust-registry is for environment {registry['environment']!r}; "
                         "internal-test signing requires a synthetic registry.")
    pinned = json.loads(canonical)["keys"]
    for key_id in retired:
        if key_id not in pinned:
            raise SystemExit(f"--entitlement-retired-key-ids names {key_id!r}, which is not a pinned key.")
    for key_id, value in pinned.items():
        entry = registry["keys"].get(key_id)
        if entry is None:
            raise SystemExit(f"--entitlement-trust-registry omits pinned key {key_id!r}; installed PCs would lose it.")
        if entry["fingerprint"] != hashlib.sha256(base64.b64decode(value)).hexdigest():
            raise SystemExit(f"--entitlement-trust-registry binds pinned key {key_id!r} to other key material.")
        if key_id in retired:
            if entry["state"] != "retired":
                raise SystemExit(f"--entitlement-retired-key-ids names {key_id!r}, but the registry has it as {entry['state']}.")
        elif entry["state"] != "active":
            raise SystemExit(f"--entitlement-trust-registry has pinned key {key_id!r} as {entry['state']}, not active.")
    if projection != canonical:
        raise SystemExit("--entitlement-trust-registry's flat keyset projection is not the pinned keyset.")


def main(argv):
    if len(argv) not in (7, 8) or argv[6] not in ("production", "any", "internal-test"):
        raise SystemExit("usage: prepare-keyset.py <keyset.json> <expected-sha256> <output.json> "
                         "<registry.json> <expected-registry-sha256> <registry-output.json> "
                         "<production|any|internal-test> "
                         "[<retired-key-ids>]")
    source, expected, output, registry_source, registry_expected, registry_output, environment = argv[:7]
    retired = tuple(argv[7].split(",")) if len(argv) == 8 else ()
    canonical = read_entitlement_signing_keys(source, expected)  # raises SystemExit on any problem
    registry_bytes, projection = read_entitlement_trust_registry(registry_source, registry_expected)
    check_registry_covers_keyset(canonical, registry_bytes, projection, environment, retired)
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_bytes(canonical)
    Path(registry_output).parent.mkdir(parents=True, exist_ok=True)
    Path(registry_output).write_bytes(registry_bytes)
    print(f"entitlement keyset OK: {expected}")
    print(f"entitlement trust registry OK: {registry_expected}")


if __name__ == "__main__":
    main(sys.argv[1:])
