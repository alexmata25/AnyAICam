"""Partner portal completion pass (2026-09-26).

Two partners (Alpha, Bravo), each with an owner, a salesperson and a
technician, exercise every partner route that reads the older JSON stores:
quotes, installations, the quote detail page, the sales / installations /
performance pages and the legacy customer API. Before this pass none of them
filtered by partner, installations could never be created for a real (SQL)
customer, and the quote builder and quote calculator overwrote each other in
their shared partner_quotes.json.
"""
import json

import pytest
from fastapi.testclient import TestClient

import appliance_identity
import main
import partner_portal
from database_backend import override_target
from partner_db import connection, initialize_database, password_hash

NOW = "2026-09-26T00:00:00"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    for name in ("PARTNER_QUOTES_FILE", "PARTNER_INSTALLATIONS_FILE", "PARTNER_CUSTOMERS_FILE", "AUDIT_LOG_FILE"):
        monkeypatch.setattr(main, name, tmp_path / f"{name.lower()}.json")
    monkeypatch.setattr(partner_portal, "QUOTES_FILE", main.PARTNER_QUOTES_FILE)
    for name in ("ANYAICAM_APPLIANCE_ID", "ANYAICAM_APPLIANCE_CLOUD_ID", "ANYAICAM_APPLIANCE_CREDENTIAL"):
        monkeypatch.delenv(name, raising=False)
    with override_target(sqlite_path=tmp_path / "partner_pass.db"):
        initialize_database()
        with connection() as db:
            for pid, name in (("p-alpha", "Alpha Security"), ("p-bravo", "Bravo Installs")):
                db.execute("INSERT INTO partners(id,name,approval_status,source,created_at,territory) VALUES(?,?,?,?,?,?)", (pid, name, "approved", "real", NOW, f"{name} territory"))
                for role in ("partner_owner", "salesperson", "technician"):
                    db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)",
                               (f"u-{pid}-{role}", pid, f"{role}@{pid}.test", f"{name} {role}", role, password_hash("x-password-123"), 1, NOW))
                db.execute("INSERT INTO customers(id,partner_id,name,company,email,status,trial_status,source,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                           (f"cust-{pid}", pid, f"{name} Client", "", f"owner@cust-{pid}.test", "active", "eligible", "real", NOW))
        with TestClient(main.app) as test_client:
            yield test_client


def cookies(role, partner):
    return {partner_portal.SESSION_COOKIE: partner_portal._token(f"{role}@{partner}.test", role, partner)}


ALPHA = cookies("partner_owner", "p-alpha")
BRAVO = cookies("partner_owner", "p-bravo")


def _bravo_quote(client):
    response = client.post("/api/partner/quotes", json={"customer_id": "cust-p-bravo", "quote_name": "BRAVO SECRET"}, cookies=BRAVO)
    assert response.json()["status"] == "complete", response.text
    return response.json()["quote"]["id"]


def _bravo_installation(client):
    response = client.post("/api/partner/installations", json={"customer_id": "cust-p-bravo", "installer_name": "Bravo Tech"}, cookies=BRAVO)
    assert response.json()["status"] == "complete", response.text  # was always "Partner customer not found."
    return response.json()["installation"]


# ------------------------------------------------------------ quotes


def test_quote_detail_page_is_tenant_scoped(client):
    quote_id = _bravo_quote(client)
    assert "BRAVO SECRET" in client.get(f"/partner/quotes/{quote_id}", cookies=BRAVO).text
    alpha_view = client.get(f"/partner/quotes/{quote_id}", cookies=ALPHA).text
    assert "BRAVO SECRET" not in alpha_view and "Quote not found" in alpha_view


def test_a_partner_cannot_update_another_partners_quote(client):
    quote_id = _bravo_quote(client)
    response = client.put(f"/api/partner/quotes/{quote_id}", json={"quote_name": "ALPHA WAS HERE"}, cookies=ALPHA)
    assert response.json()["status"] == "error"
    stored = [q for q in main.load_partner_quotes() if q["id"] == quote_id][0]
    assert stored["quote_name"] == "BRAVO SECRET"
    own = client.put(f"/api/partner/quotes/{quote_id}", json={"status": "sent"}, cookies=BRAVO)
    assert own.json()["status"] == "complete"


def test_technicians_cannot_create_or_edit_quotes(client):
    tech = cookies("technician", "p-alpha")
    assert client.post("/api/partner/quotes", json={"customer_id": "cust-p-alpha"}, cookies=tech).status_code == 403


# ------------------------------------------------------------ installations


def test_installations_can_be_created_for_real_customers_and_are_stamped(client):
    record = _bravo_installation(client)
    assert record["partner_id"] == "p-bravo"


def test_a_partner_cannot_create_or_update_another_partners_installation(client):
    record = _bravo_installation(client)
    assert client.post("/api/partner/installations", json={"customer_id": "cust-p-bravo"}, cookies=ALPHA).json()["status"] == "error"
    assert client.put(f"/api/partner/installations/{record['id']}", json={"notes": "x"}, cookies=ALPHA).json()["status"] == "error"
    assert client.put(f"/api/partner/installations/{record['id']}", json={"status": "scheduled"}, cookies=BRAVO).json()["status"] == "complete"


# ------------------------------------------------------------ pages


@pytest.mark.parametrize("path", ["/partner-sales", "/partner-installations", "/partner-performance"])
def test_legacy_partner_pages_show_only_own_data(client, path):
    _bravo_quote(client)
    _bravo_installation(client)
    page = client.get(path, cookies=ALPHA).text
    assert "Bravo" not in page and "BRAVO" not in page and "cust-p-bravo" not in page


@pytest.mark.parametrize("path", ["/partner-sales", "/partner-performance"])
def test_technicians_cannot_open_sales_pipeline_pages(client, path):
    assert client.get(path, cookies=cookies("technician", "p-alpha")).status_code == 403


def test_technicians_can_still_open_installations(client):
    assert client.get("/partner-installations", cookies=cookies("technician", "p-alpha")).status_code == 200


# ------------------------------------------------------------ legacy customer API


def test_legacy_customer_api_is_partner_scoped(client):
    created = client.post("/api/partner/customers", json={"name": "Legacy B", "email": "legacy-b@example.test", "status": "active", "site_name": "Main"}, cookies=BRAVO)
    assert created.json()["status"] == "complete", created.text
    assert created.json()["customer"]["partner_id"] == "p-bravo"
    legacy_id = created.json()["customer"]["id"]
    assert client.get("/api/partner/customers", cookies=ALPHA).json()["customers"] == []
    assert [c["id"] for c in client.get("/api/partner/customers", cookies=BRAVO).json()["customers"]] == [legacy_id]
    update = client.put(f"/api/partner/customers/{legacy_id}", json={"name": "Hijack", "email": "legacy-b@example.test", "status": "active", "site_name": "Main"}, cookies=ALPHA)
    assert update.json()["status"] == "error"


# ------------------------------------------------------------ shared quote store


def test_quote_builder_and_calculator_keep_each_others_records(client, monkeypatch):
    import pricing_config

    config = pricing_config.load_pricing()
    config["partner"]["pricing_mode"] = "percentage"
    config["partner"]["percentage_discount"] = 20
    monkeypatch.setattr(pricing_config, "load_pricing", lambda: config)
    monkeypatch.setattr(partner_portal, "calculate_partner_quote", lambda payload: pricing_config.calculate_partner_quote(payload))
    calc = client.post("/api/partner/calculate", json={"resolution": "2mp", "recording": "motion", "retention": 7, "quantity": 3, "addons": [], "customer": "Calc customer", "site": "HQ", "save_quote": True}, cookies=BRAVO)
    assert calc.status_code == 200, calc.text
    quote_id = _bravo_quote(client)  # builder save after the calculator save
    client.post("/api/partner/calculate", json={"resolution": "2mp", "recording": "motion", "retention": 7, "quantity": 2, "addons": [], "customer": "Calc two", "site": "HQ", "save_quote": True}, cookies=BRAVO)
    stored = json.loads(main.PARTNER_QUOTES_FILE.read_text(encoding="utf-8"))
    assert sum("quote_name" in item for item in stored) == 1
    assert sum("quote_name" not in item for item in stored) == 2
    assert [q["id"] for q in main.load_partner_quotes()] == [quote_id]
    assert all("quote_name" not in q for q in partner_portal._read_quotes())


# ------------------------------------------------------------ navigation / chrome


@pytest.mark.parametrize("role", ["partner_owner", "salesperson", "technician"])
def test_partner_roles_get_partner_navigation_only(client, role):
    page = client.get("/partner", cookies=cookies(role, "p-alpha")).text
    mobile = page.split('<nav class="mobile-nav"', 1)[1].split("</nav>", 1)[0]
    for legacy in ('href="/alerts"', 'href="/investigate"', 'href="/dashboard"', 'href="/business-users"', 'href="/sites-management"'):
        assert legacy not in mobile, (role, legacy)
    assert 'href="/partner"' in mobile
    assert "License attention" not in page


def test_workspace_shows_real_partner_details_and_honest_materials(client):
    page = client.get("/partner", cookies=ALPHA).text
    assert "Alpha Security territory" in page
    assert "partner_owner@p-alpha.test" in page and "salesperson@p-alpha.test" in page
    assert "bravo" not in page.lower()
    assert "Partner owner" in page and "· partner_owner</p>" not in page
    assert "comingSoon('Brochures')" not in page and "Coming soon" in page


# ------------------------------------------------------------ team invitations


@pytest.fixture()
def sent_mail(monkeypatch):
    import partner_workspace

    sent = []

    class Mail:
        def send(self, message_type, to, subject, text, html=None, metadata=None):
            sent.append((message_type, to, text))
            return {"status": "sent"}

    monkeypatch.setattr(partner_workspace, "get_email_service", lambda: Mail())
    return sent


def test_customer_owner_cannot_invite_partner_staff(client, sent_mail):
    customer = {partner_portal.SESSION_COOKIE: partner_portal._token("owner@cust-p-alpha.test", "customer_owner", "p-alpha", "cust-p-alpha")}
    for role in ("partner_owner", "salesperson", "technician"):
        response = client.post("/api/partner/users/invite", json={"email": f"x-{role}@example.test", "role": role}, cookies=customer)
        assert response.status_code == 403, (role, response.text)
    with connection() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM partner_users WHERE email LIKE 'x-%'").fetchone()["n"] == 0
    assert sent_mail == []


def test_partner_owner_invites_a_team_member_and_the_email_is_sent(client, sent_mail):
    response = client.post("/api/partner/users/invite", json={"email": "new-tech@example.test", "name": "New Tech", "role": "technician"}, cookies=ALPHA)
    assert response.status_code == 200, response.text
    assert response.json()["email_status"] == "sent"
    assert sent_mail and sent_mail[0][0] == "invitation" and sent_mail[0][1] == "new-tech@example.test"
    with connection() as db:
        created = db.execute("SELECT partner_id, role, must_change_password FROM partner_users WHERE email='new-tech@example.test'").fetchone()
    assert (created["partner_id"], created["role"], created["must_change_password"]) == ("p-alpha", "technician", 1)


def test_salesperson_cannot_invite_and_email_is_required(client, sent_mail):
    assert client.post("/api/partner/users/invite", json={"email": "a@example.test", "role": "technician"}, cookies=cookies("salesperson", "p-alpha")).status_code == 403
    assert client.post("/api/partner/users/invite", json={"email": "not-an-email", "role": "technician"}, cookies=ALPHA).status_code == 400


def test_invite_form_is_offered_to_partner_owners_only(client):
    assert 'id="team-invite-form"' in client.get("/partner", cookies=ALPHA).text
    assert 'id="team-invite-form"' not in client.get("/partner", cookies=cookies("salesperson", "p-alpha")).text


# ------------------------------------------------------------ price sheet / appliances / wizard copy


def test_price_sheet_honours_percentage_pricing(client, monkeypatch):
    import pricing_config

    config = pricing_config.load_pricing()
    config["partner"]["pricing_mode"] = "percentage"
    config["partner"]["percentage_discount"] = 20
    monkeypatch.setattr(partner_portal, "load_pricing", lambda: config)
    page = client.get("/partner-prices", cookies=ALPHA).text
    assert "$6.39" in page  # 7.99 retail less 20%
    assert "20.0%" in page
    assert "2mp.motion.2" not in page and "Motion · 2 days" in page


def test_appliance_dashboard_shows_own_unactivated_appliances_only(client):
    with connection() as db:
        db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-p-alpha','Main',?)", (NOW,))
        db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-a','cust-p-alpha','site-a','AIC-ALPHA-001',?)", (NOW,))
    assert "AIC-ALPHA-001" in client.get("/partner/appliance-dashboard", cookies=cookies("technician", "p-alpha")).text
    assert "AIC-ALPHA-001" not in client.get("/partner/appliance-dashboard", cookies=cookies("technician", "p-bravo")).text


def test_onboarding_wizard_does_not_show_infrastructure_wording(client):
    page = client.get("/partner/onboarding", cookies=ALPHA).text
    assert "send to AWS" not in page and "AWS provisioning result" not in page and "issued by AWS" not in page


# ------------------------------------------------------------ partner applications (platform tool)


def test_partner_applications_require_a_global_administrator(client):
    with connection() as db:
        db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES('u-company-admin','p-alpha','company-admin@p-alpha.test','CA','administrator','x',1,?)", (NOW,))
        appliance_identity.create_grant(db, user_id="u-company-admin", role="administrator", scope_type="partner", scope_id="p-alpha", granted_by="test")
        db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES('u-global','p-alpha','global@anyaicam.test','G','administrator','x',1,?)", (NOW,))
        appliance_identity.create_grant(db, user_id="u-global", role="administrator", scope_type="global", scope_id=None, granted_by="test")
    company_admin = {partner_portal.SESSION_COOKIE: partner_portal._token("company-admin@p-alpha.test", "administrator", "p-alpha")}
    global_admin = {partner_portal.SESSION_COOKIE: partner_portal._token("global@anyaicam.test", "administrator", "p-alpha")}
    assert client.get("/api/admin/partner-applications", cookies=company_admin).status_code == 403
    assert client.put("/api/admin/partner-applications/x", json={"status": "approved"}, cookies=company_admin).status_code == 403
    assert client.get("/api/admin/partner-applications", cookies=global_admin).status_code == 200
    assert client.get("/api/admin/partner-applications", cookies=ALPHA).status_code == 403


# ------------------------------------------------------------ temporary passwords


def test_temporary_password_accounts_must_choose_their_own(client):
    with connection() as db:
        db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at,must_change_password) VALUES('u-temp','p-alpha','temp-tech@p-alpha.test','T','technician',?,1,?,1)",
                   (password_hash("Temp-Pass-12345!"), NOW))
    login = client.post("/api/portal-login", json={"email": "temp-tech@p-alpha.test", "password": "Temp-Pass-12345!", "portal": "technician"}, follow_redirects=False)
    assert login.status_code == 303 and login.headers["location"] == "/change-password"
    session = {partner_portal.SESSION_COOKIE: login.cookies[partner_portal.SESSION_COOKIE]}
    blocked = client.get("/partner/appliance-dashboard", cookies=session, follow_redirects=False)
    assert blocked.status_code == 303 and blocked.headers["location"] == "/change-password"
    assert client.get("/change-password", cookies=session).status_code == 200
    with connection() as db:
        db.execute("UPDATE partner_users SET must_change_password=0 WHERE id='u-temp'")
    assert client.get("/partner/appliance-dashboard", cookies=session, follow_redirects=False).status_code == 200
