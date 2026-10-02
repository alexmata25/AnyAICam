"""Admin portal completion pass (2026-09-26): tab-by-tab defects found by
driving every Administrator portal tab as both kinds of administrator the
cloud actually has:

  - a platform administrator (partner_db account + live global grant --
    the real cloud admin shape, reaching legacy pages through
    cloud_administrator_bridge()), and
  - the legacy local administrator (users.json 'local-admin').

Each test pins one fix on the real HTTP route.
"""
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

import appliance_identity
import main
import partner_portal
from database_backend import override_target
from partner_db import connection, initialize_database, password_hash


@pytest.fixture()
def cloud_client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    for name in ("ANYAICAM_APPLIANCE_ID", "ANYAICAM_APPLIANCE_CLOUD_ID", "ANYAICAM_APPLIANCE_CREDENTIAL"):
        monkeypatch.delenv(name, raising=False)
    with override_target(sqlite_path=tmp_path / "admin_tabs.db"):
        initialize_database()
        appliance_identity.reset_cloud_identity_backend_for_tests()
        with TestClient(main.app) as client:
            yield client
    appliance_identity.reset_cloud_identity_backend_for_tests()


def _platform_admin_cookies(email="platform@anyaicam.test", *, grant_scope="global"):
    with connection() as db:
        now = "2026-09-26T00:00:00"
        db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-1", "Partner", "approved", "real", now))
        user_id = "u-" + email.split("@")[0]
        db.execute(
            "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (user_id, "partner-1", email, "Admin", "administrator", password_hash("x-password-123"), 1, now),
        )
        appliance_identity.create_grant(db, user_id=user_id, role="administrator", scope_type=grant_scope,
                                        scope_id=None if grant_scope == "global" else "partner-1", granted_by="test")
    return {partner_portal.SESSION_COOKIE: partner_portal._token(email, "administrator", None, None, None)}


def _legacy_admin_cookies():
    main.save_users([{"id": "local-admin", "email": "admin@local", "role": "administrator", "enabled": True, "camera_ids": []}])
    return {main.SESSION_COOKIE_NAME: main.create_session("local-admin")}


def _nav_hrefs(html):
    nav = html.split('<nav class="nav"', 1)[1].split("</nav>", 1)[0]
    return [chunk.split('"', 1)[0] for chunk in nav.split('href="')[1:]]


# ------------------------------------------------------------ Camera health


def test_camera_health_script_parses(cloud_client):
    page = cloud_client.get("/camera-health", cookies=_platform_admin_cookies()).text
    assert "const get_camera_count()=" not in page  # a Python name pasted into JS broke the whole script
    assert "const CAMERA_COUNT=" in page
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    for script in [chunk.split("</script>")[0] for chunk in page.split("<script>")[1:]]:
        result = subprocess.run([node, "--check", "-"], input=script, capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, result.stderr[:400]


# ------------------------------------------------------------ Business users / Analytics entitlements


def test_platform_administrator_can_open_business_users(cloud_client):
    page = cloud_client.get("/business-users", cookies=_platform_admin_cookies())
    assert page.status_code == 200
    assert "master administrator access" not in page.text
    assert "Master-controlled business accounts" in page.text


def test_platform_administrator_can_manage_analytics_entitlements(cloud_client):
    cookies = _platform_admin_cookies()
    assert cloud_client.get("/analytics-entitlements", cookies=cookies).status_code == 200
    state = cloud_client.get("/api/admin/analytics-entitlements", cookies=cookies)
    assert state.status_code == 200
    assert "customers" in state.json()


def test_partner_scoped_administrator_still_cannot_open_master_pages(cloud_client):
    cookies = _platform_admin_cookies("company-admin@anyaicam.test", grant_scope="partner")
    assert "master administrator access" in cloud_client.get("/business-users", cookies=cookies).text or \
        cloud_client.get("/business-users", cookies=cookies).status_code in (401, 403)
    assert cloud_client.get("/api/admin/analytics-entitlements", cookies=cookies).status_code in (401, 403)


def test_revoked_grant_loses_master_pages_on_the_next_request(cloud_client):
    cookies = _platform_admin_cookies()
    assert cloud_client.get("/api/admin/analytics-entitlements", cookies=cookies).status_code == 200
    with connection() as db:
        db.execute("UPDATE identity_grants SET revoked_at=?", ("2026-09-26T01:00:00",))
    assert cloud_client.get("/api/admin/analytics-entitlements", cookies=cookies).status_code in (401, 403)


def test_legacy_local_admin_keeps_master_pages(cloud_client):
    cookies = _legacy_admin_cookies()
    assert "Master-controlled business accounts" in cloud_client.get("/business-users", cookies=cookies).text
    assert cloud_client.get("/api/admin/analytics-entitlements", cookies=cookies).status_code == 200


# ------------------------------------------------------------ Facial Recognition nav


def test_facial_recognition_link_only_shows_where_the_page_opens(cloud_client):
    legacy = cloud_client.get("/admin-portal", cookies=_legacy_admin_cookies()).text
    assert "/aac/people" not in _nav_hrefs(legacy)  # /aac/* authenticates Partner Portal sessions only

    platform_cookies = _platform_admin_cookies()
    platform = cloud_client.get("/admin-portal", cookies=platform_cookies).text
    assert "/aac/people" in _nav_hrefs(platform)
    assert cloud_client.get("/aac/people", cookies=platform_cookies).status_code == 200


def test_every_admin_nav_tab_opens_for_a_platform_administrator(cloud_client):
    cookies = _platform_admin_cookies()
    tabs = _nav_hrefs(cloud_client.get("/admin-portal", cookies=cookies).text)
    assert len(tabs) >= 30
    for href in tabs:
        response = cloud_client.get(href, cookies=cookies)
        assert response.status_code == 200, (href, response.status_code)
        assert "does not include" not in response.text, href


# ------------------------------------------------------------ Billing / Customer accounts


@pytest.fixture()
def billing_files(tmp_path, monkeypatch):
    for name in ("BILLING_ACCOUNTS_FILE", "BILLING_INVOICES_FILE", "BILLING_EVENTS_FILE", "AUDIT_LOG_FILE"):
        monkeypatch.setattr(main, name, tmp_path / f"{name.lower()}.json")
    return tmp_path


def test_json_list_stores_round_trip(tmp_path):
    path = tmp_path / "events.json"
    main.save_json_file(path, [{"a": 1}, {"a": 2}])
    assert main.load_json_file(path, []) == [{"a": 1}, {"a": 2}]  # was always [] -> every append replaced the file
    main.save_json_file(path, {"k": "v"})
    assert main.load_json_file(path, {}) == {"k": "v"}
    assert main.load_json_file(path, []) == []  # wrong shape still falls back to the caller's default


def _save_account(client, cookies, account_id="acct-1", **fields):
    payload = {"customer_name": "Owner", "billing_email": "billing@example.test", "company_name": "Example Co",
               "external_customer_id": "cust-ext-1", "payment_status": "current", "billing_cycle": "monthly",
               "next_billing_date": "", "notes": ""}
    payload.update(fields)
    return client.put(f"/api/billing/accounts/{account_id}", json=payload, cookies=cookies)


def test_billing_events_accumulate_across_saves(cloud_client, billing_files):
    cookies = _platform_admin_cookies()
    for status in ("current", "past_due", "suspended", "current"):
        assert _save_account(cloud_client, cookies, payment_status=status).status_code == 200
    events = main.load_billing_events()
    assert [e["action"] for e in events] == ["billing_account_updated"] * 4


def test_trial_and_manual_are_accepted_everywhere_the_forms_offer_them(cloud_client, billing_files):
    cookies = _platform_admin_cookies()
    assert _save_account(cloud_client, cookies, payment_status="trial").status_code == 200
    assert _save_account(cloud_client, cookies, billing_cycle="manual").status_code == 200
    assert _save_account(cloud_client, cookies, payment_status="bogus").status_code == 400
    customers_page = cloud_client.get("/admin-customers", cookies=cookies).text
    assert '<option value="trial">Trial</option>' in customers_page
    assert '<option value="manual">Manual</option>' in customers_page
    assert '<option value="trial">Trial</option>' in cloud_client.get("/billing-operations", cookies=cookies).text


def test_customer_accounts_form_keeps_the_real_external_customer_id(cloud_client, billing_files):
    cookies = _platform_admin_cookies()
    _save_account(cloud_client, cookies, external_customer_id="cust-ext-1")
    page = cloud_client.get("/admin-customers", cookies=cookies).text
    assert 'data-external="cust-ext-1"' in page
    assert "external_customer_id:customerForm.dataset.external||''" in page
    assert "external_customer_id:accountId" not in page


def test_only_one_billing_account_update_route_is_registered():
    routes = [r for r in main.app.routes if getattr(r, "path", "") == "/api/billing/accounts/{account_id}" and "PUT" in getattr(r, "methods", set())]
    assert len(routes) == 1


def test_payment_reminder_goes_through_the_email_service(cloud_client, billing_files, monkeypatch):
    import email_service

    sent = []

    class FakeService:
        def send(self, message_type, to, subject, text, html=None, metadata=None):
            sent.append((message_type, to, subject))
            return {"status": "sent"}

    monkeypatch.setattr(email_service, "get_email_service", lambda: FakeService())
    cookies = _platform_admin_cookies()
    _save_account(cloud_client, cookies)
    response = cloud_client.post("/api/admin/customers/acct-1/payment-reminder", cookies=cookies)
    assert response.status_code == 200, response.text
    assert sent == [("payment_reminder", "billing@example.test", "AnyAiCam payment reminder")]
    assert "payment_reminder" in email_service.EMAIL_TYPES


def test_payment_reminder_reports_a_failed_send(cloud_client, billing_files, monkeypatch):
    import email_service

    monkeypatch.setattr(email_service, "get_email_service", lambda: type("S", (), {"send": lambda self, *a, **k: {"status": "failed"}})())
    cookies = _platform_admin_cookies()
    _save_account(cloud_client, cookies)
    response = cloud_client.post("/api/admin/customers/acct-1/payment-reminder", cookies=cookies)
    assert response.status_code == 503
    assert "could not be sent" in response.json()["message"]


def test_billing_admin_api_refuses_non_admins(cloud_client, billing_files):
    assert _save_account(cloud_client, {}).status_code in (401, 403)
    customer_cookies = {partner_portal.SESSION_COOKIE: partner_portal._token("owner@example.test", "customer_owner", None, "cust-1", None)}
    assert _save_account(cloud_client, customer_cookies).status_code in (401, 403)


def test_forms_reset_with_event_target_after_await():
    source = (main.Path(main.__file__)).read_text(encoding="utf-8")
    assert "event.currentTarget.reset()" not in source  # currentTarget is null once the handler awaits


# ------------------------------------------------------------ runtime NameErrors / unguarded legacy routes


def test_backup_creation_no_longer_crashes(cloud_client, tmp_path, monkeypatch):
    for name in ("BACKUP_FOLDER", "BACKUPS_FOLDER"):
        monkeypatch.setattr(main, name, tmp_path / "backups")
    (tmp_path / "backups").mkdir()
    monkeypatch.setattr(main, "BACKUP_JOBS_FILE", tmp_path / "backup_jobs.json")
    monkeypatch.setattr(main, "AUDIT_LOG_FILE", tmp_path / "audit.jsonl")
    response = cloud_client.post("/api/backups", json={"label": "admin pass"}, cookies=_legacy_admin_cookies())
    assert response.status_code == 200, response.text[:300]  # was NameError: INVITATIONS_FILE
    assert list((tmp_path / "backups").iterdir()), "no archive was written"


def test_investigation_case_create_and_update_succeed(cloud_client, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "INVESTIGATION_CASES_FILE", tmp_path / "cases.json")
    monkeypatch.setattr(main, "AUDIT_LOG_FILE", tmp_path / "audit.jsonl")
    cookies = _legacy_admin_cookies()
    created = cloud_client.post("/api/investigation-cases", json={"title": "Break-in review"}, cookies=cookies)
    assert created.status_code == 200, created.text[:300]  # was NameError: append_audit_log after saving
    case_id = created.json()["case"]["id"]
    updated = cloud_client.put(f"/api/investigation-cases/{case_id}", json={"title": "Break-in review", "priority": "high", "status": "open"}, cookies=cookies)
    assert updated.status_code == 200, updated.text[:300]
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "investigation.case_created" in audit and "investigation.case_updated" in audit


def _customer_cookies():
    return {partner_portal.SESSION_COOKIE: partner_portal._token("owner@example.test", "customer_owner", None, "cust-1", None)}


@pytest.mark.parametrize("method,path,body", [
    ("PUT", "/api/pricing-config-legacy", {"trial_days": 0}),
    ("GET", "/pricing-admin-legacy", None),
    ("POST", "/api/quotes", {"resolution": "2mp", "recording": "motion", "retention": 7, "quantity": 1}),
    ("PUT", "/api/cameras/1/event-settings", {"camera": 1}),
    ("PUT", "/api/cameras/1/alert-rule", {"camera": 1}),
    ("POST", "/api/analytics/rules", {"camera": 1, "name": "Gate", "analytic_type": "intrusion"}),
    ("POST", "/api/snapshots", {"camera": 1, "image_data": "data:image/png;base64,AAAA"}),
])
def test_customers_cannot_use_admin_or_camera_settings_routes(cloud_client, method, path, body):
    response = cloud_client.request(method, path, json=body, cookies=_customer_cookies(), follow_redirects=False)
    assert response.status_code in (401, 403), (path, response.status_code, response.text[:200])


def test_admin_can_still_edit_legacy_pricing_and_camera_settings(cloud_client, tmp_path, monkeypatch):
    import pricing_config

    saved = {}
    monkeypatch.setattr(pricing_config, "save_pricing", lambda config: saved.update(config))
    import pricing_portal
    monkeypatch.setattr(pricing_portal, "save_pricing", lambda config: saved.update(config))
    monkeypatch.setattr(main, "ALERT_RULES_FILE", tmp_path / "alert_rules.json")
    cookies = _legacy_admin_cookies()
    assert cloud_client.put("/api/pricing-config-legacy", json={"trial_days": 14}, cookies=cookies).status_code == 200
    assert saved.get("trial_days") == 14
    assert cloud_client.put("/api/cameras/1/alert-rule", json={"camera": 1}, cookies=cookies).status_code == 200


def test_audit_log_filters_match_real_roles_and_namespaced_actions(cloud_client, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "AUDIT_LOG_FILE", tmp_path / "audit.jsonl")
    for action, role in (("billing.account_updated", "administrator"), ("investigation.case_created", "administrator"), ("view", "viewer")):
        main.append_audit_entry(main.AuditEntryModel(user_id="u", user_name="u", role=role, action=action, resource="r").model_dump(mode="json"))
    cookies = _legacy_admin_cookies()
    page = cloud_client.get("/audit-logs", cookies=cookies).text
    assert "<option>administrator</option>" in page
    get = lambda **params: [e["action"] for e in cloud_client.get("/api/audit-logs", params=params, cookies=cookies).json()["entries"] if e["action"] != "view" or params.get("action") == "view"]
    assert "billing.account_updated" in get(action="update")
    assert "investigation.case_created" in get(action="create")
    roles = {e["role"] for e in cloud_client.get("/api/audit-logs", params={"role": "administrator"}, cookies=cookies).json()["entries"]}
    assert roles == {"administrator"}


def test_cloud_admin_nav_omits_single_appliance_footage_pages(cloud_client, monkeypatch):
    tabs = _nav_hrefs(cloud_client.get("/admin-portal", cookies=_platform_admin_cookies()).text)
    for href in ("/analytics", "/investigate", "/ai-detection"):
        assert href not in tabs
    monkeypatch.setattr(main, "RUNTIME_ROLE", "edge")
    assert main.navigation_keys_for_role("administrator") >= {"analytics", "investigate", "ai-detection"}
