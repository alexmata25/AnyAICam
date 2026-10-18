"""Build step (Windows): validate the cloud's entitlement-signing PUBLIC keyset and write its canonical form for Setup.

The same rules as the Linux release build (installer/build_release_installer.read_entitlement_signing_keys):
public keys only, {"keys": {key_id: base64 32-byte Ed25519 key}}, and the canonical bytes must hash to the SHA-256
pinned out of band (docs/entitlement-signing-keys.md). The appliance trusts nothing else, so a Windows install that
ships without it licenses 0 cameras.

Usage: prepare-keyset.py <keyset.json> <expected-sha256> <output.json>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # installer/
from build_release_installer import read_entitlement_signing_keys  # noqa: E402


def main(argv):
    if len(argv) != 3:
        raise SystemExit("usage: prepare-keyset.py <keyset.json> <expected-sha256> <output.json>")
    source, expected, output = argv
    canonical = read_entitlement_signing_keys(source, expected)  # raises SystemExit on any problem
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_bytes(canonical)
    print(f"entitlement keyset OK: {expected}")


if __name__ == "__main__":
    main(sys.argv[1:])
