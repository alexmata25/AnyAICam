"""Zero-terminal onboarding, cloud pages (2026-10-08).

The appliance's own "AnyAiCam Setup" page sends the customer's browser to
/claim#code=<code>. /claim keeps the code out of every URL and log and hands
it to the signed-in claim page, which never displays it: it looks the
appliance up by itself, the customer confirms the site, and once the
appliance has linked the page continues to setup's Discover cameras step
(/customer/setup?step=4). Fixtures and helpers come from
test_appliance_claims.py and test_customer_setup_cloud_id_field.py."""
import re
import sqlite3

from test_appliance_claims import (  # noqa: F401  (pytest fixtures)
    _customer_cookie, _seeded_customer, claim_flow_key, client, connection, db_path, identity_file,
    override_target, partner_portal,
)


def _claim_page(client, query="?return=setup"):
    response = client.get("/customer/claim-appliance" + query, cookies={partner_portal.SESSION_COOKIE: _customer_cookie()})
    assert response.status_code == 200
    return response.text


def test_claim_entry_accepts_a_code_from_the_appliance_setup_page(client, db_path):
    body = client.get("/claim").text
    assert "(?:^|[#&])code=([0-9A-Za-z]{8})(?:&|$)" in body
    assert "sessionStorage.setItem('anyaicam.claimCode',linked[1].toUpperCase())" in body
    assert "history.replaceState(null,'',location.pathname)" in body  # out of the address bar and history
    # The label QR flow is unchanged.
    assert "sessionStorage.setItem('anyaicam.claimLabel'" in body


def test_claim_page_uses_the_code_without_ever_showing_it(client, db_path):
    _seeded_customer(db_path)
    body = _claim_page(client)
    assert "linkCode=sessionStorage.getItem('anyaicam.claimCode')||''" in body
    assert "sessionStorage.removeItem('anyaicam.claimCode')" in body  # read once
    assert "if(!/^[0-9A-Z]{8}$/.test(linkCode))linkCode='';" in body  # nothing but a code is accepted
    # Hidden input and button; the code goes into the request body only.
    assert "document.getElementById('claim-code-input').closest('label').hidden=true;" in body
    assert "if(linkCode)return {claim_code:linkCode};" in body
    assert "claim-code-input').value=linkCode" not in body
    assert "if(linkCode)document.getElementById('claim-lookup-button').onclick();" in body  # looks it up by itself


def test_after_confirming_the_page_waits_for_the_appliance_then_goes_to_discovery(client, db_path):
    _seeded_customer(db_path)
    body = _claim_page(client)
    # Codex finding 3: readiness of THAT appliance, then discovery with it
    # selected (test_claim_enrollment_readiness.py covers the endpoint).
    assert "fetch('/api/portal/claims/enrollment?device_id='" in body
    assert "continueLink.href='/customer/setup?step=4&appliance='+encodeURIComponent(applianceId)" in body
    assert "Discover cameras" in body
    # A failed link asks the customer to use the appliance's page again (no code to type).
    assert "open AnyAiCam Setup and choose “Link this appliance” again" in body


def test_confirmation_still_requires_an_explicit_click_and_a_site_of_this_account(client, db_path):
    _seeded_customer(db_path)
    body = _claim_page(client)
    assert re.search(r"document\.getElementById\('claim-confirm-button'\)\.onclick=async\(\)=>", body)
    assert "claim-confirm-button').onclick()" not in body  # never confirmed automatically
    assert "Only confirm if you just chose “Link this appliance” on your own AnyAiCam appliance." in body
