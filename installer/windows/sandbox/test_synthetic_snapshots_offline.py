"""Offline proof (Linux container, before any Sandbox run) that every synthetic-snapshots.py case is accepted or
refused by the REAL acceptance policy (appliance-agent/system/capacity_accept._advance) with exactly the reason code
acceptance-127.ps1 expects. Synthetic key only; no network. Usage (repo root as cwd):
    python installer/windows/sandbox/test_synthetic_snapshots_offline.py <signing-key.json>"""
import base64, json, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "appliance-agent" / "system"), str(ROOT / "app")]
import capacity_accept  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: E402

SIGNER = Path(__file__).with_name("synthetic-snapshots.py")
key_file = Path(sys.argv[1])
key = json.loads(key_file.read_text())
public_keys = {key["key_id"]: base64.b64decode(key["public_key_b64"])}
identity = {"appliance_id": "app-1", "cloud_id": "CLOUD-1", "customer_id": "test-customer"}
work = Path(tempfile.mkdtemp())
(work / "identity.json").write_text(json.dumps(identity))


def verifier(public_key, message, signature):
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message); return True
    except Exception:
        return False


def make(case, capacity=9, offset=0):
    out = work / f"{case}.json"
    r = subprocess.run([sys.executable, str(SIGNER), "--app", str(ROOT / "app"), "--key", str(key_file), "--identity",
                        str(work / "identity.json"), "--case", case, "--capacity", str(capacity),
                        "--issued-offset-seconds", str(offset), "--out", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(out.read_text())


ok = []
def check(name, cond, detail=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))


def outcome(snapshot, state):
    try:
        new_state, label = capacity_accept._advance(snapshot, public_keys, identity, state, verifier=verifier)
        return label, new_state   # _advance returns (state record, outcome)
    except capacity_accept.Reject as e:
        return str(e), state


r = subprocess.run([sys.executable, str(SIGNER), "--app", str(ROOT / "app"), "--key", str(key_file), "--case", "selfcheck"],
                   capture_output=True, text=True)
check("synthetic key pair self-check (sign + verify)", r.returncode == 0 and "signs and verifies" in r.stdout, r.stdout + r.stderr)
code, state = outcome(make("valid", 2), None)
check("valid synthetic snapshot (capacity 2) is accepted first", code == "accepted" and state is not None, code)
for case, expected in [("tampered", "bad_signature"), ("unknown-key", "unknown_key"), ("wrong-binding", "identity_mismatch"),
                       ("expired", "expired"), ("future", "issued_in_future"), ("replay", "replay")]:
    code, after = outcome(make(case), state)
    check(f"{case}: refused with {expected}, accepted state unchanged", code == expected and after is state, code)
code, newer = outcome(make("valid", 3, offset=5), state)
check("a newer valid snapshot (capacity 3) is accepted", code == "accepted" and isinstance(newer, dict), code)
print(f"SYNTHETIC SNAPSHOTS OFFLINE TEST {sum(ok)}/{len(ok)} passed")
raise SystemExit(0 if all(ok) else 1)
