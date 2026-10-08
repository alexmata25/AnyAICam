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


# Review of 9e6740f: after an unselectable ?appliance= link the page's own
# reloads kept that parameter, so choosing or linking an appliance landed on
# the same "not on your account" page again. These run the page's REAL setup
# script under Node (a minimal DOM, fetch returning the endpoint's documented
# response shape), trigger the customer's action, capture where the browser
# is sent, then load that URL from the app.
_NODE_HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8'), action = JSON.parse(process.argv[3]);
const nav = {href: null, reloaded: false}, posted = [];
function stub(id) {
  return {id, value: '', textContent: '', innerHTML: '', hidden: false, disabled: false, dataset: {}, style: {},
          classList: {toggle() {}, add() {}, remove() {}}, setAttribute() {}, removeAttribute() {},
          getAttribute() { return ''; }, replaceChildren() {}, append() {}, addEventListener() {},
          scrollIntoView() {}, querySelectorAll() { return []; }, closest() { return null; }};
}
const els = {};
const sandbox = {
  document: {getElementById: id => els[id] || (els[id] = stub(id)), querySelectorAll: () => [],
             querySelector: () => null, createElement: tag => stub(tag)},
  location: {get href() { return nav.href; }, set href(v) { nav.href = v; }, reload() { nav.reloaded = true; }},
  fetch: async (url, opts) => { posted.push(url); return {ok: true, status: 200, json: async () => action.responses[url] || {}}; },
  showToast() {}, confirm: () => true, prompt: () => '', setTimeout: () => 0, encodeURIComponent, JSON, Number, String, Object, Array,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
(async () => {
  if (action.kind === 'select') { els['customer-appliance'].value = action.value; await els['customer-appliance'].onchange(); }
  if (action.kind === 'link') { await els['link-customer-appliance'].onclick(); }
  if (action.kind === 'provision') { await els['provision-appliance-button'].onclick(); }
  process.stdout.write(JSON.stringify({nav, posted}));
})().catch(error => { console.error(error); process.exit(1); });
"""


def _run_setup_script(http_client, query, action, tmp_path):
    import json
    import os
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        if os.environ.get("ANYAICAM_REQUIRE_JS_TESTS", "").lower() == "true":
            pytest.fail("node is required (ANYAICAM_REQUIRE_JS_TESTS=true) but not installed")
        pytest.skip("node not installed in this environment")
    body = http_client.get("/customer/setup" + query, cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}).text
    start = body.index("<script>let setupStep=") + len("<script>")
    script = body[start:body.index("</script>", start)]
    (tmp_path / "setup.js").write_text(script, encoding="utf-8")
    (tmp_path / "harness.js").write_text(_NODE_HARNESS, encoding="utf-8")
    result = subprocess.run([node, str(tmp_path / "harness.js"), str(tmp_path / "setup.js"), json.dumps(action)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_choosing_an_appliance_after_an_unselectable_link_goes_to_that_appliance(http_client, db_path, tmp_path):
    conn = _two_appliances(db_path)
    _foreign(conn)
    _saved_draft(conn, "appl-old", step=5)
    out = _run_setup_script(http_client, "?step=4&appliance=appl-foreign", {"kind": "select", "value": "appl-new", "responses": {}}, tmp_path)
    assert out["nav"]["reloaded"] is False  # a reload would keep ?appliance=appl-foreign
    assert out["posted"][0] == "/api/customer/setup/progress"  # the choice is still saved first
    url = out["nav"]["href"]
    assert url == "/customer/setup?step=3&appliance=appl-new"
    # Follow it: the chosen appliance, not the older/draft one, and not discovery before it has enrolled.
    assert _page(http_client, url[len("/customer/setup"):]) == ("3", ["appl-new"])
    page = http_client.get(url, cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}).text
    assert 'id="appliance-not-found"' not in page


def test_linking_an_appliance_after_an_unselectable_link_goes_to_the_linked_one(http_client, db_path, tmp_path):
    conn = _two_appliances(db_path)
    _make_ready(conn, "appl-old")
    _foreign(conn)
    _saved_draft(conn, "appl-old", step=6)
    # The link endpoint's success response (link_customer_appliance) carries the linked appliance's id.
    linked = {"message": "Appliance linked to customer account.", "appliance_id": "appl-new", "camera_slots_purchased": 0}
    out = _run_setup_script(http_client, "?appliance=does-not-exist",
                            {"kind": "link", "responses": {"/api/customer/appliances/link": linked}}, tmp_path)
    assert out["nav"]["reloaded"] is False
    url = out["nav"]["href"]
    assert url == "/customer/setup?step=3&appliance=appl-new"
    assert _page(http_client, url[len("/customer/setup"):]) == ("3", ["appl-new"])
    # Once it has enrolled the same page lets the customer on to discovery; not before.
    assert _page(http_client, "?step=4&appliance=appl-new") == ("3", ["appl-new"])
    _make_ready(conn, "appl-new")
    assert _page(http_client, "?step=4&appliance=appl-new") == ("4", ["appl-new"])


def test_provisioning_after_an_unselectable_link_goes_to_the_new_appliance(http_client, db_path, tmp_path):
    conn = _two_appliances(db_path)
    _foreign(conn)
    provisioned = {"status": "provisioned", "appliance_id": "appl-new", "cloud_id": "AIC-N", "site_id": "site-1"}
    out = _run_setup_script(http_client, "?appliance=appl-foreign",
                            {"kind": "provision", "responses": {"/api/customer/appliances/provision": provisioned}}, tmp_path)
    assert out["nav"] == {"href": "/customer/setup?step=3&appliance=appl-new", "reloaded": False}


def test_a_normal_setup_page_still_just_reloads(http_client, db_path, tmp_path):
    _two_appliances(db_path)
    for query in ("", "?step=4", "?step=3&appliance=appl-new"):
        out = _run_setup_script(http_client, query, {"kind": "select", "value": "appl-old", "responses": {}}, tmp_path)
        assert out["nav"] == {"href": None, "reloaded": True}, query
        linked = {"message": "ok", "appliance_id": "appl-new"}
        out = _run_setup_script(http_client, query, {"kind": "link", "responses": {"/api/customer/appliances/link": linked}}, tmp_path)
        assert out["nav"] == {"href": None, "reloaded": True}, query
