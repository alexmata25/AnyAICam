"""/customer/claim-appliance with the code an appliance shows on its own
screen (2026-10-08): the Linux 1.2.4 `anyaicam-setup --claim` flow, reached
from /customer/setup Step 2. The customer enters the 8-character code,
chooses the site and confirms; the appliance completes enrollment itself.
Exactly one appliance results -- no Cloud ID, no activation token, no
duplicate. Fixtures and helpers come from test_appliance_claims.py."""
from test_appliance_claims import (  # noqa: F401  (pytest fixtures)
    DEVICE_SECRET, VALID_DEVICE_ID, _begin, _confirm, _customer_cookie, _lookup, _seeded_customer, _status,
    claim_flow_key, client, connection, db_path, identity_file, override_target, partner_portal,
)


def _page(client, query=""):
    response = client.get("/customer/claim-appliance" + query, cookies={partner_portal.SESSION_COOKIE: _customer_cookie()})
    assert response.status_code == 200
    return response.text


def test_page_names_every_place_a_code_comes_from(client, db_path):
    _seeded_customer(db_path)
    body = _page(client)
    assert "on its screen during setup" in body
    assert "Windows installer" in body
    assert "printed on its label" in body
    assert 'placeholder="Claim code"' in body
    # An 8-character terminal code is sent as claim_code, a 12-character label code as label_code.
    assert "compact.length===12?{label_code:compact}:{claim_code:raw}" in body


def test_done_step_returns_to_setup_only_when_coming_from_setup(client, db_path):
    _seeded_customer(db_path)
    assert 'id="claim-done-continue" href="/customer/setup">Continue setup' in _page(client, "?return=setup")
    assert 'id="claim-done-continue" href="/">Go to your dashboard' in _page(client)
    assert 'href="/customer/setup"' not in _page(client, "?return=https://evil.example")


def test_terminal_code_claim_enrolls_exactly_one_appliance(client, db_path):
    _seeded_customer(db_path)
    session = _begin(client).json()  # what `anyaicam-setup --claim` opens
    code = session["claim_code"]
    assert len(code) == 8
    assert _lookup(client, code, cookie=_customer_cookie()).status_code == 200
    assert _confirm(client, code, "site-1", cookie=_customer_cookie()).status_code == 200
    proof = _status(client, session["claim_session_id"]).json()["claim_proof"]
    done = client.post("/api/appliance/claim/complete",
                       json={"device_secret": DEVICE_SECRET, "claim_session_id": session["claim_session_id"], "claim_proof": proof})
    assert done.status_code == 200
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            appliances = db.execute("SELECT cloud_id, customer_id, site_id FROM appliances").fetchall()
    assert [tuple(a) for a in appliances] == [(VALID_DEVICE_ID.upper(), "cust-1", "site-1")]
    # The same appliance cannot be claimed a second time into a new row.
    assert _begin(client).status_code == 409
