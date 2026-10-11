"""Owner-reported production case (read-only evidence, 2026-10-11).

A fully set-up customer signs in and is sent to /customer/setup ("Step 6 --
Review", Hybrid, 1 licensed camera). Their appliance came in through a claim
code: activation_status='pending', state/online_status 'degraded' (checking
in), no activation token, credential created_by='claim' not revoked and used
recently, claim completed; customer_setup_drafts at step 6, not completed;
a billing-v2 Hybrid x1 plan; NO hardware/build order. The account also keeps
an older appliance row for the same box from a previous identity (claim
completed, credential not revoked, last used ~18 h ago, stale 'online',
1.2.4).

Required: sign-in lands on the account (no forced setup); the wizard, if
opened, can reach step 7 and confirming completes it; revoking the appliance
sends the owner back to setup; and none of it touches Stripe -- nor, for a
customer whose deferred storage order was claimed before the claim-activation
cutover, ever starts that storage.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

import main
from database_backend import override_target
from partner_db import password_hash
from test_portal_ui_fixes_20261010 import _owner_cookie, db_path, http_client  # noqa: F401  (fixtures)

PASSWORD = "Owner-pass-20261011!"
EMAIL = "owner@example.test"


@pytest.fixture()
def stripe_calls(monkeypatch):
    calls = {"post": [], "get": []}
    monkeypatch.setattr(main, "stripe_api_post", lambda path, fields=None, idempotency_key=None, **_: calls["post"].append(path) or {})
    monkeypatch.setattr(main, "stripe_api_get", lambda path, *a, **k: calls["get"].append(path) or {})
    return calls


def _ago(**delta):
    return (datetime.now() - timedelta(**delta)).isoformat()


def _seed(db_path, *, old_revoked=False, old_first=True, deferred=None):
    """The production rows. deferred='awaiting' adds a pending deferred
    storage order (paid appliance order) for the pre-cutover variant."""
    conn = sqlite3.connect(db_path)
    now = datetime.now().isoformat()
    conn.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','Partner',?)", (now,))
    conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Real Customer',?,'active',?)",
                 (EMAIL, now))
    conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Home',?)", (now,))
    conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
                 "VALUES('user-1','partner-1',?,'Owner','customer_owner',?,1,'cust-1',?)", (EMAIL, password_hash(PASSWORD), now))

    def appliance(appliance_id, *, created, check_in, state, software, used, revoked):
        conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,state,online_status,last_check_in,"
                     "software_version,created_at) VALUES(?,'cust-1','site-1',?,'pending',?,?,?,?,?)",
                     (appliance_id, f"CLOUD-{appliance_id}".upper(), "revoked" if revoked else state,
                      "revoked" if revoked else state, check_in, software, created))
        conn.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at,created_by,last_used_at,revoked_at) "
                     "VALUES(?,?,'x',?,'claim',?,?)", (f"cred-{appliance_id}", appliance_id, created, used, now if revoked else None))
        conn.execute("INSERT INTO appliance_claims(id,device_id,claim_session_id,claim_code_hash,status,customer_id,site_id,appliance_id,"
                     "expires_at,completed_at,created_at) VALUES(?,?,?,'x','completed','cust-1','site-1',?,?,?,?)",
                     (f"claim-{appliance_id}", f"dev-{appliance_id}", f"sess-{appliance_id}", appliance_id, created, created, created))

    old = dict(created=_ago(days=3), check_in=_ago(hours=18), state="online", software="1.2.4", used=_ago(hours=18), revoked=old_revoked)
    new = dict(created=_ago(hours=17), check_in=_ago(minutes=1), state="degraded", software="1.2.5", used=_ago(minutes=1), revoked=False)
    for appliance_id, spec in ((("appl-old", old), ("appl-new", new)) if old_first else (("appl-new", new), ("appl-old", old))):
        appliance(appliance_id, **spec)
    conn.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,status,device_key,created_at) "
                 "VALUES('cam-1','cust-1','site-1','appl-new',1,'Driveway','configured','urn:uuid:driveway',?)", (now,))
    conn.execute("INSERT INTO camera_plan_entitlements_v2(id,customer_id,plan_key,camera_quantity,stripe_customer_id,stripe_subscription_id,"
                 "stripe_subscription_item_id,stripe_price_id,status,stripe_status,local_retention_days,cloud_event_storage,created_at,updated_at) "
                 "VALUES('v2-1','cust-1','hybrid',1,'cus_real','sub_real','si_real','price_hybrid','active','active',30,1,?,?)", (now, now))
    conn.execute("INSERT INTO customer_setup_drafts(customer_id,current_step,data_json,updated_at,completed_at) VALUES('cust-1',6,'{}',?,NULL)",
                 (now,))
    if deferred:
        conn.execute("INSERT INTO hardware_orders(id,customer_id,sku,product_name,stripe_price_id,quantity,amount_cents,currency,status,"
                     "fulfillment_status,stripe_checkout_session_id,created_at,updated_at) VALUES('hw-1','cust-1','AIC-APPLIANCE-RYZEN-STARTER',"
                     "'AnyAiCam Starter','price_hw',1,124999,'usd','paid','delivered','cs_old',?,?)", (now, now))
        conn.execute("INSERT INTO deferred_storage_plans(customer_id,plan_key,camera_quantity,stripe_customer_id,checkout_session_id,state,"
                     "attempt_round,attempts,created_at,updated_at) VALUES('cust-1','hybrid',1,'cus_real','cs_old','awaiting_activation',0,0,?,?)",
                     (now, now))
        conn.execute("DELETE FROM camera_plan_entitlements_v2 WHERE customer_id='cust-1'")  # storage not started: no plan yet
    conn.commit()
    conn.close()


def _sign_in_and_follow(http_client):
    """POST /api/partner-login (customer_only), then follow every redirect."""
    http_client.cookies.clear()
    response = http_client.post("/api/partner-login", json={"email": EMAIL, "password": PASSWORD, "customer_only": True})
    assert response.status_code in (200, 303), response.text
    location = response.headers.get("location")
    if not location:
        body = response.json()
        location = body.get("redirect") or body.get("destination") or body.get("next")
    visited = []
    for _ in range(8):
        assert location, visited
        visited.append(location)
        page = http_client.get(location)
        if page.status_code not in (301, 302, 303, 307, 308):
            return visited, page
        location = page.headers["location"]
    raise AssertionError(f"redirect loop: {visited}")


def _ready():
    import appliance_readiness
    return appliance_readiness.customer_has_activated_appliance("cust-1")


@pytest.mark.parametrize("old_revoked,old_first", [(False, True), (True, True), (True, False), (False, False)])
def test_sign_in_lands_on_the_account_not_setup(http_client, db_path, stripe_calls, old_revoked, old_first):
    _seed(db_path, old_revoked=old_revoked, old_first=old_first)
    visited, page = _sign_in_and_follow(http_client)
    assert page.status_code == 200, visited
    assert not any(path.startswith("/customer/setup") for path in visited), visited
    account = http_client.get("/customer-account")
    assert account.status_code == 200, account.headers.get("location")
    dashboard = http_client.get("/dashboard", follow_redirects=True)
    assert dashboard.status_code == 200 and "/customer/setup" not in str(dashboard.url)
    assert stripe_calls == {"post": [], "get": []}  # no Stripe request of any kind


def test_readiness_does_not_depend_on_the_older_identity(http_client, db_path):
    _seed(db_path, old_revoked=True)
    with override_target(sqlite_path=db_path):
        assert _ready() is True  # the current appliance alone
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM appliance_credentials WHERE appliance_id='appl-old'")
        conn.execute("DELETE FROM appliance_claims WHERE appliance_id='appl-old'")
        conn.execute("DELETE FROM appliances WHERE id='appl-old'")
        conn.commit()
        conn.close()
        assert _ready() is True


@pytest.mark.parametrize("old_first", [True, False])
def test_the_wizard_reaches_step_7_and_confirm_completes_it(http_client, db_path, stripe_calls, old_first):
    _seed(db_path, old_revoked=True, old_first=old_first)  # the stale identity listed first must not hold it at step 3
    html = http_client.get("/customer/setup", cookies=_owner_cookie()).text
    assert "let setupStep=6," in html  # the saved Review step, not capped at the appliance status step
    assert 'data-step="7"' in html
    assert '"id": "appl-new"' in html and '<option value="appl-new" selected' in html
    confirm = http_client.post("/api/customer/setup/confirm", cookies=_owner_cookie(), json={"appliance_id": "appl-new"})
    assert confirm.status_code == 200, confirm.text
    conn = sqlite3.connect(db_path)
    completed_at, step = conn.execute("SELECT completed_at,current_step FROM customer_setup_drafts WHERE customer_id='cust-1'").fetchone()
    conn.close()
    assert completed_at and step == 7
    assert stripe_calls == {"post": [], "get": []}  # no Stripe request of any kind


def test_revoking_the_appliance_offers_setup_again(http_client, db_path, stripe_calls):
    _seed(db_path, old_revoked=True)  # a revoked older identity never counts
    assert http_client.get("/customer-account", cookies=_owner_cookie()).status_code == 200
    conn = sqlite3.connect(db_path)
    # As appliance_cloud's revoke does: credentials revoked, state='revoked'.
    conn.execute("UPDATE appliance_credentials SET revoked_at=? WHERE appliance_id='appl-new'", (datetime.now().isoformat(),))
    conn.execute("UPDATE appliances SET state='revoked',online_status='revoked',credential_revoked_at=? WHERE id='appl-new'",
                 (datetime.now().isoformat(),))
    conn.commit()
    conn.close()
    with override_target(sqlite_path=db_path):
        assert _ready() is False
        import per_camera_billing
        assert per_camera_billing._setup_needed("cust-1") is True
    response = http_client.get("/customer-account", cookies=_owner_cookie())
    assert response.status_code == 303 and response.headers["location"] == "/customer/setup"
    assert stripe_calls == {"post": [], "get": []}  # no Stripe request of any kind


def test_no_path_calls_stripe_for_this_customer(http_client, db_path, stripe_calls):
    _seed(db_path)
    _sign_in_and_follow(http_client)
    http_client.get("/customer/setup")
    http_client.post("/api/customer/setup/confirm", json={"appliance_id": "appl-new"})
    import build_orders
    with override_target(sqlite_path=db_path):
        assert build_orders.retry_storage_starts() == 0
        assert build_orders.storage_plan("cust-1") is None  # no build order, nothing deferred
    assert stripe_calls == {"post": [], "get": []}


@pytest.mark.parametrize("held", [True, False])
def test_a_pre_cutover_claimed_customer_with_a_pending_storage_order_is_never_auto_started(http_client, db_path, stripe_calls, held):
    _seed(db_path, deferred="awaiting")
    import build_orders
    import db_migrations
    with override_target(sqlite_path=db_path):
        if held:  # the deploy's one-time hold
            conn = sqlite3.connect(db_path)
            conn.execute("DELETE FROM schema_migrations WHERE version=?", (db_migrations.HOLD_CLAIMED_STORAGE_STARTS,))
            conn.commit()
            conn.close()
            db_migrations.apply_migrations()
        expected = "owner_review" if held else "awaiting_activation"
        assert build_orders.storage_plan("cust-1")["state"] == expected
        assert _ready() is True
    visited, page = _sign_in_and_follow(http_client)
    http_client.get("/customer-account")
    http_client.get("/customer/setup")
    http_client.post("/api/customer/setup/confirm", json={"appliance_id": "appl-new"})
    with override_target(sqlite_path=db_path):
        assert build_orders.retry_storage_starts() == 0
        assert build_orders.on_appliance_activated("cust-1")["status"] == "ignored"
        assert build_orders.storage_plan("cust-1")["state"] == expected
    assert stripe_calls == {"post": [], "get": []}  # no Stripe request of any kind


# ------------------------------------------------------------ setup confirmation uses appliance_readiness (Codex, 6627c1b)

def _confirm(http_client, appliance_id="appl-new"):
    return http_client.post("/api/customer/setup/confirm", cookies=_owner_cookie(), json={"appliance_id": appliance_id})


def _completed(db_path):
    conn = sqlite3.connect(db_path)
    found = conn.execute("SELECT completed_at FROM customer_setup_drafts WHERE customer_id='cust-1'").fetchone()[0]
    conn.close()
    return found


def _sql(db_path, *statements):
    conn = sqlite3.connect(db_path)
    for statement, args in statements:
        conn.execute(statement, args)
    conn.commit()
    conn.close()


def test_a_revoked_token_activated_appliance_cannot_confirm_setup(http_client, db_path, stripe_calls):
    _seed(db_path, old_revoked=True)
    revoked = datetime.now().isoformat()
    _sql(db_path, ("UPDATE appliances SET activation_status='activated' WHERE id='appl-new'", ()),  # token-activated
         ("UPDATE appliance_credentials SET revoked_at=? WHERE appliance_id='appl-new'", (revoked,)),
         ("UPDATE appliances SET state='revoked',online_status='revoked',credential_revoked_at=? WHERE id='appl-new'", (revoked,)))
    assert _confirm(http_client).status_code == 409 and _completed(db_path) is None
    # Credential revoked but state not yet updated: still refused.
    _sql(db_path, ("UPDATE appliances SET state='online' WHERE id='appl-new'", ()))
    assert _confirm(http_client).status_code == 409 and _completed(db_path) is None
    assert stripe_calls == {"post": [], "get": []}


def test_a_claimed_appliance_whose_credential_was_never_used_cannot_confirm_setup(http_client, db_path, stripe_calls):
    _seed(db_path, old_revoked=True)
    _sql(db_path, ("UPDATE appliance_credentials SET last_used_at=NULL WHERE appliance_id='appl-new'", ()))
    assert _confirm(http_client).status_code == 409 and _completed(db_path) is None
    # ...nor one whose only heartbeat predates the credential (enrollment rolled back).
    _sql(db_path, ("UPDATE appliance_credentials SET last_used_at=? WHERE appliance_id='appl-new'", (_ago(minutes=1),)),
         ("UPDATE appliances SET last_check_in=? WHERE id='appl-new'", (_ago(days=2),)))
    assert _confirm(http_client).status_code == 409 and _completed(db_path) is None
    assert stripe_calls == {"post": [], "get": []}


def test_the_production_case_still_confirms(http_client, db_path, stripe_calls):
    _seed(db_path)  # exactly as reported, the non-revoked older identity included
    assert _confirm(http_client).status_code == 200 and _completed(db_path)
    assert stripe_calls == {"post": [], "get": []}


def test_token_recovery_is_refused_for_claimed_and_revoked_appliances(http_client, db_path, stripe_calls):
    _seed(db_path, old_revoked=True)
    for appliance_id in ("appl-new", "appl-old"):  # claimed and in use; revoked
        response = http_client.post(f"/api/customer/appliances/{appliance_id}/activation-token/recover", cookies=_owner_cookie())
        assert response.status_code == 409, (appliance_id, response.text)
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM appliance_activation_tokens").fetchone()[0] == 0
    conn.close()
    assert stripe_calls == {"post": [], "get": []}
