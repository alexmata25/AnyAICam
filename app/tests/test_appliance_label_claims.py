"""Headless label claim (2026-10-07): an appliance shipped without a screen
is claimed with the code printed on its label (appliance_claims.py,
"Headless label claim"). The appliance sends only the code's verifier with
claim/begin; the customer types or scans the code in the portal.

Fixtures and helpers come from test_appliance_claims.py (same throwaway
database, same rate-limiter resets, same claim-flow key)."""

import hashlib
import logging

import pytest

from test_appliance_claims import (  # noqa: F401  (pytest fixtures)
    DEVICE_SECRET, VALID_DEVICE_ID, _customer_cookie, _seeded_customer, _status,
    appliance_claims, claim_flow_key, client, connection, db_path, identity_file, override_target, partner_portal,
)

LABEL_CODE = "7K3M-9QX2-H4TB"
LABEL_COMPACT = "7K3M9QX2H4TB"
OTHER_DEVICE_ID = "22222222-2222-4222-9222-222222222222"


def _verifier(code=LABEL_COMPACT):
    # Exactly what installer/09-identity.sh computes with sha256sum.
    return hashlib.sha256(("anyaicam-label-claim-v1:" + code).encode()).hexdigest()


def _begin(client, device_id=VALID_DEVICE_ID, verifier=None, secret=DEVICE_SECRET):
    body = {"device_secret": secret, "device_id": device_id}
    if verifier is not None:
        body["label_verifier"] = verifier
    return client.post("/api/appliance/claim/begin", json=body)


def _cookies():
    return {partner_portal.SESSION_COOKIE: _customer_cookie()}


def _lookup_label(client, code, cookies=None):
    return client.post("/api/portal/claims/lookup", cookies=cookies or _cookies(), json={"label_code": code})


def _confirm_label(client, code, site_id="site-1", cookies=None):
    return client.post("/api/portal/claims/confirm", cookies=cookies or _cookies(), json={"label_code": code, "site_id": site_id})


# --------------------------------------------------------- the code itself


@pytest.mark.parametrize("typed", ["7K3M-9QX2-H4TB", "7k3m9qx2h4tb", " 7K3M 9QX2 H4TB ", "7K3M-9QX2-H4TB"])
def test_label_code_normalizes_how_people_type_it(typed):
    assert appliance_claims.normalize_label_code(typed) == LABEL_COMPACT


def test_label_code_maps_lookalike_letters():
    # Crockford base32 has no I, L, O, U: a reader's O/I/L means 0/1/1.
    assert appliance_claims.normalize_label_code("O1IL-2345-6789") == "0111" + "23456789"


@pytest.mark.parametrize("bad", ["", "7K3M9QX2H4T", "7K3M9QX2H4TBA", "7K3M9QX2H4TU", "7K3M9QX2H4T!", None])
def test_label_code_rejects_wrong_length_or_characters(bad):
    assert appliance_claims.normalize_label_code(bad) is None


def test_label_verifier_matches_the_installer_formula():
    assert appliance_claims.label_verifier(LABEL_COMPACT) == _verifier()


# --------------------------------------------------------- claim/begin


def test_begin_stores_only_a_hash_of_the_verifier(client, db_path):
    _seeded_customer(db_path)
    assert _begin(client, verifier=_verifier()).status_code == 200
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            stored = db.execute("SELECT label_verifier_hash FROM appliance_claims WHERE device_id=?", (VALID_DEVICE_ID,)).fetchone()[0]
    assert stored and stored.startswith("pbkdf2_sha256$")
    assert _verifier() not in stored and LABEL_COMPACT not in stored


@pytest.mark.parametrize("bad", ["abc", "G" * 64, _verifier()[:-1], _verifier() + "0"])
def test_begin_rejects_a_malformed_verifier(client, db_path, bad):
    _seeded_customer(db_path)
    assert _begin(client, verifier=bad).status_code == 400


def test_begin_without_a_verifier_still_works_for_terminal_claims(client, db_path):
    _seeded_customer(db_path)
    response = _begin(client)
    assert response.status_code == 200 and response.json()["claim_code"]
    assert _lookup_label(client, LABEL_CODE).status_code == 404


def test_resume_adds_the_verifier_to_the_pending_claim(client, db_path):
    _seeded_customer(db_path)
    first = _begin(client).json()
    resumed = _begin(client, verifier=_verifier()).json()
    assert resumed["claim_session_id"] == first["claim_session_id"] and resumed["resumed"] is True
    assert _lookup_label(client, LABEL_CODE).status_code == 200


# --------------------------------------------------------- portal lookup/confirm


def test_lookup_by_label_code_finds_the_waiting_appliance(client, db_path):
    _seeded_customer(db_path)
    _begin(client, verifier=_verifier())
    response = _lookup_label(client, "7k3m 9qx2 h4tb")
    assert response.status_code == 200
    assert response.json()["device_id"] == VALID_DEVICE_ID


def test_lookup_with_a_wrong_label_code_is_404(client, db_path):
    _seeded_customer(db_path)
    _begin(client, verifier=_verifier())
    assert _lookup_label(client, "7K3M-9QX2-H4TC").status_code == 404
    assert _lookup_label(client, "not a code").status_code == 404


def test_label_lookup_matches_only_its_own_appliance(client, db_path):
    _seeded_customer(db_path)
    _begin(client, verifier=_verifier())
    _begin(client, device_id=OTHER_DEVICE_ID, verifier=_verifier("ABCDEFGHJKMN"), secret=DEVICE_SECRET + "-other")
    assert _lookup_label(client, LABEL_CODE).json()["device_id"] == VALID_DEVICE_ID
    assert _lookup_label(client, "ABCD-EFGH-JKMN").json()["device_id"] == OTHER_DEVICE_ID


def test_label_lookup_requires_a_signed_in_customer_owner(client, db_path):
    _seeded_customer(db_path)
    _begin(client, verifier=_verifier())
    assert client.post("/api/portal/claims/lookup", json={"label_code": LABEL_CODE}).status_code == 403
    viewer = {partner_portal.SESSION_COOKIE: partner_portal._token("owner@example.test", "customer_viewer", None, "cust-1")}
    assert _lookup_label(client, LABEL_CODE, cookies=viewer).status_code == 403


def test_an_expired_claim_is_not_found_by_label(client, db_path, monkeypatch):
    _seeded_customer(db_path)
    _begin(client, verifier=_verifier())
    from datetime import datetime, timedelta
    monkeypatch.setattr(appliance_claims, "_now", lambda: datetime.now() + timedelta(minutes=appliance_claims.CLAIM_SESSION_TTL_MINUTES + 1))
    assert _lookup_label(client, LABEL_CODE).status_code == 404


def test_confirm_by_label_rejects_another_customers_site(client, db_path):
    _seeded_customer(db_path)
    from test_appliance_claims import _seed_other_customer_site
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_other_customer_site(db)
    _begin(client, verifier=_verifier())
    assert _confirm_label(client, LABEL_CODE, site_id="site-other").status_code == 403


def test_confirm_needs_a_code(client, db_path):
    _seeded_customer(db_path)
    response = client.post("/api/portal/claims/confirm", cookies=_cookies(), json={"site_id": "site-1"})
    assert response.status_code == 400


def test_full_label_claim_completes_and_never_logs_the_code(client, db_path, caplog):
    caplog.set_level(logging.DEBUG)
    _seeded_customer(db_path)
    session = _begin(client, verifier=_verifier()).json()
    confirm = _confirm_label(client, LABEL_CODE)
    assert confirm.status_code == 200 and confirm.json()["device_id"] == VALID_DEVICE_ID
    assert _confirm_label(client, LABEL_CODE).status_code == 404  # no longer pending
    status = _status(client, session["claim_session_id"]).json()
    assert status["status"] == "claimed" and status["claim_proof"]
    complete = client.post("/api/appliance/claim/complete", json={
        "device_secret": DEVICE_SECRET, "claim_session_id": session["claim_session_id"], "claim_proof": status["claim_proof"]})
    assert complete.status_code == 200
    assert complete.json()["cloud_id"] == VALID_DEVICE_ID.upper()
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (LABEL_CODE, LABEL_COMPACT, _verifier(), status["claim_proof"], complete.json()["credential"]):
        assert secret not in log_text


# --------------------------------------------------------- pages


def test_claim_entry_page_is_public_and_keeps_the_code_out_of_urls(client, db_path):
    response = client.get("/claim")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    body = response.text
    assert "sessionStorage.setItem('anyaicam.claimLabel'" in body
    assert "history.replaceState" in body and "location.replace('/customer/claim-appliance')" in body


def test_claim_page_prefills_from_session_storage_and_sends_label_code(client, db_path):
    _seeded_customer(db_path)
    response = client.get("/customer/claim-appliance", cookies=_cookies())
    assert response.status_code == 200
    body = response.text
    assert "sessionStorage.getItem('anyaicam.claimLabel')" in body
    assert "label_code:compact" in body
    assert "printed on its label" in body
