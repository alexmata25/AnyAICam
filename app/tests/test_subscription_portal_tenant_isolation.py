"""D-AUTHZ-3 (Linux 1.2.7.1): My subscription, subscription requests, billing
support tickets and invoice downloads are scoped to the authenticated
customer's tenant; administrators need an explicit live global principal.

Green IDs: U-A3-1 (real customer principal, anonymous -> 401), U-A3-2
(customer_id filters; admin = live global grant, never manage_settings),
U-A3-3 (no cloud-wide licence/usage to a customer); Z4b/Z4f (B never sees A's
rows), Z4h (detail by id), Z4i (update by id), Z4j (anonymous), Z4k (B's own
usage/licence), Z4l (every row carries its creator's customer_id).
Codex b8c7a7ed D-AUTHZ-3; Red design: quarantine of unattributable rows, no
"primary" billing-account fallback.
"""
import json

import pytest

import main
from authz_helpers import cookies_for, isolated_app, make_partner_user, partner_session_cookie
from partner_db import connection


@pytest.fixture(params=["cloud", "edge"])
def env(request, tmp_path, monkeypatch):
    with isolated_app(tmp_path, monkeypatch, cloud=(request.param == "cloud")) as client:
        make_partner_user("owner-a@customers.test", "customer_owner", partner_id="partner-x", customer_id="cust-a")
        make_partner_user("owner-b@customers.test", "customer_owner", partner_id="partner-x", customer_id="cust-b")
        make_partner_user("viewer-a@customers.test", "customer_viewer", partner_id="partner-x", customer_id="cust-a")
        make_partner_user("sales@partner.test", "partner_owner", partner_id="partner-x")
        make_partner_user("ops@anyaicam.test", "administrator", global_admin=True)
        tokens = {
            "a": partner_session_cookie("owner-a@customers.test", "customer_owner"),
            "b": partner_session_cookie("owner-b@customers.test", "customer_owner"),
            "viewer_a": partner_session_cookie("viewer-a@customers.test", "customer_viewer"),
            "partner": partner_session_cookie("sales@partner.test", "partner_owner"),
            "admin": partner_session_cookie("ops@anyaicam.test", "administrator"),
        }
        yield client, tokens, request.param


def _create_request(client, token, marker, **extra):
    return client.post("/api/subscription-requests", json={"request_type": "camera_increase", "requested_camera_limit": 8,
                                                           "reason": marker, **extra}, cookies=cookies_for(token))


def _create_ticket(client, token, marker, **extra):
    return client.post("/api/billing-support-tickets", json={"subject": marker, "message": marker + " body", **extra}, cookies=cookies_for(token))


def _portal(client, token=None):
    return client.get("/api/subscription-portal", cookies=cookies_for(token) if token else None)


# ------------------------------------------------------------------ U-A3-1 / Z4j


def test_anonymous_callers_get_401_everywhere(env):
    client, _tokens, _ = env
    assert _portal(client).status_code == 401
    assert client.post("/api/subscription-requests", json={"request_type": "camera_increase"}).status_code == 401
    assert client.post("/api/billing-support-tickets", json={"subject": "s", "message": "m"}).status_code == 401
    assert client.put("/api/subscription-requests/x", json={"status": "approved"}).status_code == 401
    assert client.put("/api/billing-support-tickets/x", json={"status": "closed"}).status_code == 401
    assert client.get("/api/customer-invoices/x/download").status_code == 401


def test_rows_carry_the_creators_customer_never_anonymous(env):
    client, tokens, _ = env
    request_row = _create_request(client, tokens["a"], "A-REQ").json()["request"]
    ticket_row = _create_ticket(client, tokens["a"], "A-TKT").json()["ticket"]
    for row in (request_row, ticket_row):
        assert row["customer_id"] == "cust-a"
        assert row["created_by_role"] == "customer_owner"
        assert row["created_by_user_id"] and row["created_by_user_id"] != "anonymous"
        assert row.get("user_id") != "anonymous"
    # Z4l: every stored row is attributed
    for path in (main.SUBSCRIPTION_REQUESTS_FILE, main.BILLING_SUPPORT_TICKETS_FILE):
        for row in json.loads(path.read_text(encoding="utf-8")).values():
            assert row["customer_id"] and row.get("user_id") != "anonymous"


# ------------------------------------------------------------------ Z4b / Z4f


def test_customer_b_never_sees_customer_as_requests_or_tickets(env):
    client, tokens, _ = env
    assert _create_request(client, tokens["a"], "A-REQ-MARKER").status_code == 200
    assert _create_ticket(client, tokens["a"], "A-TKT-MARKER").status_code == 200
    body = _portal(client, tokens["b"])
    assert body.status_code == 200, body.text
    assert "A-REQ-MARKER" not in body.text and "A-TKT-MARKER" not in body.text
    own = _portal(client, tokens["a"]).text
    assert "A-REQ-MARKER" in own and "A-TKT-MARKER" in own


def test_viewer_reads_its_own_tenant_but_cannot_create(env):
    client, tokens, _ = env
    _create_request(client, tokens["a"], "A-REQ-MARKER")
    assert "A-REQ-MARKER" in _portal(client, tokens["viewer_a"]).text
    assert _create_request(client, tokens["viewer_a"], "V").status_code == 403
    assert _create_ticket(client, tokens["viewer_a"], "V").status_code == 403


def test_partner_roles_are_not_billing_principals(env):
    client, tokens, _ = env
    assert _portal(client, tokens["partner"]).status_code == 403
    assert _create_request(client, tokens["partner"], "P").status_code == 403


# ------------------------------------------------------------------ Z4h / Z4i


def test_customers_cannot_update_requests_or_tickets(env):
    client, tokens, _ = env
    request_id = _create_request(client, tokens["a"], "A").json()["request"]["id"]
    ticket_id = _create_ticket(client, tokens["a"], "A").json()["ticket"]["id"]
    before = (main.SUBSCRIPTION_REQUESTS_FILE.read_text(encoding="utf-8"), main.BILLING_SUPPORT_TICKETS_FILE.read_text(encoding="utf-8"))
    assert client.put(f"/api/subscription-requests/{request_id}", json={"status": "approved"}, cookies=cookies_for(tokens["b"])).status_code == 404
    assert client.put(f"/api/billing-support-tickets/{ticket_id}", json={"status": "closed"}, cookies=cookies_for(tokens["b"])).status_code == 404
    assert client.put(f"/api/subscription-requests/{request_id}", json={"status": "approved"}, cookies=cookies_for(tokens["a"])).status_code == 403
    assert client.put(f"/api/billing-support-tickets/{ticket_id}", json={"status": "closed"}, cookies=cookies_for(tokens["a"])).status_code == 403
    assert client.put("/api/subscription-requests/guessed-id", json={"status": "approved"}, cookies=cookies_for(tokens["b"])).status_code == 404
    after = (main.SUBSCRIPTION_REQUESTS_FILE.read_text(encoding="utf-8"), main.BILLING_SUPPORT_TICKETS_FILE.read_text(encoding="utf-8"))
    assert after == before


def test_admin_updates_and_must_name_the_customer_to_create(env):
    client, tokens, _ = env
    request_id = _create_request(client, tokens["a"], "A").json()["request"]["id"]
    updated = client.put(f"/api/subscription-requests/{request_id}", json={"status": "approved", "admin_note": "ok"}, cookies=cookies_for(tokens["admin"]))
    assert updated.status_code == 200, updated.text
    assert _create_request(client, tokens["admin"], "ADM").status_code == 400
    created = _create_request(client, tokens["admin"], "ADM", customer_id="cust-b").json()["request"]
    assert created["customer_id"] == "cust-b" and created["created_by_role"] == "administrator"
    listing = _portal(client, tokens["admin"]).json()
    assert {row["customer_id"] for row in listing["requests"]} >= {"cust-a", "cust-b"}


def test_manage_settings_alone_is_not_an_admin_billing_principal_on_the_cloud(env, monkeypatch):
    client, tokens, profile = env
    if profile != "cloud":
        pytest.skip("cloud rule")
    main.save_users([{"id": "sa", "email": "sa@x.test", "role": "support_admin", "enabled": True, "password_hash": main.hash_password("p" * 12)}])
    token = main.create_session("sa")
    response = client.get("/api/subscription-portal", cookies={main.SESSION_COOKIE_NAME: token})
    assert response.status_code in (401, 403)


# ------------------------------------------------------------------ quarantine


def test_unattributable_legacy_rows_are_quarantined_not_shown_not_deleted(env):
    client, tokens, _ = env
    legacy = {"old-1": {"id": "old-1", "user_id": "anonymous", "reason": "OLD-ANON-MARKER", "status": "pending", "created_at": "2026-10-01"},
              "old-2": {"id": "old-2", "user_id": "cloud-administrator:ops@anyaicam.test", "reason": "OLD-ADMIN-MARKER", "status": "pending",
                        "created_at": "2026-10-01"}}
    main.SUBSCRIPTION_REQUESTS_FILE.write_text(json.dumps(legacy), encoding="utf-8")
    main.BILLING_SUPPORT_TICKETS_FILE.write_text(json.dumps({"t-1": {"id": "t-1", "user_id": "local-admin", "subject": "OLD-TKT-MARKER",
                                                                    "status": "open", "created_at": "2026-10-01"}}), encoding="utf-8")
    for token in (tokens["a"], tokens["b"]):
        text = _portal(client, token).text
        assert "OLD-ANON-MARKER" not in text and "OLD-ADMIN-MARKER" not in text and "OLD-TKT-MARKER" not in text
    stored = json.loads(main.SUBSCRIPTION_REQUESTS_FILE.read_text(encoding="utf-8"))
    assert set(stored) == {"old-1", "old-2"}
    assert all(row["quarantined"] is True and row["quarantine_reason"] and row["quarantined_at"] for row in stored.values())
    assert json.loads(main.BILLING_SUPPORT_TICKETS_FILE.read_text(encoding="utf-8"))["t-1"]["quarantined"] is True
    admin_view = _portal(client, tokens["admin"]).json()
    assert {row["id"] for row in admin_view["requests"] if row.get("quarantined")} == {"old-1", "old-2"}
    # idempotent
    first = main.SUBSCRIPTION_REQUESTS_FILE.read_text(encoding="utf-8")
    main.load_subscription_requests()
    assert main.SUBSCRIPTION_REQUESTS_FILE.read_text(encoding="utf-8") == first


# ------------------------------------------------------------------ account / invoices


def _seed_billing():
    accounts = {
        "primary": {"id": "primary", "customer_name": "PRIMARY-ACCOUNT", "billing_email": "ops@anyaicam.test", "payment_status": "current"},
        "acct-email-b": {"id": "acct-email-b", "customer_name": "EMAIL-MATCH-ACCOUNT", "billing_email": "owner-b@customers.test"},
        "acct-a": {"id": "acct-a", "customer_id": "cust-a", "customer_name": "ACCOUNT-A", "billing_email": "billing@a.test"},
    }
    invoices = {"inv-a": {"id": "inv-a", "account_id": "acct-a", "amount_cents": 1000, "currency": "usd", "status": "paid", "description": "A invoice"},
                "inv-p": {"id": "inv-p", "account_id": "primary", "amount_cents": 5000, "currency": "usd", "status": "paid", "description": "P invoice"}}
    main.BILLING_ACCOUNTS_FILE.write_text(json.dumps(accounts), encoding="utf-8")
    main.BILLING_INVOICES_FILE.write_text(json.dumps(invoices), encoding="utf-8")


def test_account_is_resolved_by_customer_id_only(env):
    client, tokens, _ = env
    _seed_billing()
    b = _portal(client, tokens["b"]).json()
    assert b["account"]["id"] == "unassigned"
    assert "PRIMARY-ACCOUNT" not in json.dumps(b) and "EMAIL-MATCH-ACCOUNT" not in json.dumps(b)
    assert b["invoices"] == []
    a = _portal(client, tokens["a"]).json()
    assert a["account"]["id"] == "acct-a" and [inv["id"] for inv in a["invoices"]] == ["inv-a"]


def test_invoice_download_is_tenant_scoped(env):
    client, tokens, _ = env
    _seed_billing()
    assert client.get("/api/customer-invoices/inv-a/download", cookies=cookies_for(tokens["a"])).status_code == 200
    assert client.get("/api/customer-invoices/inv-a/download", cookies=cookies_for(tokens["b"])).status_code in (403, 404)
    assert client.get("/api/customer-invoices/inv-p/download", cookies=cookies_for(tokens["b"])).status_code in (403, 404)
    assert client.get("/api/customer-invoices/inv-p/download", cookies=cookies_for(tokens["admin"])).status_code == 200


# ------------------------------------------------------------------ U-A3-3 / Z4k


def test_customer_usage_and_licence_are_the_customers_own(env):
    client, tokens, _ = env
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-a','A','2026-01-01')")
        db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-b','cust-b','B','2026-01-01')")
        for index in range(3):
            db.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,created_at) VALUES(?,?,?,?,?,?)",
                       (f"cam-a-{index}", "cust-a", "site-a", f"A{index}", "active", "2026-01-01"))
        db.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,created_at) VALUES('cam-b-0','cust-b','site-b','B0','active','2026-01-01')")
    body = _portal(client, tokens["b"]).json()
    usage, licence = body["usage"], body["license"]
    for leaked in ("s3_bucket", "s3_prefix", "cloudfront_url", "edge_runtime_role", "deployment_environment",
                   "ai_processing_status", "cloud_upload_status", "recording_file_count", "local_storage_bytes"):
        assert leaked not in usage, leaked
    assert usage["configured_cameras"] == 1
    assert licence.get("scope") == "customer"
    assert licence.get("current_camera_count", 0) <= 1
