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
    selected = re.findall(r'<option value="([^"]*)" selected', select)
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


def _foreign(conn):
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-2','partner-1','Other','o@example.test','active','2026')")
    conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-foreign','cust-2','site-1','AIC-F','2026')")
    conn.commit()
    _make_ready(conn, "appl-foreign")


def _saved_draft(conn, appliance_id="appl-old", step=5):
    conn.execute("INSERT INTO customer_setup_drafts(customer_id,current_step,data_json,updated_at) VALUES('cust-1',?,?,?)",
                 (step, '{"appliance_id": "%s"}' % appliance_id, "2026-10-08"))
    conn.commit()


def test_an_appliance_of_another_account_is_never_selected(http_client, db_path):
    conn = _two_appliances(db_path)
    _foreign(conn)
    step, selected = _page(http_client, "?step=4&appliance=appl-foreign")
    assert selected == [""]  # the "Choose an appliance" placeholder only
    assert step == "2"
    body = http_client.get("/customer/setup?appliance=appl-foreign", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}).text
    assert "appl-foreign" not in body


# Codex re-review of 14b3a93: an explicit ?appliance= that the customer cannot
# select must never fall back to an older owned appliance (first or the saved
# draft's) and must never reach discovery.
def test_a_foreign_appliance_never_falls_back_to_an_owned_one_or_enters_discovery(http_client, db_path):
    conn = _two_appliances(db_path)
    _make_ready(conn, "appl-old")
    _make_ready(conn, "appl-new")
    _foreign(conn)
    _saved_draft(conn, "appl-old", step=5)
    for query in ("?step=4&appliance=appl-foreign", "?step=7&appliance=appl-foreign", "?appliance=appl-foreign"):
        step, selected = _page(http_client, query)
        assert selected == [""], query
        assert "appl-old" not in selected and "appl-new" not in selected, query
        assert int(step) <= 2, query  # never Discover cameras (4) or later


def test_an_invalid_or_unknown_appliance_id_never_selects_or_enters_discovery(http_client, db_path):
    conn = _two_appliances(db_path)
    _make_ready(conn, "appl-old")
    _saved_draft(conn, "appl-old", step=6)
    for bad in ("does-not-exist", "x" * 200, "%3Cscript%3E", "appl-old%20", "APPL-OLD"):
        step, selected = _page(http_client, f"?step=4&appliance={bad}")
        assert selected == [""], bad
        assert int(step) <= 2, bad
    body = http_client.get("/customer/setup?step=4&appliance=%3Cscript%3E", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}).text
    assert "That appliance is not on your account." in body
    assert "<script>" not in body.split('<script>let setupStep', 1)[0].split('</header>', 1)[1][:2000]  # not echoed back


def test_a_valid_newly_claimed_appliance_is_selected_and_advances_once_ready(http_client, db_path):
    # The guards above must not get in the way of the normal path: the
    # customer's own just-claimed appliance wins over the older one and the
    # saved draft's, and reaches discovery as soon as it has enrolled.
    conn = _two_appliances(db_path)
    _make_ready(conn, "appl-old")
    _foreign(conn)
    _saved_draft(conn, "appl-old", step=6)
    assert _page(http_client, "?step=4&appliance=appl-new") == ("3", ["appl-new"])  # not enrolled yet: its status
    _make_ready(conn, "appl-new")
    assert _page(http_client, "?step=4&appliance=appl-new") == ("4", ["appl-new"])  # discovery
    assert _page(http_client, "?step=5&appliance=appl-new") == ("5", ["appl-new"])
    assert _page(http_client, "?step=3&appliance=appl-new") == ("3", ["appl-new"])
    body = http_client.get("/customer/setup?step=4&appliance=appl-new", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}).text
    assert 'id="appliance-not-found"' not in body
    assert "Choose an appliance</option>" not in body


def test_an_empty_appliance_parameter_is_the_same_as_none(http_client, db_path):
    conn = _two_appliances(db_path)
    _saved_draft(conn, "appl-new", step=5)
    assert _page(http_client, "?appliance=") == ("5", ["appl-new"])


def test_without_appliance_parameter_behaviour_is_unchanged(http_client, db_path):
    conn = _two_appliances(db_path)
    assert _page(http_client, "?step=4") == ("4", ["appl-old"])
    _saved_draft(conn, "appl-new", step=5)  # the saved draft still wins without the parameter
    assert _page(http_client, "") == ("5", ["appl-new"])
