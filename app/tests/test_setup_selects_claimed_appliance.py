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
    # Selection without ?appliance= is unchanged (first appliance, or the
    # saved draft's); discovery (step 4) only once THAT appliance has
    # enrolled (Codex review of d0a00ac).
    conn = _two_appliances(db_path)
    assert _page(http_client, "?step=4") == ("3", ["appl-old"])
    _make_ready(conn, "appl-old")
    assert _page(http_client, "?step=4") == ("4", ["appl-old"])
    _saved_draft(conn, "appl-new", step=5)  # the saved draft still wins without the parameter
    assert _page(http_client, "") == ("5", ["appl-new"])


# ------------------- initial page load: step 4 only for an enrolled appliance
def _scan_button(http_client, query):
    body = http_client.get("/customer/setup" + query, cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}).text
    button = body[body.index('<button class="action-button" id="start-camera-scan"'):]
    return button[:button.index(">") + 1], 'id="scan-not-ready"' in body


def test_a_direct_step_4_url_shows_status_until_the_selected_appliance_enrolls(http_client, db_path):
    conn = _two_appliances(db_path)
    _make_ready(conn, "appl-new")  # a different appliance being ready does not count
    assert _page(http_client, "?step=4") == ("3", ["appl-old"])
    assert _scan_button(http_client, "?step=4") == ('<button class="action-button" id="start-camera-scan" disabled>', True)
    _make_ready(conn, "appl-old")
    assert _page(http_client, "?step=4") == ("4", ["appl-old"])
    assert _scan_button(http_client, "?step=4") == ('<button class="action-button" id="start-camera-scan">', False)


def test_a_saved_step_4_draft_shows_status_until_its_appliance_enrolls(http_client, db_path):
    conn = _two_appliances(db_path)
    _make_ready(conn, "appl-old")  # the first appliance is ready; the draft's is not
    _saved_draft(conn, "appl-new", step=4)
    assert _page(http_client, "") == ("3", ["appl-new"])
    assert _scan_button(http_client, "")[1] is True
    _make_ready(conn, "appl-new")
    assert _page(http_client, "") == ("4", ["appl-new"])
    assert _scan_button(http_client, "") == ('<button class="action-button" id="start-camera-scan">', False)


def test_a_saved_draft_with_an_explicit_unready_appliance_never_lands_past_its_status(http_client, db_path):
    conn = _two_appliances(db_path)
    _saved_draft(conn, "appl-new", step=6)
    assert _page(http_client, "?appliance=appl-new") == ("3", ["appl-new"])
    _make_ready(conn, "appl-new")
    assert _page(http_client, "?appliance=appl-new") == ("6", ["appl-new"])


def test_no_appliance_at_all_never_opens_on_discovery(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    conn.execute("INSERT INTO customer_setup_drafts(customer_id,current_step,data_json,updated_at) VALUES('cust-1',4,'{}','2026-10-08')")
    conn.commit()
    step = re.search(r"let setupStep=(\d+)", http_client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}).text).group(1)
    assert step == "3"


# Review of 9e6740f: after an unselectable ?appliance= link the page's own
# reloads kept that parameter, so choosing or linking an appliance landed on
# the same "not on your account" page again. Review of 6c23573: Next, Back and
# the step tabs could enter discovery (step 4) before the selected appliance
# had enrolled. These run the page's REAL setup script under Node against a
# DOM holding only the elements the rendered page really has (an element the
# page lacks is null, as in a browser), with the page's real step tabs and
# appliance picker; fetch returns the endpoint's documented response shape.
# They perform the customer's action and report where the page went.
_NODE_HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const html = fs.readFileSync(process.argv[2], 'utf8'), src = fs.readFileSync(process.argv[3], 'utf8');
const action = JSON.parse(process.argv[4]);
const nav = {href: null, reloaded: false}, posted = [], toasts = [];
function stub(id, extra) {
  return Object.assign({id, value: '', textContent: '', innerHTML: '', hidden: false, disabled: false, dataset: {}, style: {},
          classList: {toggle() {}, add() {}, remove() {}}, setAttribute() {}, removeAttribute() {},
          getAttribute() { return ''; }, replaceChildren() {}, append() {}, addEventListener() {},
          scrollIntoView() {}, querySelectorAll() { return []; }, closest() { return null; }}, extra || {});
}
const els = {};
for (const m of html.matchAll(/ id="([^"]+)"/g)) els[m[1]] = stub(m[1]);
const picker = html.slice(html.indexOf('<select id="customer-appliance">'));
const chosen = /<option value="([^"]*)" selected/.exec(picker.slice(0, picker.indexOf('</select>')));
if (els['customer-appliance']) els['customer-appliance'].value = chosen ? chosen[1] : '';
const tabs = [...html.matchAll(/class="workspace-tab[^"]*" data-step="([0-9])"/g)].map(m => stub('tab' + m[1], {dataset: {step: m[1]}}));
const steps = [...html.matchAll(/class="customer-setup-step" data-step="([0-9])"/g)].map(m => stub('step' + m[1], {dataset: {step: m[1]}}));
const sandbox = {
  document: {getElementById: id => els[id] || null,
             querySelectorAll: sel => sel === '#customer-setup-tabs .workspace-tab' ? tabs : sel === '.customer-setup-step' ? steps : [],
             querySelector: () => null, createElement: tag => stub(tag)},
  location: {get href() { return nav.href; }, set href(v) { nav.href = v; }, reload() { nav.reloaded = true; }},
  fetch: async (url, opts) => {
    posted.push(url);
    const r = action.responses[url] || {status: 200, body: {}};
    return {ok: r.status < 400, status: r.status, json: async () => r.body};
  },
  showToast: message => toasts.push(message), confirm: () => true, prompt: () => '', setTimeout: () => 0,
  encodeURIComponent, JSON, Number, String, Object, Array, Boolean,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
const step = () => vm.runInContext('setupStep', sandbox);
(async () => {
  const visited = [];
  for (const act of action.acts) {
    if (act.kind === 'select') { els['customer-appliance'].value = act.value; await els['customer-appliance'].onchange(); }
    if (act.kind === 'link') { await els['link-customer-appliance'].onclick(); }
    if (act.kind === 'provision') { await els['provision-appliance-button'].onclick(); }
    if (act.kind === 'next') { await els['customer-setup-next'].onclick(); }
    if (act.kind === 'back') { await els['customer-setup-back'].onclick(); }
    if (act.kind === 'tab') { await tabs.find(t => t.dataset.step === String(act.step)).onclick(); }
    if (act.kind === 'scan') { await els['start-camera-scan'].onclick(); }
    visited.push(step());
  }
  const scanMessage = els['scan-message'] ? els['scan-message'].textContent : null;
  process.stdout.write(JSON.stringify({nav, posted, toasts, visited, step: step(), scanMessage}));
})().catch(error => { console.error(error); process.exit(1); });
"""


def _run_setup_script(http_client, query, acts, tmp_path, responses=None):
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
    (tmp_path / "page.html").write_text(body, encoding="utf-8")
    (tmp_path / "setup.js").write_text(script, encoding="utf-8")
    (tmp_path / "harness.js").write_text(_NODE_HARNESS, encoding="utf-8")
    action = {"acts": acts, "responses": responses or {}}
    result = subprocess.run([node, str(tmp_path / "harness.js"), str(tmp_path / "page.html"), str(tmp_path / "setup.js"), json.dumps(action)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _ok(body):
    return {"status": 200, "body": body}


def test_choosing_an_appliance_after_an_unselectable_link_goes_to_that_appliance(http_client, db_path, tmp_path):
    conn = _two_appliances(db_path)
    _foreign(conn)
    _saved_draft(conn, "appl-old", step=5)
    out = _run_setup_script(http_client, "?step=4&appliance=appl-foreign", [{"kind": "select", "value": "appl-new"}], tmp_path)
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
    out = _run_setup_script(http_client, "?appliance=does-not-exist", [{"kind": "link"}], tmp_path,
                            {"/api/customer/appliances/link": _ok(linked)})
    assert out["nav"]["reloaded"] is False
    url = out["nav"]["href"]
    assert url == "/customer/setup?step=3&appliance=appl-new"
    assert _page(http_client, url[len("/customer/setup"):]) == ("3", ["appl-new"])
    # Once it has enrolled the same page lets the customer on to discovery; not before.
    assert _page(http_client, "?step=4&appliance=appl-new") == ("3", ["appl-new"])
    _make_ready(conn, "appl-new")
    assert _page(http_client, "?step=4&appliance=appl-new") == ("4", ["appl-new"])


def test_provisioning_after_an_unselectable_link_goes_to_the_new_appliance(http_client, db_path, tmp_path):
    # The Provision button only exists for an account with no appliance yet;
    # the harness has no element the real page lacks, so this proves it is there.
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    page = http_client.get("/customer/setup?appliance=does-not-exist", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}).text
    assert 'id="provision-appliance-button"' in page
    provisioned = {"status": "provisioned", "appliance_id": "appl-new", "cloud_id": "AIC-N", "site_id": "site-1"}
    out = _run_setup_script(http_client, "?appliance=does-not-exist", [{"kind": "provision"}], tmp_path,
                            {"/api/customer/appliances/provision": _ok(provisioned)})
    assert out["posted"] == ["/api/customer/appliances/provision"]
    assert out["nav"] == {"href": "/customer/setup?step=3&appliance=appl-new", "reloaded": False}
    # What provisioning created; following the URL selects it, at its status.
    conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-new','cust-1','site-1','AIC-N','2026')")
    conn.commit()
    assert _page(http_client, "?step=3&appliance=appl-new") == ("3", ["appl-new"])


def test_a_normal_setup_page_still_just_reloads(http_client, db_path, tmp_path):
    _two_appliances(db_path)
    linked = {"message": "ok", "appliance_id": "appl-new"}
    for query in ("", "?step=4", "?step=3&appliance=appl-new"):
        out = _run_setup_script(http_client, query, [{"kind": "select", "value": "appl-old"}], tmp_path)
        assert out["nav"] == {"href": None, "reloaded": True}, query
        out = _run_setup_script(http_client, query, [{"kind": "link"}], tmp_path, {"/api/customer/appliances/link": _ok(linked)})
        assert out["nav"] == {"href": None, "reloaded": True}, query


# ------------------------------------------- discovery only after enrollment
def test_next_and_tabs_do_not_enter_discovery_before_the_appliance_has_enrolled(http_client, db_path, tmp_path):
    conn = _two_appliances(db_path)
    _make_ready(conn, "appl-old")  # another appliance being ready does not count
    out = _run_setup_script(http_client, "?step=3&appliance=appl-new",
                            [{"kind": "next"}, {"kind": "tab", "step": 4}, {"kind": "tab", "step": 5}, {"kind": "back"}], tmp_path)
    # Next and the Discover tab stay at its status (3); the Cameras tab is
    # allowed, but Back from it does not land in discovery either.
    assert out["visited"] == [3, 3, 5, 3]
    assert len(out["toasts"]) == 3 and "finished linking" in out["toasts"][0]


def test_next_and_tabs_enter_discovery_once_the_appliance_has_enrolled(http_client, db_path, tmp_path):
    conn = _two_appliances(db_path)
    _make_ready(conn, "appl-new")
    out = _run_setup_script(http_client, "?step=3&appliance=appl-new",
                            [{"kind": "next"}, {"kind": "tab", "step": 3}, {"kind": "tab", "step": 4}, {"kind": "tab", "step": 5}, {"kind": "back"}], tmp_path)
    assert out["visited"] == [4, 3, 4, 5, 4]
    assert out["toasts"] == []


def test_the_readiness_checked_is_the_selected_appliances(http_client, db_path, tmp_path):
    conn = _two_appliances(db_path)
    _make_ready(conn, "appl-new")
    # Legacy page (no ?appliance=): the first appliance, appl-old, is selected and not enrolled.
    out = _run_setup_script(http_client, "?step=3", [{"kind": "next"}], tmp_path)
    assert out["visited"] == [3]


def _scan(http_client, appliance_id):
    return http_client.post(f"/api/customer/appliances/{appliance_id}/scan", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})


def _scan_jobs(db_path):
    return sqlite3.connect(db_path).execute("SELECT appliance_id FROM camera_scan_jobs").fetchall()


def test_a_direct_scan_request_is_refused_until_that_appliance_has_enrolled(http_client, db_path):
    conn = _two_appliances(db_path)
    _make_ready(conn, "appl-old")
    response = _scan(http_client, "appl-new")
    assert response.status_code == 409
    assert "has not finished linking" in response.json()["detail"]
    assert _scan_jobs(db_path) == []  # nothing queued for the appliance
    _make_ready(conn, "appl-new")
    response = _scan(http_client, "appl-new")
    assert response.status_code == 200 and response.json()["job_id"]
    assert _scan_jobs(db_path) == [("appl-new",)]


def test_a_revoked_credential_is_not_enrollment(http_client, db_path):
    conn = _two_appliances(db_path)
    _make_ready(conn, "appl-new")
    conn.execute("UPDATE appliance_credentials SET revoked_at='2026-10-08T11:00:00' WHERE appliance_id='appl-new'")
    conn.commit()
    assert _scan(http_client, "appl-new").status_code == 409


def test_another_accounts_appliance_is_still_not_found_ready_or_not(http_client, db_path):
    conn = _two_appliances(db_path)
    _foreign(conn)  # enrolled, but not this customer's
    assert _scan(http_client, "appl-foreign").status_code == 404
    assert _scan(http_client, "does-not-exist").status_code == 404
    assert _scan_jobs(db_path) == []


def test_a_refused_scan_shows_why_on_the_page(http_client, db_path, tmp_path):
    conn = _two_appliances(db_path)
    _make_ready(conn, "appl-new")
    refused = {"status": 409, "body": {"detail": "This appliance has not finished linking yet. Camera discovery starts once it has checked in."}}
    out = _run_setup_script(http_client, "?step=4&appliance=appl-new", [{"kind": "scan"}], tmp_path,
                            {"/api/customer/appliances/appl-new/scan": refused})
    assert out["scanMessage"] == refused["body"]["detail"]
