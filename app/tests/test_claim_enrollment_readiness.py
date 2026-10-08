"""Codex finding 3 on fc50f44 (2026-10-08): the claim page reported "linked" and
offered discovery as soon as the cloud's appliance row existed -- but
claim/complete creates that row before the appliance has saved its identity,
and the appliance can still fail and roll back. "Ready" now requires THIS
appliance to have authenticated with its own credential and sent a heartbeat
after that credential was issued (appliance_readiness.py), via
GET /api/portal/claims/enrollment. Fixtures come from test_appliance_claims.py."""
import pytest

from test_appliance_claims import (  # noqa: F401  (pytest fixtures)
    _customer_cookie, _seed_customer, _seed_other_customer_site, claim_flow_key, client, connection, db_path,
    identity_file, override_target, partner_portal,
)
import appliance_claims  # noqa: E402

DEVICE = "11111111-1111-4111-8111-111111111111"
OTHER_DEVICE = "22222222-2222-4222-9222-222222222222"


@pytest.fixture(autouse=True)
def _reset_limiter():
    appliance_claims.claim_enrollment_limiter.events.clear()


def _seed(db_path, *, customer="cust-1", device=DEVICE, appliance_id="appl-1", credential_used=None,
          credential_created="2026-10-08T10:00:00", check_in=None, revoked=None):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            if not db.execute("SELECT 1 FROM customers WHERE id='cust-1'").fetchone():
                _seed_customer(db)
                _seed_other_customer_site(db)
            site = "site-1" if customer == "cust-1" else "site-other"
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,last_check_in,created_at) VALUES(?,?,?,?,?,?)",
                       (appliance_id, customer, site, device.upper(), check_in, credential_created))
            db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at,last_used_at,revoked_at,created_by) VALUES(?,?,?,?,?,?,?)",
                       ("cred-" + appliance_id, appliance_id, "x", credential_created, credential_used, revoked, "claim"))


def _enrollment(client, device=DEVICE, cookie=None):
    cookies = {partner_portal.SESSION_COOKIE: cookie or _customer_cookie()}
    return client.get("/api/portal/claims/enrollment", params={"device_id": device}, cookies=cookies)


def test_nothing_yet_is_waiting(client, db_path):
    _seed(db_path, device=OTHER_DEVICE)  # someone's appliance, not this one
    assert _enrollment(client).json() == {"status": "waiting"}


def test_a_row_created_by_claim_complete_alone_is_not_ready(client, db_path):
    _seed(db_path)  # credential issued but never used, no heartbeat: the appliance may have rolled back
    assert _enrollment(client).json() == {"status": "enrolling", "appliance_id": "appl-1"}


def test_credential_used_but_no_heartbeat_is_not_ready(client, db_path):
    _seed(db_path, credential_used="2026-10-08T10:00:05")
    assert _enrollment(client).json()["status"] == "enrolling"


def test_a_heartbeat_from_before_this_credential_does_not_count(client, db_path):
    _seed(db_path, credential_used="2026-10-08T10:00:05", check_in="2026-10-07T09:00:00")
    assert _enrollment(client).json()["status"] == "enrolling"


def test_ready_once_the_appliance_authenticated_and_checked_in(client, db_path):
    _seed(db_path, credential_used="2026-10-08T10:00:05", check_in="2026-10-08T10:00:20")
    assert _enrollment(client).json() == {"status": "ready", "appliance_id": "appl-1"}


def test_a_revoked_credential_is_never_ready(client, db_path):
    _seed(db_path, credential_used="2026-10-08T10:00:05", check_in="2026-10-08T10:00:20", revoked="2026-10-08T10:01:00")
    assert _enrollment(client).json()["status"] == "enrolling"


def test_another_accounts_appliance_is_never_revealed(client, db_path):
    _seed(db_path, customer="cust-other", credential_used="2026-10-08T10:00:05", check_in="2026-10-08T10:00:20")
    assert _enrollment(client).json() == {"status": "waiting"}  # no appliance_id, no status


def test_requires_a_signed_in_customer_owner_and_a_device_id(client, db_path):
    _seed(db_path)
    assert client.get("/api/portal/claims/enrollment", params={"device_id": DEVICE}).status_code in (401, 403)
    assert _enrollment(client, device="not-a-uuid").status_code == 400


def test_polling_is_rate_limited(client, db_path):
    _seed(db_path)
    statuses = [_enrollment(client).status_code for _ in range(61)]
    assert statuses[:60] == [200] * 60 and statuses[60] == 429


def test_claim_page_waits_for_readiness_and_selects_that_appliance(client, db_path):
    _seed(db_path)
    body = client.get("/customer/claim-appliance?return=setup", cookies={partner_portal.SESSION_COOKIE: _customer_cookie()}).text
    assert "fetch('/api/portal/claims/enrollment?device_id='+encodeURIComponent(claimedDevice))" in body
    assert "s.status==='ready'&&applianceId" in body
    assert "continueLink.href='/customer/setup?step=4&appliance='+encodeURIComponent(applianceId)" in body
    # Not ready in time: its status, never discovery.
    assert "'/customer/setup?step=3&appliance='+encodeURIComponent(applianceId)" in body
    assert "fetch('/api/customer/setup/status')" not in body
