"""The panels' seam to Red's backend (e2eec24): software_license.customer_contract(customer_id, *, role).
The page passes the viewer's role; an error inside the backend is not hidden by the local fallback."""
import sys
import types

import pytest

import software_license_panels as panels


def _fake_backend(monkeypatch, behaviour):
    module = types.ModuleType("software_license")
    calls = []

    def customer_contract(customer_id, *, role="customer_owner"):
        calls.append((customer_id, role))
        return behaviour(customer_id, role)
    module.customer_contract = customer_contract
    monkeypatch.setitem(sys.modules, "software_license", module)
    return calls


def test_the_backend_receives_the_viewers_role(monkeypatch):
    calls = _fake_backend(monkeypatch, lambda cid, role: {"role_seen": role})
    assert panels.contract_for("cust-1", role="customer_viewer") == {"role_seen": "customer_viewer"}
    assert panels.contract_for("cust-1") == {"role_seen": "customer_owner"}
    assert calls == [("cust-1", "customer_viewer"), ("cust-1", "customer_owner")]


def test_a_backend_error_is_not_masked_by_the_local_fallback(monkeypatch):
    def broken(cid, role):
        raise ImportError("a missing dependency inside the backend")
    _fake_backend(monkeypatch, broken)
    with pytest.raises(ImportError):
        panels.contract_for("cust-1")
