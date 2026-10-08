"""Codex findings 2 and 3 on fc50f44 (2026-10-08): /customer/setup?step=4 used
whichever appliance came first (or the saved draft's), so a customer with more
than one appliance could discover cameras on the wrong one, and discovery was
offered before the new appliance had enrolled. Now ?appliance=<id> selects
exactly that appliance -- only if it belongs to the signed-in customer -- and
step 4+ only once it is ready (else its status, step 3). Fixtures come from
test_customer_setup_cloud_id_field.py."""
import re
import sqlite3

from test_customer_setup_cloud_id_field import (  # noqa: F401  (pytest fixtures)
    _owner_cookie, _seed_appliance, _seed_tenant, db_path, http_client, partner_portal,
)


def _page(http_client, query):
    body = http_client.get("/customer/setup" + query, cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}).text
    step = re.search(r"let setupStep=(\d+)", body).group(1)
    select = body[body.index('<select id="customer-appliance">'):]
    select = select[:select.index("</select>")]
    selected = re.findall(r'<option value="([^"]+)" selected>', select)
    return step, selected


def _make_ready(conn, appliance_id):
    conn.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at,last_used_at,created_by) VALUES(?,?,?,?,?,?)",
                 ("cred-" + appliance_id, appliance_id, "x", "2026-10-08T10:00:00", "2026-10-08T10:00:05", "claim"))
    conn.execute("UPDATE appliances SET last_check_in='2026-10-08T10:00:20' WHERE id=?", (appliance_id,))
    conn.commit()


def _two_appliances(db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_appliance(conn, appliance_id="appl-old")   # first in the list
    _seed_appliance(conn, appliance_id="appl-new")   # the one just claimed
    return conn


def test_the_claimed_appliance_is_selected_not_the_first_one(http_client, db_path):
    conn = _two_appliances(db_path)
    _make_ready(conn, "appl-new")
    assert _page(http_client, "?step=4&appliance=appl-new") == ("4", ["appl-new"])


def test_discovery_waits_until_that_appliance_is_ready(http_client, db_path):
    conn = _two_appliances(db_path)
    _make_ready(conn, "appl-old")  # another appliance being ready does not count
    assert _page(http_client, "?step=4&appliance=appl-new") == ("3", ["appl-new"])
    _make_ready(conn, "appl-new")
    assert _page(http_client, "?step=4&appliance=appl-new") == ("4", ["appl-new"])


def test_an_appliance_of_another_account_is_never_selected(http_client, db_path):
    conn = _two_appliances(db_path)
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-2','partner-1','Other','o@example.test','active','2026')")
    conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-foreign','cust-2','site-1','AIC-F','2026')")
    conn.commit()
    _make_ready(conn, "appl-foreign")
    step, selected = _page(http_client, "?step=4&appliance=appl-foreign")
    assert "appl-foreign" not in selected
    body = http_client.get("/customer/setup?appliance=appl-foreign", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}).text
    assert "appl-foreign" not in body


def test_without_appliance_parameter_behaviour_is_unchanged(http_client, db_path):
    _two_appliances(db_path)
    assert _page(http_client, "?step=4") == ("4", ["appl-old"])
