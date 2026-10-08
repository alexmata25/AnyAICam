"""/customer/setup Step 2 offers claiming with a code (2026-10-08).

An appliance installed with `anyaicam-setup --claim` (Linux 1.2.4, the
Samsung) waited at "Waiting for a customer to confirm this claim in the
portal", but Step 2 ("Add appliance") only offered "Provision your first
appliance" and Cloud ID + activation token -- nothing led to
/customer/claim-appliance. Step 2 now leads with "Claim your appliance with
its code"; the provision and Cloud ID paths are unchanged.
"""
import sqlite3

from test_customer_setup_cloud_id_field import (  # noqa: F401  (pytest fixtures)
    _owner_cookie, _seed_appliance, _seed_tenant, db_path, http_client, partner_portal,
)


def _step2(body):
    start = body.index('<div class="customer-setup-step" data-step="2"')
    return body[start:body.index('<div class="customer-setup-step" data-step="3"', start)]


def _setup_page(http_client):
    response = http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    return response.text


def test_step2_leads_with_claim_by_code_before_provision_and_cloud_id(http_client, db_path):
    _seed_tenant(sqlite3.connect(db_path))
    step2 = _step2(_setup_page(http_client))
    assert 'id="claim-appliance-panel"' in step2
    assert 'href="/customer/claim-appliance?return=setup"' in step2
    assert "Enter claim code" in step2
    claim = step2.index('id="claim-appliance-panel"')
    assert claim < step2.index('id="provision-first-appliance"'), "claim must come before provisioning a new appliance"
    assert claim < step2.index('id="customer-cloud-id"')
    assert "instead of provisioning a new appliance" in step2


def test_existing_paths_are_unchanged(http_client, db_path):
    _seed_tenant(sqlite3.connect(db_path))
    step2 = _step2(_setup_page(http_client))
    for element in ('id="provision-appliance-button"', 'id="customer-cloud-id"', 'id="customer-activation-token"',
                    'id="customer-qr-file"', 'id="link-customer-appliance"', "Or link with a Cloud ID and activation token"):
        assert element in step2


def test_claim_option_stays_when_the_customer_already_has_an_appliance(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_appliance(conn)
    step2 = _step2(_setup_page(http_client))
    assert 'id="claim-appliance-panel"' in step2  # adding a second appliance by its code
    assert 'id="provision-first-appliance"' not in step2  # unchanged: never offered twice
