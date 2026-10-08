"""Zero-terminal onboarding (2026-10-08): /customer/setup?step=N lets the claim
page send a just-linked appliance's owner straight to Discover cameras (step
4) -- only once the customer has an appliance, so a direct link never skips
adding one. Fixtures come from test_customer_setup_cloud_id_field.py."""
import re
import sqlite3

from test_customer_setup_cloud_id_field import (  # noqa: F401  (pytest fixtures)
    _owner_cookie, _seed_appliance, _seed_tenant, db_path, http_client, partner_portal,
)


def _setup_step(http_client, query):
    body = http_client.get("/customer/setup" + query, cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}).text
    return re.search(r"let setupStep=(\d+)", body).group(1)


def test_setup_opens_discovery_for_a_linked_appliance(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_appliance(conn, activation_status="activated")
    # Discovery only once the appliance has really enrolled (its credential
    # used, a heartbeat since -- appliance_readiness.py); until then its
    # status, step 3 (Codex review of d0a00ac).
    assert _setup_step(http_client, "?step=4") == "3"
    conn.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at,last_used_at) "
                 "VALUES('cred-appl-1','appl-1','x','2026-10-08T10:00:00','2026-10-08T10:00:05')")
    conn.execute("UPDATE appliances SET last_check_in='2026-10-08T10:00:20' WHERE id='appl-1'")
    conn.commit()
    assert _setup_step(http_client, "?step=4") == "4"
    assert _setup_step(http_client, "?step=99") == "7"
    assert _setup_step(http_client, "?step=x") == "1"


def test_setup_never_skips_adding_an_appliance(http_client, db_path):
    _seed_tenant(sqlite3.connect(db_path))
    assert _setup_step(http_client, "?step=4") == "1"
