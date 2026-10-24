"""Windows capacity acceptance policy and ACL fail-closed regressions.

Codex 07e934a, ported onto Red's Windows trust layout: the ACL seam is platform_paths.windows_write_problem() walked
from each file up to %ProgramData%\AnyAiCam (acl_problem returns '' when safe), not a second ACL verifier."""
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import types

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import platform_paths

SYSTEM_DIR = Path(__file__).resolve().parents[2] / "appliance-agent" / "system"
sys.path.insert(0, str(SYSTEM_DIR))
import capacity_accept_windows as windows_accept

IDENTITY = {"appliance_id": "appl-win-test", "cloud_id": "AIC-win-test", "customer_id": "cust-win-test"}


@pytest.fixture
def acceptance(tmp_path):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    keyset = {"keys": {"test-key": base64.b64encode(public).decode("ascii")}}
    paths = {name: tmp_path / name for name in ("staging.json", "identity.json", "keys.json", "accepted.json")}
    paths["root"] = tmp_path
    paths["accepted.json"].parent.mkdir(exist_ok=True)
    paths["identity.json"].write_text(json.dumps(IDENTITY), encoding="utf-8")
    now = datetime.now(timezone.utc)
    state = {"private": private, "keyset": keyset, "paths": paths, "now": now}
    return state


def signed_snapshot(state, *, quantity=5, issued=None, expires=None, changes=None):
    now = state["now"]
    body = {"type": "anyaicam.feature_entitlements", "version": 1, **IDENTITY,
            "features": {},
            "camera_capacity": {"camera_slot_quantity": quantity, "plan_camera_slots": quantity,
                                "vms_license_capacity": quantity},
            "issued_at": (issued or now).isoformat(),
            "expires_at": (expires or now + timedelta(hours=12)).isoformat()}
    body.update(changes or {})
    signature = state["private"].sign(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return {**body, "signature": {"alg": "Ed25519", "key_id": "test-key",
                                   "value": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")}}


def accept(state, snapshot, *, keyset=None, registry=None, trust_state=None, now=None):
    """registry=/trust_state= are the TRUST-1 seams (None = read the files under the test root, absent = legacy)."""
    paths = state["paths"]
    paths["staging.json"].write_text(json.dumps(snapshot), encoding="utf-8")
    return windows_accept._accept_from_paths(
        {"root": paths["root"], "staging": paths["staging.json"], "identity": paths["identity.json"],
         "trust": paths["keys.json"], "accepted": paths["accepted.json"],
         "registry": paths["root"] / "entitlement_trust_registry.json",
         "trust_state": paths["root"] / "entitlement_trust_state.json"},
        keyset=keyset or (None if registry is not None else state["keyset"]), registry=registry,
        trust_state=trust_state, acl_problem=lambda *_args, **_kwargs: "",
        verifier=windows_accept._verify_ed25519, now=now or state["now"])


def test_accepts_valid_cloud_signed_camera_capacity_and_enforces_bound_identity(acceptance):
    assert accept(acceptance, signed_snapshot(acceptance, quantity=7)) == "accepted"
    saved = json.loads(acceptance["paths"]["accepted.json"].read_text(encoding="utf-8"))
    assert saved["snapshot"]["camera_capacity"]["camera_slot_quantity"] == 7
    assert saved["identity"] == IDENTITY


@pytest.mark.parametrize("changes", [
    {"customer_id": "other-customer"},
    {"cloud_id": "other-cloud"},
    {"type": "other-product"},
    {"version": 2},
])
def test_rejects_wrong_identity_or_product_snapshot_type(acceptance, changes):
    with pytest.raises(windows_accept.Reject):
        accept(acceptance, signed_snapshot(acceptance, changes=changes))
    assert not acceptance["paths"]["accepted.json"].exists()


@pytest.mark.parametrize("quantity", [-1, 10_001, True, 5.5, None])
def test_rejects_invalid_or_over_capacity_entitlement(acceptance, quantity):
    snapshot = signed_snapshot(acceptance, changes={"camera_capacity": {"camera_slot_quantity": quantity}})
    with pytest.raises(windows_accept.Reject):
        accept(acceptance, snapshot)


def test_rejects_forged_or_modified_entitlement_and_preserves_prior_limit(acceptance):
    assert accept(acceptance, signed_snapshot(acceptance, quantity=4)) == "accepted"
    forged = signed_snapshot(acceptance, quantity=40)
    forged["camera_capacity"]["camera_slot_quantity"] = 400
    with pytest.raises(windows_accept.Reject):
        accept(acceptance, forged)
    saved = json.loads(acceptance["paths"]["accepted.json"].read_text(encoding="utf-8"))
    assert saved["snapshot"]["camera_capacity"]["camera_slot_quantity"] == 4


def test_rejects_snapshot_signed_by_key_not_in_release_keyset(acceptance):
    outsider = Ed25519PrivateKey.generate()
    outsider_public = outsider.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    with pytest.raises(windows_accept.Reject):
        accept(acceptance, signed_snapshot(acceptance),
               keyset={"keys": {"outsider": base64.b64encode(outsider_public).decode("ascii")}})
    assert not acceptance["paths"]["accepted.json"].exists()


def test_rejects_insecure_acl_before_reading_any_capacity_input(acceptance):
    paths = acceptance["paths"]
    paths["staging.json"].write_text("{}", encoding="utf-8")
    with pytest.raises(windows_accept.Reject, match="unsafe_input_acl"):
        windows_accept._accept_from_paths(
            {"root": paths["root"], "staging": paths["staging.json"], "identity": paths["identity.json"],
             "trust": paths["keys.json"], "accepted": paths["accepted.json"]},
            keyset=acceptance["keyset"], acl_problem=lambda *_args, **_kwargs: "is writable by BU",
            verifier=windows_accept._verify_ed25519, now=acceptance["now"])


def test_rejects_expired_new_snapshot_but_keeps_last_accepted_capacity(acceptance):
    assert accept(acceptance, signed_snapshot(acceptance, quantity=3)) == "accepted"
    expired = signed_snapshot(acceptance, quantity=9,
                              issued=acceptance["now"] - timedelta(days=2),
                              expires=acceptance["now"] - timedelta(days=1))
    with pytest.raises(windows_accept.Reject):
        accept(acceptance, expired)
    saved = json.loads(acceptance["paths"]["accepted.json"].read_text(encoding="utf-8"))
    assert saved["snapshot"]["camera_capacity"]["camera_slot_quantity"] == 3


def test_acl_policy_requires_a_system_or_administrators_owned_store():
    """The same SDDL rule the trust anchor uses (platform_paths.sddl_write_problem)."""
    safe = "O:SYG:SYD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)"
    assert platform_paths.sddl_write_problem(safe) == ""
    assert platform_paths.sddl_write_problem(safe + "(A;OICI;FA;;;BU)") == "is writable by BU"
    assert platform_paths.sddl_write_problem(safe.replace("O:SY", "O:BU")).startswith("is owned by BU")


def test_the_acl_walk_checks_every_directory_up_to_the_data_root(tmp_path, monkeypatch):
    root = tmp_path / "AnyAiCam"
    target = root / "update" / "vms_capacity_accepted.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    seen = []
    monkeypatch.setattr(windows_accept, "_platform_paths", lambda: type("P", (), {
        "windows_write_problem": staticmethod(lambda p: seen.append(p) or "")}))
    assert windows_accept._windows_acl_problem(target, root) == ""
    assert seen == [target, target.parent, root]

    monkeypatch.setattr(windows_accept, "_platform_paths", lambda: type("P", (), {
        "windows_write_problem": staticmethod(lambda p: "is writable by BU" if p == root else "")}))
    assert windows_accept._windows_acl_problem(target, root).endswith("is writable by BU")
    assert windows_accept._windows_acl_problem(tmp_path / "elsewhere.json", root).endswith(f"is outside {root}")


def test_an_unreadable_acl_fails_closed(tmp_path, monkeypatch):
    def boom(_path):
        raise OSError("cannot read the security descriptor")
    monkeypatch.setattr(windows_accept, "_platform_paths", lambda: type("P", (), {"windows_write_problem": staticmethod(boom)}))
    assert "could not be checked" in windows_accept._windows_acl_problem(tmp_path / "x.json", tmp_path)


def test_production_helper_does_not_accept_caller_selected_paths():
    with pytest.raises(TypeError):
        windows_accept.accept_pending(staging_path=Path("untrusted"))


def test_windows_sync_promotes_the_staged_snapshot_only_on_the_real_path(tmp_path, monkeypatch):
    """apply_sync() runs the acceptance helper on Windows for the production staging path; a redirected (test) path
    only stages, exactly as on Linux. A helper failure never raises and keeps the last accepted capacity."""
    import vms_capacity
    calls = []
    monkeypatch.setattr(vms_capacity, "_stage_snapshot", lambda path, snapshot: True)
    monkeypatch.setattr(windows_accept, "accept_pending", lambda: calls.append("accept") or "accepted")
    monkeypatch.setitem(sys.modules, "capacity_accept_windows", windows_accept)
    # only vms_capacity sees Windows: patching the shared os module would make pathlib build WindowsPath on Linux
    monkeypatch.setattr(vms_capacity, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(vms_capacity, "SIGNED_FILE", vms_capacity._DEFAULT_SIGNED_FILE)  # conftest redirects it
    assert vms_capacity.apply_sync({}, {"signature": {}}) == "accepted" and calls == ["accept"]

    monkeypatch.setattr(windows_accept, "accept_pending", lambda: (_ for _ in ()).throw(windows_accept.Reject("unsafe_store_acl")))
    assert vms_capacity.apply_sync({}, {"signature": {}}) == "accept_rejected"

    monkeypatch.setattr(vms_capacity, "SIGNED_FILE", tmp_path / "staging.json")
    assert vms_capacity.apply_sync({}, {"signature": {}}) == "staged"


def test_the_installer_ships_the_acceptance_helper_and_its_policy_core():
    iss = (Path(__file__).resolve().parents[2] / "installer" / "windows" / "AnyAiCam-VMS.iss").read_text(encoding="utf-8-sig")
    bs = chr(92)
    for name in ("capacity_accept.py", "capacity_accept_windows.py"):
        source = bs.join(["..", "..", "appliance-agent", "system", name])
        assert f'Source: "{source}"; DestDir: "{{app}}{bs}agent{bs}system"' in iss


# ------------------------------------------------------------------------------ TRUST-1 + G2 parity (1.2.7.1)
import capacity_accept as policy  # noqa: E402  (the shared Linux policy core the Windows helper delegates to)


def windows_registry(state, entries, generation):
    keys = {}
    for key_id, (public_b64, lifecycle) in entries.items():
        keys[key_id] = {"public_key": public_b64, "state": lifecycle,
                        "purposes": ["analytics", "capacity", "features", "local_access"],
                        "environment": "production", "changed_generation": 1}
    return {"type": "anyaicam.entitlement_trust_registry", "version": 1, "environment": "production",
            "generation": generation, "issued_at": state["now"].isoformat(), "keys": keys}


def second_key():
    private = Ed25519PrivateKey.generate()
    return private, base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw,
                                                                     serialization.PublicFormat.Raw)).decode("ascii")


def resign(private, snapshot, key_id):
    body = {k: v for k, v in snapshot.items() if k != "signature"}
    signature = private.sign(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return {**body, "signature": {"alg": "Ed25519", "key_id": key_id,
                                   "value": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")}}


def test_windows_acceptor_records_signer_and_ratchets_the_trust_state(acceptance):
    current = acceptance["keyset"]["keys"]["test-key"]
    registry = windows_registry(acceptance, {"test-key": (current, "active")}, 1)
    assert accept(acceptance, signed_snapshot(acceptance, quantity=3), registry=registry) == "accepted"
    saved = json.loads(acceptance["paths"]["accepted.json"].read_text(encoding="utf-8"))
    assert saved["signer"]["key_id"] == "test-key" and saved["trust_generation"] == 1
    trust_state = json.loads((acceptance["paths"]["root"] / "entitlement_trust_state.json").read_text())
    assert trust_state["generation"] == 1 and "test-key" in trust_state["key_bindings"]


def test_windows_rotation_retired_keeps_state_refuses_new_old_key_grants_accepts_new_key(acceptance):
    current = acceptance["keyset"]["keys"]["test-key"]
    assert accept(acceptance, signed_snapshot(acceptance, quantity=3),
                  registry=windows_registry(acceptance, {"test-key": (current, "active")}, 1)) == "accepted"
    private2, public2 = second_key()
    rotated = windows_registry(acceptance, {"test-key": (current, "retired"), "next-key": (public2, "active")}, 2)
    later = acceptance["now"] + timedelta(minutes=1)
    with pytest.raises(windows_accept.Reject, match="retired_cannot_sign"):
        accept(acceptance, signed_snapshot(acceptance, quantity=9, issued=later), registry=rotated, now=later)
    newer = resign(private2, signed_snapshot(acceptance, quantity=5, issued=later), "next-key")
    assert accept(acceptance, newer, registry=rotated, now=later) == "accepted"
    saved = json.loads(acceptance["paths"]["accepted.json"].read_text(encoding="utf-8"))
    assert (saved["signer"]["key_id"], saved["snapshot"]["camera_capacity"]["camera_slot_quantity"]) == ("next-key", 5)


def test_windows_revoked_or_rolled_back_registry_never_trusts_new_grants(acceptance):
    current = acceptance["keyset"]["keys"]["test-key"]
    private2, public2 = second_key()
    gen1 = windows_registry(acceptance, {"test-key": (current, "active")}, 1)
    assert accept(acceptance, signed_snapshot(acceptance, quantity=3), registry=gen1) == "accepted"
    gen2 = windows_registry(acceptance, {"test-key": (current, "revoked"), "next-key": (public2, "active")}, 2)
    later = acceptance["now"] + timedelta(minutes=1)
    with pytest.raises(windows_accept.Reject, match="revoked"):
        accept(acceptance, signed_snapshot(acceptance, quantity=9, issued=later), registry=gen2, now=later)
    with pytest.raises(windows_accept.Reject, match="registry_rollback"):  # state is now generation 2
        accept(acceptance, signed_snapshot(acceptance, quantity=9, issued=later), registry=gen1, now=later)
    with pytest.raises(windows_accept.Reject, match="registry_rollback"):  # registry deleted after generation 1
        accept(acceptance, signed_snapshot(acceptance, quantity=9, issued=later), now=later)
    saved = json.loads(acceptance["paths"]["accepted.json"].read_text(encoding="utf-8"))
    assert saved["snapshot"]["camera_capacity"]["camera_slot_quantity"] == 3


def test_windows_clock_rollback_does_not_block_and_a_future_new_grant_never_overwrites(acceptance):
    """G2 parity: a clock behind the accepted grant is not a reason to refuse the accepted state; the new grant that
    looks 'future' is refused and the last-known-good stays."""
    assert accept(acceptance, signed_snapshot(acceptance, quantity=3)) == "accepted"
    behind = acceptance["now"] - timedelta(days=30)
    with pytest.raises(windows_accept.Reject, match="issued_in_future"):
        accept(acceptance, signed_snapshot(acceptance, quantity=9, issued=acceptance["now"] + timedelta(minutes=1)),
               now=behind)
    saved = json.loads(acceptance["paths"]["accepted.json"].read_text(encoding="utf-8"))
    assert saved["snapshot"]["camera_capacity"]["camera_slot_quantity"] == 3
    policy._validate_state(saved, policy.TrustContext(policy.legacy_trust_registry(acceptance["keyset"]["keys"]), None),
                           now=behind, verifier=windows_accept._verify_ed25519)


def test_windows_paths_include_the_registry_and_trust_state(monkeypatch):
    real_os = __import__("os")
    monkeypatch.setattr(windows_accept, "os", types.SimpleNamespace(name="nt", path=real_os.path))
    paths = windows_accept._paths()
    assert paths["registry"].name == "entitlement_trust_registry.json"
    assert paths["trust_state"].name == "entitlement_trust_state.json"
    assert paths["registry"].parent == paths["trust"].parent == paths["accepted"].parent
