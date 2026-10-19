"""Synthetic, TEST-ONLY signed capacity snapshots for the Windows Sandbox acceptance (Red 2026-10-18, Codex 21:07).
NEVER for production: signed with the synthetic Sandbox key (make-synthetic-keyset.py), never with a cloud key, and
bound to the Sandbox's own test-cloud identity -- no customer data exists anywhere in the Sandbox.

Run with the INSTALLED app's bundled Python, so the body and signature are made by the app's own
appliance_identity.canonical_json/sign_body (exactly what the cloud uses):

    python synthetic-snapshots.py --app "C:\\Program Files\\AnyAiCam\\app" --key signing-key.json
        --identity C:\\ProgramData\\AnyAiCam\\recordings\\appliance_identity.json
        --case <valid|tampered|expired|unknown-key|wrong-binding|replay|future|selfcheck>
        [--capacity N] [--issued-offset-seconds S] --out <staging file>

Each case writes ONE snapshot (the staging file the VMS writes after a cloud sync) and prints its issued_at.
"""
import argparse, base64, datetime, json, os, sys
from pathlib import Path


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--app", required=True)
    a.add_argument("--key", required=True)
    a.add_argument("--identity")
    a.add_argument("--case", required=True,
                   choices=["valid", "tampered", "expired", "unknown-key", "wrong-binding", "replay", "future", "selfcheck"])
    a.add_argument("--capacity", type=int, default=2)
    a.add_argument("--issued-offset-seconds", type=int, default=0)
    a.add_argument("--out")
    args = a.parse_args()
    sys.path.insert(0, args.app)
    import appliance_identity
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = json.loads(Path(args.key).read_text(encoding="ascii"))
    if not key["key_id"].startswith("sandbox-test-"):
        sys.exit("REFUSED: not a synthetic sandbox-test key")
    private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(key["private_key_b64"]))
    if base64.b64encode(private.public_key().public_bytes_raw()).decode() != key["public_key_b64"]:
        sys.exit("REFUSED: synthetic key pair is inconsistent")
    if args.case == "selfcheck":
        sig = appliance_identity.sign_body({"probe": 1}, key_id=key["key_id"], private_key_b64=key["private_key_b64"])
        appliance_identity.verify_signed_body({"probe": 1}, sig, public_keys={key["key_id"]: key["public_key_b64"]})
        print(f"synthetic key {key['key_id']} signs and verifies")
        return
    ident = json.loads(Path(args.identity).read_text(encoding="utf-8-sig"))
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=args.issued_offset_seconds)
    issued, expires = now, now + datetime.timedelta(hours=24)
    if args.case == "expired":
        issued, expires = now - datetime.timedelta(hours=48), now - datetime.timedelta(hours=24)
    elif args.case == "replay":
        issued, expires = now - datetime.timedelta(hours=1), now + datetime.timedelta(hours=23)
    elif args.case == "future":
        issued, expires = now + datetime.timedelta(hours=1), now + datetime.timedelta(hours=25)
    body = {"type": "anyaicam.feature_entitlements", "version": 1,
            "appliance_id": str(ident["appliance_id"]), "cloud_id": str(ident["cloud_id"]),
            "customer_id": str(ident["customer_id"]),
            "features": {"talk_down": False, "voice_call": False},
            "camera_capacity": {"camera_slot_quantity": args.capacity, "plan_camera_slots": args.capacity,
                                "vms_license_capacity": args.capacity},
            "issued_at": issued.isoformat(), "expires_at": expires.isoformat()}
    if args.case == "wrong-binding":
        body["appliance_id"] = "not-this-appliance"
    key_id, private_b64 = key["key_id"], key["private_key_b64"]
    if args.case == "unknown-key":
        other = Ed25519PrivateKey.generate()
        key_id = "sandbox-unknown-key"
        private_b64 = base64.b64encode(other.private_bytes_raw()).decode()
    snapshot = {**body, "signature": appliance_identity.sign_body(body, key_id=key_id, private_key_b64=private_b64)}
    if args.case == "tampered":
        snapshot["camera_capacity"]["camera_slot_quantity"] = args.capacity + 48   # edited AFTER signing
    out = Path(args.out)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(snapshot), encoding="utf-8")
    os.replace(tmp, out)
    print(f"{args.case}: issued_at {body['issued_at']}")


if __name__ == "__main__":
    main()
