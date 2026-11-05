"""LIC-W1: the Windows trust registry install (appliance-agent/system/trust_install_windows.py).

Policy tests on temporary folders with the ACL seam injected; every registry here is SYNTHETIC. Real ACLs and the real
setup are the Sandbox's job. The registry uses a non-production environment because the fixture's signer is "test-key",
an id a production registry refuses."""
import base64
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from test_windows_capacity_accept import acceptance, accept, signed_snapshot, windows_accept  # noqa: F401

SYSTEM_DIR = Path(__file__).resolve().parents[2] / "appliance-agent" / "system"
sys.path.insert(0, str(SYSTEM_DIR))
import capacity_accept as policy
import trust_install_windows as helper

ENVIRONMENT = "synthetic"
ISSUED = "2026-11-01T00:00:00+00:00"


def public_b64(private):
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


OTHER = public_b64(Ed25519PrivateKey.generate())
NEXT = public_b64(Ed25519PrivateKey.generate())


def registry(keys, *, generation=1, environment=ENVIRONMENT, key_purposes=None):
    """keys: {key_id: (public key, state)}; canonical bytes, as the cloud tool prints them."""
    key_purposes = key_purposes or {}
    return policy.canonical_trust_registry({
        "type": policy.TRUST_REGISTRY_TYPE, "version": policy.TRUST_REGISTRY_VERSION, "environment": environment,
        "generation": generation, "issued_at": ISSUED,
        "keys": {key_id: {"public_key": key, "state": state, "purposes": list(key_purposes.get(key_id, ("capacity",))),
                          "environment": environment,
                          "changed_generation": 1} for key_id, (key, state) in keys.items()}})


def projection(data):
    return helper._release_checks().entitlement_keyset_projection(data)


@pytest.fixture
def pc(acceptance, tmp_path):
    """A 1.3.4 PC: flat keyset, no registry, no state."""
    acceptance["key"] = acceptance["keyset"]["keys"]["test-key"]
    acceptance["store"] = {"root": tmp_path, "staging": tmp_path / "staging.json", "identity": tmp_path / "identity.json",
                           "trust": tmp_path / "keys.json", "accepted": tmp_path / "accepted.json",
                           "registry": tmp_path / "entitlement_trust_registry.json",
                           "trust_state": tmp_path / "entitlement_trust_state.json"}
    acceptance["store"]["trust"].write_text(json.dumps(acceptance["keyset"]), encoding="utf-8")
    return acceptance


def with_grant(pc, **kwargs):
    assert accept(pc, signed_snapshot(pc, quantity=8, **kwargs), keyset=pc["keyset"]) == "accepted"
    pc["store"]["staging"].unlink(missing_ok=True)
    return pc


def install(pc, data, *, sha=None, keyset=None):
    return helper._install_from_paths(pc["store"], data, sha or hashlib.sha256(data).hexdigest(),
                                      projection(data) if keyset is None else keyset,
                                      acl_problem=lambda *_args, **_kwargs: "")


def check(pc, data):
    return helper._check_from_paths(pc["store"], data, hashlib.sha256(data).hexdigest())


def files(pc):
    return {name: path.read_bytes() for name, path in pc["store"].items()
            if name in ("trust", "accepted", "registry", "trust_state") and path.exists()}


def refused(pc, data, reason, **kwargs):
    """The reason, from both the early and the full decision, with every file byte-identical after each."""
    before = files(pc)
    if not kwargs:
        with pytest.raises(helper.Reject, match=reason):
            check(pc, data)
        assert files(pc) == before
    with pytest.raises(helper.Reject, match=reason):
        install(pc, data, **kwargs)
    assert files(pc) == before


def state_of(pc):
    return json.loads(pc["store"]["trust_state"].read_bytes())


def accepted_of(pc):
    return json.loads(pc["store"]["accepted"].read_bytes())


def capacity_verdict(pc):
    """What the shared policy says of the accepted grant under the installed files, as the VMS would."""
    trust = windows_accept._trust_context(policy, pc["store"])
    accepted = accepted_of(pc)
    return policy._validate_state(accepted, trust, verifier=windows_accept._verify_ed25519)


# ---------------------------------------------------------------- upgrade
def test_first_install_keeps_a_grant_from_an_active_key_and_migrates_it(pc):
    with_grant(pc)
    assert accepted_of(pc)["trust_generation"] == 0  # what 1.3.4 writes in LEGACY mode
    data = registry({"test-key": (pc["key"], "active")})
    assert check(pc, data) == "installed"
    assert install(pc, data) == ("installed", "migrated")
    saved = accepted_of(pc)
    assert saved["snapshot"]["camera_capacity"]["camera_slot_quantity"] == 8
    assert saved["trust_generation"] == 1 and saved["signer"]["key_id"] == "test-key"
    assert saved["signer"]["public_key_sha256"] == hashlib.sha256(base64.b64decode(pc["key"])).hexdigest()
    assert pc["store"]["registry"].read_bytes() == data
    assert state_of(pc)["registry_sha256"] == hashlib.sha256(data).hexdigest()
    capacity_verdict(pc)


def test_first_install_keeps_a_grant_from_a_retired_key(pc):
    with_grant(pc)
    assert install(pc, registry({"test-key": (pc["key"], "retired"), "next": (NEXT, "active")})) == ("installed", "migrated")
    capacity_verdict(pc)


def test_no_grant_yet_installs_and_touches_no_grant(pc):
    assert install(pc, registry({"test-key": (pc["key"], "active")})) == ("installed", "no_accepted_state")
    assert not pc["store"]["accepted"].exists()


def test_a_fresh_pc_with_nothing_in_the_trust_folder_installs(pc):
    pc["store"]["trust"].unlink()
    assert install(pc, registry({"test-key": (pc["key"], "active")})) == ("installed", "no_accepted_state")


# ---------------------------------------------------------------- accidental exclusion
def test_a_registry_omitting_the_signer_is_refused_not_mistaken_for_a_revocation(pc):
    with_grant(pc)
    refused(pc, registry({"next": (NEXT, "active")}), "signer_absent")
    assert not pc["store"]["registry"].exists() and not pc["store"]["trust_state"].exists()


def test_the_same_id_with_other_key_material_is_refused(pc):
    with_grant(pc)
    refused(pc, registry({"test-key": (OTHER, "active")}), "signer_key_mismatch")


def test_other_key_material_is_refused_even_with_nothing_recorded_to_compare(pc):
    with_grant(pc)
    pc["store"]["trust"].unlink()
    saved = accepted_of(pc)
    for field in ("signer", "accepted_key"):
        del saved[field]
    pc["store"]["accepted"].write_text(json.dumps(dict(saved, schema=1)), encoding="utf-8")
    data = registry({"test-key": (OTHER, "active")})
    before = files(pc)
    assert check(pc, data) == "installed"  # the early run has no signature library: only the full run can tell
    with pytest.raises(helper.Reject, match="signer_key_mismatch"):
        install(pc, data)
    assert files(pc) == before


def test_a_signer_without_the_capacity_purpose_is_refused(pc):
    with_grant(pc)
    refused(pc, registry({"test-key": (pc["key"], "active"), "next": (NEXT, "active")},
                         key_purposes={"test-key": ("features",)}), "signer_purpose_denied")


def test_an_explicit_revocation_installs_and_the_grant_then_reads_as_revoked(pc):
    with_grant(pc)
    kept = accepted_of(pc)
    data = registry({"test-key": (pc["key"], "revoked"), "next": (NEXT, "active")})
    assert check(pc, data) == "installed"
    assert install(pc, data) == ("installed", "accepted_state_untrusted")
    assert accepted_of(pc) == kept  # never rewritten
    assert state_of(pc)["revoked"]["test-key"]
    with pytest.raises(policy.Reject, match="revoked"):
        capacity_verdict(pc)


def test_an_explicit_revocation_with_other_key_material_is_still_refused(pc):
    with_grant(pc)
    refused(pc, registry({"test-key": (OTHER, "revoked"), "next": (NEXT, "active")}), "signer_key_mismatch")


# ---------------------------------------------------------------- anti-rollback
def generations(pc):
    pc["gen2"] = registry({"test-key": (pc["key"], "active"), "next": (NEXT, "retired")}, generation=2)
    install(pc, pc["gen2"])


def test_a_lower_generation_is_refused(pc):
    generations(pc)
    refused(pc, registry({"test-key": (pc["key"], "active"), "next": (NEXT, "retired")}), "registry_rollback")


def test_a_different_document_at_the_same_generation_is_refused(pc):
    generations(pc)
    refused(pc, registry({"test-key": (pc["key"], "active"), "next": (NEXT, "active")}, generation=2), "registry_rollback")


def test_a_dropped_key_is_refused(pc):
    generations(pc)
    refused(pc, registry({"test-key": (pc["key"], "active")}, generation=3), "dropped_binding")


def test_a_dropped_key_is_refused_even_with_the_old_registry_deleted(pc):
    generations(pc)
    pc["store"]["registry"].unlink()
    refused(pc, registry({"test-key": (pc["key"], "active")}, generation=3), "dropped_binding")


def test_a_rebound_key_is_refused(pc):
    generations(pc)
    refused(pc, registry({"test-key": (pc["key"], "active"), "next": (OTHER, "retired")}, generation=3), "binding_mismatch")


def test_a_backwards_lifecycle_move_is_refused(pc):
    generations(pc)
    refused(pc, registry({"test-key": (pc["key"], "active"), "next": (NEXT, "active")}, generation=3), "lifecycle_backwards")


def test_a_revoked_key_never_returns(pc):
    install(pc, registry({"test-key": (pc["key"], "revoked"), "next": (NEXT, "active")}))
    refused(pc, registry({"test-key": (pc["key"], "active"), "next": (NEXT, "active")}, generation=2), "lifecycle_backwards")
    pc["store"]["registry"].unlink()  # with no old registry, the protected state alone still refuses it
    refused(pc, registry({"test-key": (pc["key"], "active"), "next": (NEXT, "active")}, generation=2), "registry_rollback")


def test_another_environment_is_refused(pc):
    install(pc, registry({"test-key": (pc["key"], "active")}))
    refused(pc, registry({"test-key": (pc["key"], "active")}, generation=2, environment="other"), "environment_mismatch")


def test_a_migrated_grant_is_a_generation_floor_even_with_registry_and_state_deleted(pc):
    with_grant(pc)
    generations(pc)
    pc["store"]["registry"].unlink()
    pc["store"]["trust_state"].unlink()
    refused(pc, registry({"test-key": (pc["key"], "active")}), "registry_rollback")


# ---------------------------------------------------------------- repair
def test_the_same_document_is_a_no_op(pc):
    with_grant(pc)
    data = registry({"test-key": (pc["key"], "active")})
    install(pc, data)
    before = files(pc)
    assert check(pc, data) == "unchanged"
    assert install(pc, data) == ("unchanged", "unchanged")
    assert files(pc) == before


def test_a_deleted_registry_is_restored_exactly(pc):
    with_grant(pc)
    data = registry({"test-key": (pc["key"], "active")})
    install(pc, data)
    before = files(pc)
    pc["store"]["registry"].unlink()
    assert check(pc, data) == "restored"
    assert install(pc, data) == ("restored", "unchanged")
    assert files(pc) == before


def test_a_deleted_state_is_rebuilt(pc):
    with_grant(pc)
    data = registry({"test-key": (pc["key"], "active")})
    install(pc, data)
    state = state_of(pc)
    pc["store"]["trust_state"].unlink()
    assert install(pc, data) == ("installed", "unchanged")
    assert state_of(pc) == state


@pytest.mark.parametrize("removed", [("registry",), ("registry", "trust_state")])
def test_without_a_rerun_a_missing_registry_fails_closed_never_legacy(pc, removed):
    with_grant(pc)
    install(pc, registry({"test-key": (pc["key"], "active")}))
    for name in removed:
        pc["store"][name].unlink()
    with pytest.raises(policy.Reject, match="registry_rollback"):
        capacity_verdict(pc)


# ---------------------------------------------------------------- migration
def test_migration_is_idempotent(pc):
    with_grant(pc)
    data = registry({"test-key": (pc["key"], "active")})
    install(pc, data)
    kept = pc["store"]["accepted"].read_bytes()
    assert install(pc, data)[1] == "unchanged"
    assert pc["store"]["accepted"].read_bytes() == kept


def test_a_later_generation_raises_the_grants_floor(pc):
    with_grant(pc)
    install(pc, registry({"test-key": (pc["key"], "active")}))
    assert install(pc, registry({"test-key": (pc["key"], "active")}, generation=2))[1] == "migrated"
    assert accepted_of(pc)["trust_generation"] == 2


@pytest.mark.parametrize("corrupt", [b"not json", b"[]", b'{"snapshot": {}}'])
def test_a_corrupt_grant_is_never_rewritten_and_does_not_block_the_registry(pc, corrupt):
    pc["store"]["accepted"].write_bytes(corrupt)
    assert install(pc, registry({"test-key": (pc["key"], "active")})) == ("installed", "accepted_state_untrusted")
    assert pc["store"]["accepted"].read_bytes() == corrupt


def test_a_tampered_grant_is_never_rewritten(pc):
    with_grant(pc)
    saved = accepted_of(pc)
    saved["snapshot"]["camera_capacity"]["camera_slot_quantity"] = 800
    pc["store"]["accepted"].write_text(json.dumps(saved), encoding="utf-8")
    kept = pc["store"]["accepted"].read_bytes()
    assert install(pc, registry({"test-key": (pc["key"], "active")}))[1] == "accepted_state_untrusted"
    assert pc["store"]["accepted"].read_bytes() == kept


# ---------------------------------------------------------------- fail closed
def test_a_hash_mismatch_is_refused(pc):
    data = registry({"test-key": (pc["key"], "active")})
    refused(pc, data, "registry_hash_mismatch", sha="0" * 64)


@pytest.mark.parametrize("data", [b"not json", b"{}", b'{"keys": "-----BEGIN PRIVATE KEY-----"}'])
def test_a_malformed_registry_is_refused(pc, data):
    refused(pc, data, "invalid_registry", keyset=b"")


def test_a_registry_not_in_canonical_form_is_refused(pc):
    data = registry({"test-key": (pc["key"], "active")}) + b"\n"
    refused(pc, data, "invalid_registry", keyset=b"")


def test_a_lab_key_in_a_production_registry_is_refused(pc):
    data = registry({"test-key": (pc["key"], "active")}, environment="production")
    refused(pc, data, "invalid_registry", keyset=b"")


def test_a_flat_keyset_that_is_not_the_projection_is_refused(pc):
    data = registry({"test-key": (pc["key"], "active")})
    refused(pc, data, "keyset_not_projection", keyset=b'{"keys": {}}\n')


def test_an_unreadable_trust_state_is_refused(pc):
    pc["store"]["trust_state"].write_bytes(b"not json")
    refused(pc, registry({"test-key": (pc["key"], "active")}), "invalid_trust_state")


@pytest.mark.parametrize("linked", ["accepted", "registry", "trust_state", "trust"])
def test_a_hard_linked_trust_file_is_refused_by_both_runs(pc, tmp_path, linked):
    with_grant(pc)
    data = registry({"test-key": (pc["key"], "active")})
    install(pc, data)
    os.link(pc["store"][linked], tmp_path / "second-name")
    refused(pc, registry({"test-key": (pc["key"], "active")}, generation=2), "hard_linked_trust_file")
    refused(pc, data, "hard_linked_trust_file")  # also a repair, which otherwise returns before reading these


@pytest.mark.parametrize("unsafe", ["accepted", "registry", "trust_state", "trust"])
def test_an_unsafe_acl_is_refused_before_anything_is_read(pc, unsafe):
    with_grant(pc)
    data = registry({"test-key": (pc["key"], "active")})
    install(pc, data)
    before = files(pc)
    with pytest.raises(helper.Reject, match="unsafe_store_acl"):
        helper._install_from_paths(pc["store"], data, hashlib.sha256(data).hexdigest(), projection(data),
                                   acl_problem=lambda path, *_a, **_k: "bad" if path == pc["store"][unsafe] else "")
    assert files(pc) == before


@pytest.mark.parametrize("argv", [[], ["a"], ["a", "0" * 64], ["--check", "a"], ["--check", "a", "b"], ["a", "b", "c"],
                                  ["--other", "0" * 64, "c"], ["a", "0" * 64, "c", "d"]])
def test_the_command_line_takes_only_the_fixed_arguments(argv):
    assert helper.main(argv) == 2


@pytest.mark.skipif(os.name != "nt", reason="the command line is Windows only")
def test_a_refusal_exits_3_from_the_command_line(tmp_path):
    missing = str(tmp_path / "missing.json")
    assert helper.main(["--check", missing, "0" * 64]) == helper.REFUSED_EXIT
    assert helper.main([missing, "0" * 64, missing]) == helper.REFUSED_EXIT


@pytest.mark.skipif(os.name != "nt", reason="the command line is Windows only")
def test_an_unexpected_failure_is_never_exit_0_or_refused(pc, tmp_path, monkeypatch):
    """Setup's early check tells these apart: anything but 0 stops it, and only 3 is reported as a key mismatch."""
    data = registry({"test-key": (pc["key"], "active")})
    incoming = tmp_path / "incoming.json"
    incoming.write_bytes(data)
    monkeypatch.setattr(helper.base, "_paths", lambda: pc["store"])

    def boom(*_args):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(helper, "_check_from_paths", boom)
    assert helper.main(["--check", str(incoming), hashlib.sha256(data).hexdigest()]) == 1


def test_the_early_check_runs_with_only_the_files_setup_extracts(pc, tmp_path):
    """--check runs from {tmp} beside these four files: no agent package, and importing it must need no third party."""
    folder = tmp_path / "setup-tmp"
    folder.mkdir()
    for name in ("trust_install_windows.py", "capacity_accept_windows.py", "capacity_accept.py"):
        shutil.copy(SYSTEM_DIR / name, folder / name)
    shutil.copy(SYSTEM_DIR.parents[1] / "app" / "platform_paths.py", folder / "platform_paths.py")
    source = ("import sys; sys.modules['cryptography'] = None; sys.path.insert(0, sys.argv[1]);"
              "import trust_install_windows as h; import platform_paths; print(h._policy().__file__)")
    result = subprocess.run([sys.executable, "-B", "-c", source, str(folder)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).parent == folder


# ---------------------------------------------------------------- crash
def test_a_crash_between_registry_and_state_converges_on_rerun(pc, monkeypatch):
    with_grant(pc)
    data = registry({"test-key": (pc["key"], "active")})
    real = windows_accept._atomic_replace

    def fails_on_state(path, payload):
        if path == pc["store"]["trust_state"]:
            raise PermissionError("file in use")
        real(path, payload)

    monkeypatch.setattr(windows_accept, "_atomic_replace", fails_on_state)
    with pytest.raises(PermissionError):
        install(pc, data)
    assert pc["store"]["registry"].read_bytes() == data and not pc["store"]["trust_state"].exists()
    monkeypatch.setattr(windows_accept, "_atomic_replace", real)
    assert install(pc, data) == ("installed", "migrated")
    assert state_of(pc)["registry_sha256"] == hashlib.sha256(data).hexdigest()


def test_a_crash_before_the_grant_migrates_converges_on_rerun(pc, monkeypatch):
    with_grant(pc)
    data = registry({"test-key": (pc["key"], "active")})
    real = windows_accept._atomic_replace

    def fails_on_accepted(path, payload):
        if path == pc["store"]["accepted"]:
            raise PermissionError("file in use")
        real(path, payload)

    monkeypatch.setattr(windows_accept, "_atomic_replace", fails_on_accepted)
    with pytest.raises(PermissionError):
        install(pc, data)
    monkeypatch.setattr(windows_accept, "_atomic_replace", real)
    assert install(pc, data) == ("unchanged", "migrated")


# ---------------------------------------------------------------- parity with the Linux routine
def vectors(key):
    first = registry({"test-key": (key, "active")})
    keys = {"test-key": (key, "active"), "next": (NEXT, "retired")}
    return {
        "first install": (None, first, "installed"),
        "same document": (first, first, "unchanged"),
        "next generation": (first, registry({"test-key": (key, "active")}, generation=2), "installed"),
        "lower generation": (registry(keys, generation=2), registry(keys, generation=1), "registry_rollback"),
        "same generation, other document": (first, registry({"test-key": (key, "retired"), "next": (NEXT, "active")}), "registry_rollback"),
        "dropped key": (registry(keys), registry({"test-key": (key, "active")}, generation=2), "dropped_binding"),
        "rebound key": (first, registry({"test-key": (OTHER, "active")}, generation=2), "binding_mismatch"),
        "backwards": (registry(keys), registry({"test-key": (key, "active"), "next": (NEXT, "active")}, generation=2), "lifecycle_backwards"),
    }


@pytest.mark.parametrize("name", list(vectors(OTHER)))
def test_the_same_vectors_give_the_same_outcome_as_the_linux_routine(pc, tmp_path, name):
    apply_release = pytest.importorskip("apply_release")
    start, incoming, expected = vectors(pc["key"])[name]
    linux_dir = tmp_path / "linux"
    linux_dir.mkdir()
    linux_registry, linux_state = linux_dir / "registry.json", linux_dir / "state.json"

    def linux():
        return apply_release.install_trust_registry(linux_registry, linux_state, incoming, posix=False)

    if start is not None:
        apply_release.install_trust_registry(linux_registry, linux_state, start, posix=False)
        install(pc, start)
    if expected in ("installed", "unchanged"):
        assert linux() == expected
        assert install(pc, incoming)[0] == expected
        assert pc["store"]["trust_state"].read_bytes() == linux_state.read_bytes()
    else:
        with pytest.raises(apply_release.TrustAnchorError, match=expected):
            linux()
        with pytest.raises(helper.Reject, match=expected):
            install(pc, incoming)


def test_a_newer_active_grant_recovers_a_revoked_pc(pc):
    with_grant(pc)
    install(pc, registry({"test-key": (pc["key"], "revoked"), "next": (NEXT, "active")}))
    private = Ed25519PrivateKey.generate()
    install(pc, registry({"test-key": (pc["key"], "revoked"), "next": (NEXT, "active"),
                          "new": (public_b64(private), "active")}, generation=2))
    pc["private"] = private
    snapshot = signed_snapshot(pc, quantity=9, issued=pc["now"] + timedelta(minutes=1))
    snapshot["signature"] = dict(snapshot["signature"], key_id="new")
    pc["store"]["staging"].write_text(json.dumps(snapshot), encoding="utf-8")
    outcome = windows_accept._accept_from_paths(pc["store"], acl_problem=lambda *_a, **_k: "",
                                                verifier=windows_accept._verify_ed25519, now=pc["now"] + timedelta(minutes=2))
    assert outcome == "accepted"
    capacity_verdict(pc)
