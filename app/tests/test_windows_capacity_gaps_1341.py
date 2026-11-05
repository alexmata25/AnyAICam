"""Windows 1.3.4.1: acceptance behaviour the shared policy suites prove only on Linux."""
import json
import os
from datetime import timedelta
from pathlib import Path
import threading

import pytest

from test_windows_capacity_accept import IDENTITY, accept, acceptance, signed_snapshot, windows_accept  # noqa: F401

INSTALLER = Path(__file__).resolve().parents[2] / "installer" / "windows"


def accepted_bytes(state):
    return state["paths"]["accepted.json"].read_bytes()


def reclaim(state, *, transition):
    target = dict(IDENTITY, appliance_id="appl-win-new", cloud_id="AIC-win-new", customer_id="cust-win-new")
    state["paths"]["identity.json"].write_text(json.dumps(target), encoding="utf-8")
    changes = dict(target)
    if transition:
        changes["identity_transition"] = {
            "schema": 1, "transition_id": "reclaim-0001", "kind": "authorized", "from_identity": IDENTITY,
            "to_identity": target, "reason": "reenrollment", "authorized_at": state["now"].isoformat()}
    return signed_snapshot(state, quantity=16, issued=state["now"] + timedelta(minutes=1), changes=changes)


def test_a_pc_claimed_by_another_account_cannot_replace_the_grant_without_a_signed_transition(acceptance):
    accept(acceptance, signed_snapshot(acceptance, quantity=8))
    kept = accepted_bytes(acceptance)
    with pytest.raises(windows_accept.Reject, match="accepted_identity_changed"):
        accept(acceptance, reclaim(acceptance, transition=False), now=acceptance["now"] + timedelta(minutes=2))
    assert accepted_bytes(acceptance) == kept


def test_a_signed_reenrollment_replaces_the_grant_on_windows(acceptance):
    accept(acceptance, signed_snapshot(acceptance, quantity=8))
    assert accept(acceptance, reclaim(acceptance, transition=True), now=acceptance["now"] + timedelta(minutes=2)) == "accepted"
    saved = json.loads(accepted_bytes(acceptance))
    assert saved["identity"]["appliance_id"] == "appl-win-new" and saved["transition_id"] == "reclaim-0001"
    assert saved["snapshot"]["camera_capacity"]["camera_slot_quantity"] == 16


def test_a_failed_replace_keeps_the_last_grant_and_leaves_no_temporary_file(acceptance, monkeypatch):
    accept(acceptance, signed_snapshot(acceptance, quantity=8))
    kept = accepted_bytes(acceptance)
    newer = signed_snapshot(acceptance, quantity=16, issued=acceptance["now"] + timedelta(minutes=1))

    def refused(*_args):
        raise PermissionError("file in use")

    monkeypatch.setattr(windows_accept.os, "replace", refused)
    with pytest.raises(PermissionError):
        accept(acceptance, newer, now=acceptance["now"] + timedelta(minutes=2))
    assert accepted_bytes(acceptance) == kept
    assert not list(acceptance["paths"]["root"].glob(".accepted.json.*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="the lock is a no-op off Windows")
def test_the_windows_lock_really_holds_a_second_acceptance_back(tmp_path):
    lock_path, order = tmp_path / ".lock", []
    released = threading.Event()

    def second():
        with windows_accept._windows_lock(lock_path):
            order.append("second" if released.is_set() else "second-too-early")

    with windows_accept._windows_lock(lock_path):
        thread = threading.Thread(target=second)
        thread.start()
        thread.join(0.3)
        assert thread.is_alive()
        released.set()
    thread.join(15)
    assert order == ["second"]


@pytest.mark.xfail(strict=True, reason="Known gap LIC-W1: Windows ships the flat keyset, so revocation and rotation "
                                        "cannot reach installed PCs. Owner and Codex decide before release")
def test_windows_setup_installs_the_trust_registry():
    assert "entitlement_trust_registry" in (INSTALLER / "install-runtime.ps1").read_text(encoding="utf-8")
