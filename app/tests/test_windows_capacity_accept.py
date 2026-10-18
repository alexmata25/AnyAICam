"""Windows capacity acceptance policy and ACL fail-closed regressions.

Codex 07e934a, ported onto Red's Windows trust layout: the ACL seam is platform_paths.windows_write_problem() walked
from each file up to %ProgramData%\AnyAiCam (acl_problem returns '' when safe), not a second ACL verifier."""
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

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


def accept(state, snapshot, *, keyset=None):
    paths = state["paths"]
    paths["staging.json"].write_text(json.dumps(snapshot), encoding="utf-8")
    return windows_accept._accept_from_paths(
        {"root": paths["root"], "staging": paths["staging.json"], "identity": paths["identity.json"],
         "trust": paths["keys.json"], "accepted": paths["accepted.json"]},
        keyset=keyset or state["keyset"], acl_problem=lambda *_args, **_kwargs: "",
        verifier=windows_accept._verify_ed25519, now=state["now"])


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
    monkeypatch.setattr(vms_capacity.os, "name", "nt")
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
