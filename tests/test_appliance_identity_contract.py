"""Pure-logic regression coverage for the appliance identity contract
v1 (appliance_identity.py) -- the governing rule under test throughout:
a grant must explicitly resolve to an appliance's own scope; matching
partner_id alone is never sufficient. See appliance_identity.py's
module docstring for the full design.
"""
import base64
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import appliance_identity as ai  # noqa: E402


def _keypair():
    key = ai.Ed25519PrivateKey.generate()
    priv_b64 = base64.b64encode(key.private_bytes_raw()).decode()
    pub_b64 = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    return priv_b64, pub_b64


# =============================================================== grant_resolves


def test_global_grant_resolves_to_any_appliance():
    assert ai.grant_resolves(scope_type="global", scope_id=None, partner_id="p1", customer_id="c1", site_id="s1", cloud_id="AIC-1")


def test_partner_scope_resolves_only_to_its_own_partner():
    assert ai.grant_resolves(scope_type="partner", scope_id="p1", partner_id="p1", customer_id="c1", site_id="s1", cloud_id="AIC-1")
    assert not ai.grant_resolves(scope_type="partner", scope_id="p2", partner_id="p1", customer_id="c1", site_id="s1", cloud_id="AIC-1")


def test_appliance_scope_never_resolves_from_partner_id_match_alone():
    # The governing correction, tested directly at the primitive level.
    assert not ai.grant_resolves(scope_type="appliance", scope_id="AIC-2", partner_id="p1", customer_id="c1", site_id="s1", cloud_id="AIC-1")
    assert ai.grant_resolves(scope_type="appliance", scope_id="AIC-1", partner_id="p1", customer_id="c1", site_id="s1", cloud_id="AIC-1")


def test_site_and_customer_scope_are_exact_matches():
    assert ai.grant_resolves(scope_type="site", scope_id="s1", partner_id="p1", customer_id="c1", site_id="s1", cloud_id="AIC-1")
    assert not ai.grant_resolves(scope_type="site", scope_id="s2", partner_id="p1", customer_id="c1", site_id="s1", cloud_id="AIC-1")
    assert ai.grant_resolves(scope_type="customer", scope_id="c1", partner_id="p1", customer_id="c1", site_id="s1", cloud_id="AIC-1")
    assert not ai.grant_resolves(scope_type="customer", scope_id="c2", partner_id="p1", customer_id="c1", site_id="s1", cloud_id="AIC-1")


# =============================================================== manifest identity building (2 appliances, different users)


def _user(user_id, email, authorization_version=1):
    return {"id": user_id, "email": email, "approved": 1, "account_status": "active", "authorization_version": authorization_version}


def test_two_appliances_same_partner_have_different_authorized_users():
    users = [_user("u-admin", "amata@anyaicam.com"), _user("u-tech", "tech@example.com")]
    grants = {
        "u-admin": [{"role": "administrator", "scope_type": "global", "scope_id": None, "revoked_at": None}],
        "u-tech": [{"role": "technician", "scope_type": "appliance", "scope_id": "AIC-A", "revoked_at": None}],
    }
    manifest_a = ai.build_manifest_identities(users, grants, partner_id="p1", customer_id="c1", site_id="s1", cloud_id="AIC-A")
    manifest_b = ai.build_manifest_identities(users, grants, partner_id="p1", customer_id="c1", site_id="s2", cloud_id="AIC-B")
    emails_a = {i["email"] for i in manifest_a}
    emails_b = {i["email"] for i in manifest_b}
    assert emails_a == {"amata@anyaicam.com", "tech@example.com"}  # global admin + this appliance's own technician
    assert emails_b == {"amata@anyaicam.com"}  # global admin only -- the technician's grant doesn't reach appliance B


def test_partner_cannot_access_another_partners_appliance():
    users = [_user("u-owner", "owner@partner-x.com")]
    grants = {"u-owner": [{"role": "partner_owner", "scope_type": "partner", "scope_id": "partner-x", "revoked_at": None}]}
    own_appliance = ai.build_manifest_identities(users, grants, partner_id="partner-x", customer_id="c1", site_id="s1", cloud_id="AIC-X")
    other_partners_appliance = ai.build_manifest_identities(users, grants, partner_id="partner-y", customer_id="c9", site_id="s9", cloud_id="AIC-Y")
    assert len(own_appliance) == 1
    assert other_partners_appliance == []


def test_customer_boundary_never_leaks_to_a_sibling_customer_under_the_same_partner():
    users = [_user("u-cust", "owner@customer-1.com")]
    grants = {"u-cust": [{"role": "customer_owner", "scope_type": "customer", "scope_id": "cust-1", "revoked_at": None}]}
    own_customer_appliance = ai.build_manifest_identities(users, grants, partner_id="p1", customer_id="cust-1", site_id="s1", cloud_id="AIC-1")
    sibling_customer_appliance = ai.build_manifest_identities(users, grants, partner_id="p1", customer_id="cust-2", site_id="s2", cloud_id="AIC-2")
    assert len(own_customer_appliance) == 1
    assert sibling_customer_appliance == []


def test_technician_assignment_boundary_is_per_appliance_not_per_site_by_default():
    users = [_user("u-tech", "tech@partner.com")]
    grants = {"u-tech": [{"role": "technician", "scope_type": "appliance", "scope_id": "AIC-1", "revoked_at": None}]}
    assigned = ai.build_manifest_identities(users, grants, partner_id="p1", customer_id="c1", site_id="s1", cloud_id="AIC-1")
    unassigned = ai.build_manifest_identities(users, grants, partner_id="p1", customer_id="c1", site_id="s1", cloud_id="AIC-2")
    assert len(assigned) == 1
    assert unassigned == []


def test_a_user_with_zero_resolving_grants_is_omitted_not_included_empty():
    users = [_user("u1", "nobody@example.com")]
    grants = {"u1": [{"role": "technician", "scope_type": "appliance", "scope_id": "AIC-OTHER", "revoked_at": None}]}
    manifest = ai.build_manifest_identities(users, grants, partner_id="p1", customer_id="c1", site_id="s1", cloud_id="AIC-1")
    assert manifest == []


def test_revoked_grant_is_excluded():
    users = [_user("u1", "person@example.com")]
    grants = {"u1": [{"role": "technician", "scope_type": "appliance", "scope_id": "AIC-1", "revoked_at": "2026-08-01T00:00:00"}]}
    manifest = ai.build_manifest_identities(users, grants, partner_id="p1", customer_id="c1", site_id="s1", cloud_id="AIC-1")
    assert manifest == []


def test_disabled_or_suspended_account_still_appears_but_is_flagged_not_enabled():
    users = [_user("u1", "suspended@example.com")]
    users[0]["account_status"] = "suspended"
    grants = {"u1": [{"role": "technician", "scope_type": "appliance", "scope_id": "AIC-1", "revoked_at": None}]}
    manifest = ai.build_manifest_identities(users, grants, partner_id="p1", customer_id="c1", site_id="s1", cloud_id="AIC-1")
    assert manifest[0]["enabled"] is False


# =============================================================== manifest_version_for


def test_manifest_version_is_the_max_authorization_version_among_included_identities():
    identities = [{"authorization_version": 3}, {"authorization_version": 9}, {"authorization_version": 1}]
    assert ai.manifest_version_for(identities) == 9


def test_manifest_version_is_zero_for_an_empty_manifest():
    assert ai.manifest_version_for([]) == 0


# =============================================================== signing / verification


def test_signature_round_trips():
    priv, pub = _keypair()
    body = {"a": 1, "identities": [{"email": "x@example.com"}]}
    sig = ai.sign_body(body, key_id="k1", private_key_b64=priv)
    ai.verify_signed_body(body, sig, public_keys={"k1": pub})  # does not raise


def test_tampered_body_fails_verification():
    priv, pub = _keypair()
    body = {"a": 1}
    sig = ai.sign_body(body, key_id="k1", private_key_b64=priv)
    try:
        ai.verify_signed_body({"a": 2}, sig, public_keys={"k1": pub})
        assert False, "tampering was not detected"
    except ai.ManifestError:
        pass


def test_unknown_key_id_is_rejected():
    priv, pub = _keypair()
    body = {"a": 1}
    sig = ai.sign_body(body, key_id="k1", private_key_b64=priv)
    try:
        ai.verify_signed_body(body, sig, public_keys={"some-other-key": pub})
        assert False
    except ai.ManifestError:
        pass


def test_expired_manifest_is_rejected():
    priv, pub = _keypair()
    now = datetime.now()
    body = {
        "manifest_version": 1, "issued_at": (now - timedelta(hours=2)).isoformat(), "expires_at": (now - timedelta(hours=1)).isoformat(),
        "appliance": {"cloud_id": "AIC-1", "partner_id": "p1", "customer_id": "c1", "site_id": "s1"}, "identities": [],
    }
    body["signature"] = ai.sign_body(body, key_id="k1", private_key_b64=priv)
    try:
        ai.verify_manifest(body, expected_cloud_id="AIC-1", public_keys={"k1": pub}, now=now)
        assert False, "expired manifest was accepted"
    except ai.ManifestError:
        pass


def test_manifest_for_a_different_appliance_is_rejected_if_replayed_here():
    priv, pub = _keypair()
    now = datetime.now()
    body = {
        "manifest_version": 1, "issued_at": now.isoformat(), "expires_at": (now + timedelta(hours=1)).isoformat(),
        "appliance": {"cloud_id": "AIC-OTHER", "partner_id": "p1", "customer_id": "c1", "site_id": "s1"}, "identities": [],
    }
    body["signature"] = ai.sign_body(body, key_id="k1", private_key_b64=priv)
    try:
        ai.verify_manifest(body, expected_cloud_id="AIC-MINE", public_keys={"k1": pub}, now=now)
        assert False, "cross-appliance replay was accepted"
    except ai.ManifestError:
        pass


def test_valid_manifest_verifies_cleanly():
    priv, pub = _keypair()
    now = datetime.now()
    body = {
        "manifest_version": 4, "issued_at": now.isoformat(), "expires_at": (now + timedelta(hours=1)).isoformat(),
        "appliance": {"cloud_id": "AIC-1", "partner_id": "p1", "customer_id": "c1", "site_id": "s1"}, "identities": [],
    }
    body["signature"] = ai.sign_body(body, key_id="k1", private_key_b64=priv)
    ai.verify_manifest(body, expected_cloud_id="AIC-1", public_keys={"k1": pub}, now=now)  # does not raise


# =============================================================== portal bucket matching (multi-role selector)


def test_administrator_grant_only_matches_administrator_portal():
    assert ai.portal_bucket_matches("administrator", "administrator")
    assert not ai.portal_bucket_matches("administrator", "partner")


def test_partner_owner_and_salesperson_both_match_the_partner_bucket():
    assert ai.portal_bucket_matches("partner_owner", "partner")
    assert ai.portal_bucket_matches("salesperson", "partner")


def test_technician_only_matches_technician_bucket():
    assert ai.portal_bucket_matches("technician", "technician")
    assert not ai.portal_bucket_matches("technician", "administrator")


# =============================================================== revocation reconciliation (pure)


def test_revoked_user_sessions_are_flagged_for_revocation():
    local_sessions = [{"session_id": "s1", "user_id": "u-gone", "authorization_version": 3}]
    manifest_identities = []  # user no longer in the manifest at all
    assert ai.sessions_to_revoke(local_sessions, manifest_identities) == ["s1"]


def test_role_change_bumps_authorization_version_and_invalidates_the_old_session():
    local_sessions = [{"session_id": "s1", "user_id": "u1", "authorization_version": 3}]
    manifest_identities = [{"user_id": "u1", "enabled": True, "revoked_at": None, "authorization_version": 4}]
    assert ai.sessions_to_revoke(local_sessions, manifest_identities) == ["s1"]


def test_matching_authorization_version_is_left_alone():
    local_sessions = [{"session_id": "s1", "user_id": "u1", "authorization_version": 4}]
    manifest_identities = [{"user_id": "u1", "enabled": True, "revoked_at": None, "authorization_version": 4}]
    assert ai.sessions_to_revoke(local_sessions, manifest_identities) == []


def test_disabled_identity_sessions_are_revoked_even_with_a_matching_version():
    local_sessions = [{"session_id": "s1", "user_id": "u1", "authorization_version": 4}]
    manifest_identities = [{"user_id": "u1", "enabled": False, "revoked_at": None, "authorization_version": 4}]
    assert ai.sessions_to_revoke(local_sessions, manifest_identities) == ["s1"]


# =============================================================== offline grace


def test_session_within_offline_grace_period():
    now = datetime.now()
    assert ai.session_within_offline_grace(last_verified_at=now - timedelta(hours=10), now=now, grace_hours=72)


def test_session_past_offline_grace_period():
    now = datetime.now()
    assert not ai.session_within_offline_grace(last_verified_at=now - timedelta(hours=100), now=now, grace_hours=72)
