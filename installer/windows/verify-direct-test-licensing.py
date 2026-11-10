"""Cryptographically verify the synthetic offline licensing inputs before the service is started."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import sys


def load(path: str) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("fixture must be a JSON object")
    return value


def main(argv: list[str]) -> int:
    if len(argv) != 6:
        raise ValueError("expected identity, feature, accepted-capacity, registry, app-root, and agent-system-root")
    identity_path, feature_path, capacity_path, registry_path, app_root, agent_system_root = argv
    sys.path.insert(0, str(Path(app_root).resolve()))
    sys.path.insert(0, str(Path(agent_system_root).resolve()))

    import appliance_entitlements
    import capacity_accept
    import capacity_accept_windows

    identity, feature_doc = load(identity_path), load(feature_path)
    accepted, registry_doc = load(capacity_path), load(registry_path)
    if registry_doc.get("environment") != "synthetic":
        raise ValueError("test registry is not synthetic")
    registry = appliance_entitlements.TrustContext(appliance_entitlements.parse_trust_registry(registry_doc))
    feature = feature_doc.get("snapshot", feature_doc)
    signature = feature.get("signature") if isinstance(feature, dict) else None
    key_id = signature.get("key_id") if isinstance(signature, dict) else None
    public_key = registry.resolve(key_id, purpose="features", for_new_grant=True,
                                  appliance_id=identity.get("appliance_id"))
    public_keys = {key_id: base64.b64encode(public_key).decode("ascii")}
    refusal = appliance_entitlements.verify(feature, public_keys, identity=identity)
    if refusal:
        raise ValueError(f"synthetic feature entitlement refused: {refusal}")

    capacity_registry = capacity_accept.parse_trust_registry(registry_doc)
    context = capacity_accept.TrustContext(capacity_registry)
    capacity_accept._validate_state(accepted, context, verifier=capacity_accept_windows._verify_ed25519)
    print("synthetic identity, feature signature, accepted-capacity signature, identity binding, and trust purpose: PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        print(f"DIRECT_TEST_LICENSE_FIXTURE_REFUSED:{type(exc).__name__}:{exc}", file=sys.stderr)
        raise SystemExit(1)
