"""Partner agreements, partner upcharges, activation fees, the 20% residual
and quarterly payouts (owner rules, 2026-10-04): partner_agreements.py,
partner_payouts.py, sales_commissions.py, the camera-plan checkout and the
Partner Portal pages."""
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

import pricing_catalog as pc
from database_backend import override_target

pytestmark = pytest.mark.usefixtures("stripe_follows_events")

ADMIN = {"email": "admin@example.test", "role": "administrator", "partner_id": None}
SALES = {"email": "sales@example.test", "role": "salesperson", "partner_id": "partner-1"}
OWNER_P1 = {"email": "boss@example.test", "role": "partner_owner", "partner_id": "partner-1"}
SALES_P2 = {"email": "rival@example.test", "role": "salesperson", "partner_id": "partner-2"}


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "partner-billing.db"
    with override_target(sqlite_path=str(path)):
        from partner_db import initialize_database
        initialize_database()
        conn = sqlite3.connect(path)
        conn.executescript("""
INSERT INTO partners(id,name,created_at) VALUES('partner-1','Acme Security','2026-01-01'),('partner-2','Rival Alarms','2026-01-01');
INSERT INTO customers(id,partner_id,name,email,status,created_at,created_by) VALUES
  ('cust-1','partner-1','Real Customer','owner@example.test','active','2026-01-01','sales@example.test'),
  ('cust-2','partner-2','Other Customer','other@example.test','active','2026-01-01','rival@example.test');
INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES
  ('sales-1','partner-1','sales@example.test','Sam Sales','salesperson','x',1,'2026-01-01'),
  ('boss-1','partner-1','boss@example.test','Bo Boss','partner_owner','x',1,'2026-01-01'),
  ('rival-1','partner-2','rival@example.test','Rae Rival','salesperson','x',1,'2026-01-01');
INSERT INTO partner_users(id,email,name,role,password_hash,approved,created_at) VALUES('admin-1','admin@example.test','Admin','administrator','x',1,'2026-01-01');
INSERT INTO identity_grants(id,user_id,role,scope_type,granted_at) VALUES('grant-1','admin-1','administrator','global','2026-01-01');
""")
        conn.commit()
        conn.close()
        yield path


CHARGES: dict = {}


def stripe_fee(amount):
    """The fake Stripe fee: 30 cents + 2.9%."""
    return 30 + amount * 29 // 1000


def partner_fee(partner_cents, charge_cents):
    return (2 * stripe_fee(charge_cents) * partner_cents + charge_cents) // (2 * charge_cents)


@pytest.fixture(autouse=True)
def stripe_fees(monkeypatch):
    """Charges answer with their balance transaction's fee; set
    state["available"] = False for a fee Stripe has not reported yet."""
    import main
    CHARGES.clear()
    state = {"available": True}
    original = main.stripe_api_get

    def charge(path):
        charge_id = path.split("/")[3].split("?")[0]
        amount = CHARGES[charge_id]
        balance = {"id": f"txn_{charge_id}", "fee": stripe_fee(amount)} if state["available"] else f"txn_{charge_id}"
        return {"id": charge_id, "amount": amount, "balance_transaction": balance}

    def fake(path):
        return charge(path) if path.startswith("/v1/charges/") else original(path)

    state["charge"] = charge
    monkeypatch.setattr(main, "stripe_api_get", fake)
    return state


@pytest.fixture()
def prices(monkeypatch):
    import analytics_entitlements as ae
    import customer_entitlements as ce
    for env_var, value in (("ANYAICAM_STRIPE_PRICE_LOCAL_1_8", "price_local_8"), ("ANYAICAM_STRIPE_PRICE_LOCAL_9_16", "price_local_16"),
                           ("ANYAICAM_STRIPE_PRICE_ADVANCED_ANALYTICS", "price_adv")):
        monkeypatch.setenv(env_var, value)
    monkeypatch.setattr(ae, "ANALYTICS_PRICE_MAP", ae._load_price_map())
    monkeypatch.setattr(ce, "PRICE_ID_CAMERA_SLOT_MAP", ce._load_price_tier_map())


@pytest.fixture()
def pa(db_path, prices):
    import partner_agreements
    yield partner_agreements


def _sql(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    result = [dict(r) for r in conn.execute(query, params).fetchall()]
    conn.commit()
    conn.close()
    return result


def _agree(pa, *, identity=ADMIN, upcharge=500, fee=9900, billing=pc.BILLING_ANYAICAM, tier="1-8", customer="cust-1", seller="sales-1"):
    return pa.propose(identity, customer_id=customer, plan_type="local", tier_label=tier, upcharge_cents=upcharge,
                      activation_fee_cents=fee, billing_party=billing, salesperson=seller)


def _invoice(event_id, invoice_id, lines, *, agreement_id=None, tax=0, sub="sub_ag"):
    metadata = {"anyaicam_customer_id": "cust-1"}
    if agreement_id:
        metadata["anyaicam_agreement_id"] = agreement_id
    data = []
    for kind, cents in lines:
        if kind == "base":
            data.append({"price": {"id": "price_local_8"}, "amount": cents, "type": "subscription"})
        elif kind == "addon":
            data.append({"price": {"id": "price_adv"}, "amount": cents, "type": "subscription"})
        elif kind == "upcharge":
            data.append({"price": {"id": "price_inline_up", "type": "recurring"}, "amount": cents, "type": "subscription"})
        elif kind == "fee":
            data.append({"price": {"id": "price_inline_fee", "type": "one_time"}, "amount": cents, "type": "invoiceitem"})
    CHARGES[f"ch_{invoice_id}"] = sum(c for _, c in lines) + tax
    return {"id": event_id, "type": "invoice.paid", "data": {"object": {
        "id": invoice_id, "object": "invoice", "customer": "cus_1", "subscription": sub,
        "amount_paid": sum(c for _, c in lines) + tax, "tax": tax, "currency": "usd", "charge": f"ch_{invoice_id}",
        "payment_intent": f"pi_{invoice_id}", "subscription_details": {"metadata": metadata}, "lines": {"data": data}}}}


def _sync(event):
    import sales_commissions
    return sales_commissions.sync_commissions_from_stripe_event(event)


def _ledger(kind=None):
    import sales_commissions
    return [e for e in sales_commissions.ledger_for() if kind is None or e["kind"] == kind]


def _originals(kind):
    return [e for e in _ledger(kind) if not e["reverses_entry_id"]]


# ================================================================ agreement records

def test_agreement_stores_base_upcharge_and_customer_price_separately(pa):
    agreement = _agree(pa, upcharge=500, fee=9900)
    assert (agreement["base_price_cents"], agreement["upcharge_cents"], agreement["customer_price_cents"]) == (1499, 500, 1999)
    assert agreement["activation_fee_cents"] == 9900 and agreement["billing_party"] == "ANYAICAM_BILLED"
    assert (agreement["commission_rate_bps"], agreement["commission_max_paid_months"]) == (2000, 12)
    assert agreement["camera_tier"] == 8 and agreement["salesperson_user_id"] == "sales-1" and agreement["partner_id"] == "partner-1"
    assert agreement["status"] == "approved"  # a global administrator's agreement takes effect as made
    # The authoritative catalog is never touched by a partner's selling price.
    assert pc.find_base_plan("local", "1-8")["monthly_cents"] == 1499


def test_the_base_price_comes_from_the_catalog_never_from_the_caller(pa):
    agreement = pa.propose(ADMIN, customer_id="cust-1", plan_type="local", tier_label="9-16", upcharge_cents=0,
                           activation_fee_cents=0, billing_party=pc.BILLING_PARTNER, salesperson="sales-1")
    assert agreement["base_price_cents"] == 2499 and agreement["customer_price_cents"] == 2499


@pytest.mark.parametrize("change, message", [
    ({"upcharge": -1}, "cannot be negative"), ({"upcharge": "5.00"}, "in cents"), ({"fee": 1.5}, "in cents"),
    ({"upcharge": True}, "in cents"), ({"tier": "1-9"}, "Choose an AnyAiCam plan"), ({"billing": "CUSTOMER_BILLED"}, "who bills"),
])
def test_invalid_terms_are_refused(pa, change, message):
    with pytest.raises(pa.AgreementError, match=message):
        _agree(pa, **change)


def test_a_partner_proposal_waits_for_global_admin_approval_and_is_audited(pa, db_path):
    proposal = _agree(pa, identity=SALES, seller=None)
    assert proposal["status"] == "pending_approval" and proposal["salesperson_user_id"] == "sales-1"
    assert pa.in_force("cust-1") is None
    with pytest.raises(pa.AgreementForbidden):
        pa.approve(OWNER_P1, proposal["id"])  # a partner cannot approve its own upcharge
    approved = pa.approve(ADMIN, proposal["id"])
    assert approved["status"] == "approved" and approved["approved_by"] == "admin@example.test"
    actions = [r["action"] for r in _sql(db_path, "SELECT action FROM audit_logs WHERE entity_id=? ORDER BY id", (proposal["id"],))]
    assert actions == ["partner_agreement.proposed", "partner_agreement.approved"]
    details = _sql(db_path, "SELECT details_json FROM audit_logs WHERE action='partner_agreement.approved'")[0]["details_json"]
    assert '"upcharge_cents": 500' in details and '"base_price_cents": 1499' in details


def test_a_partner_never_sees_or_changes_another_partners_customer(pa):
    mine = _agree(pa)
    with pytest.raises(pa.AgreementForbidden):
        _agree(pa, identity=SALES_P2, seller=None)
    with pytest.raises(pa.AgreementForbidden):
        pa.get(SALES_P2, mine["id"])
    assert pa.visible(SALES_P2) == [] and [a["id"] for a in pa.visible(OWNER_P1)] == [mine["id"]]
    with pytest.raises(pa.AgreementError, match="belong to this customer's partner"):
        _agree(pa, seller="rival-1")


def test_a_change_is_a_new_version_and_the_old_terms_are_kept(pa, db_path):
    first = _agree(pa, upcharge=500, billing=pc.BILLING_PARTNER)
    second = _agree(pa, upcharge=700, billing=pc.BILLING_PARTNER)
    rows = {r["id"]: r for r in _sql(db_path, "SELECT * FROM partner_agreements")}
    assert rows[first["id"]]["status"] == "superseded" and rows[first["id"]]["upcharge_cents"] == 500
    assert rows[second["id"]]["previous_agreement_id"] == first["id"] and rows[second["id"]]["version"] == 2
    assert pa.in_force("cust-1")["id"] == second["id"]


# ================================================================ AnyAiCam billing: checkout

@pytest.fixture()
def portal(db_path, prices, tmp_path, monkeypatch, fake_stripe_prices, stripe_fees):
    import main
    prices_get = main.stripe_api_get  # catalog price checks, plus charges for processing fees
    monkeypatch.setattr(main, "stripe_api_get",
                        lambda path: stripe_fees["charge"](path) if path.startswith("/v1/charges/") else prices_get(path))
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://app.example.test")
    monkeypatch.setenv("ANYAICAM_STRIPE_COUPON_FRIENDS_FAMILY_BASE", "coupon_ff_base")
    monkeypatch.setenv("ANYAICAM_STRIPE_COUPON_FRIENDS_FAMILY_ANALYTICS", "coupon_ff_analytics")
    captured = []

    def _fake_stripe(path, fields, idempotency_key=None):
        if path == "/v1/customers":
            return {"id": "cus_test_1"}
        if path.endswith("/expire"):
            return {"id": path.split("/")[4], "status": "expired"}
        captured.append(list(fields))
        return {"id": f"cs_{len(captured)}", "url": f"https://checkout.stripe.test/{len(captured)}"}

    monkeypatch.setattr(main, "stripe_api_post", _fake_stripe)
    with TestClient(main.app, follow_redirects=False) as client:
        yield client, captured


def _cookie(email, role, customer_id=None, partner_id=None):
    import partner_portal
    return {partner_portal.SESSION_COOKIE: partner_portal._token(email, role, partner_id, customer_id, None)}


CUSTOMER = _cookie("owner@example.test", "customer_owner", "cust-1")


def _checkout(client, tier="1-8", **extra):
    return client.post("/api/customer/camera-slots/checkout", json={"plan_type": "local", "tier_label": tier, **extra}, cookies=CUSTOMER)


def test_anyaicam_billed_checkout_charges_the_catalog_plan_plus_the_agreed_upcharge_and_fee(portal):
    import partner_agreements as pa
    client, captured = portal
    agreement = _agree(pa, upcharge=500, fee=9900)
    response = _checkout(client, upcharge_cents=1, line_items=[{"price": "price_evil"}])  # browser extras are ignored
    assert response.status_code == 200, response.text
    fields = dict(captured[-1])
    assert fields["line_items[0][price]"] == "price_local_8"  # the verified catalog Price, unchanged
    assert fields["line_items[1][price_data][unit_amount]"] == "500"
    assert fields["line_items[1][price_data][recurring][interval]"] == "month"
    assert fields["line_items[1][price_data][product_data][name]"] == "Acme Security service (monthly)"
    assert fields["line_items[2][price_data][unit_amount]"] == "9900"
    assert "line_items[2][price_data][recurring][interval]" not in fields  # the fee is one time
    assert fields["subscription_data[metadata][anyaicam_agreement_id]"] == agreement["id"]
    assert not any(v == "price_evil" for v in fields.values())


def test_a_customer_without_an_agreement_checks_out_exactly_as_before(portal):
    client, captured = portal
    assert _checkout(client).status_code == 200
    assert not any("price_data" in k or "agreement" in k for k, _ in captured[-1])


@pytest.mark.parametrize("setup, detail", [
    ("partner_billed", "billed by your partner"),
    ("other_plan", "agreement with your partner is for the Local 8 cameras plan"),
    ("price_changed", "price for this plan changed"),
    ("friends_family", "Friends & Family"),
])
def test_checkout_is_refused_when_it_would_disagree_with_the_agreement(portal, db_path, setup, detail):
    import partner_agreements as pa
    client, captured = portal
    agreement = _agree(pa, billing=pc.BILLING_PARTNER if setup == "partner_billed" else pc.BILLING_ANYAICAM)
    tier = "9-16" if setup == "other_plan" else "1-8"
    if setup == "price_changed":
        _sql(db_path, "UPDATE partner_agreements SET base_price_cents=1399 WHERE id=?", (agreement["id"],))
    if setup == "friends_family":  # approved before the agreement guard existed: checkout still refuses
        _sql(db_path, "INSERT INTO friends_family_requests(id,customer_id,status,requested_at,decided_at,decided_by) "
                      "VALUES('ff-old','cust-1','approved','2026-09-01','2026-09-02','admin@example.test')")
    response = _checkout(client, tier=tier)
    assert response.status_code == 409 and detail in response.json()["detail"]
    assert captured == []  # no Checkout Session was created


# ================================================================ AnyAiCam billing: what was collected

def test_a_paid_invoice_splits_into_residual_upcharge_and_activation_fee(pa, db_path):
    agreement = _agree(pa, upcharge=500, fee=9900)
    result = _sync(_invoice("evt_1", "in_1", [("base", 1499), ("upcharge", 500), ("fee", 9900)], agreement_id=agreement["id"], tax=120))
    assert sorted(result["kinds"]) == ["partner_activation_fee", "partner_upcharge", "recurring"]
    residual = _ledger("recurring")[0]
    assert (residual["basis_cents"], residual["amount_cents"], residual["commission_rate_bps"]) == (1499, 300, 2000)  # 20% of $14.99 only
    assert _ledger("partner_upcharge")[0]["amount_cents"] == 500 and _ledger("partner_activation_fee")[0]["amount_cents"] == 9900
    assert all(e["agreement_id"] == agreement["id"] and e["billing_party"] == "ANYAICAM_BILLED" for e in _ledger())
    stored = pa.in_force("cust-1")
    assert stored["status"] == "active" and stored["stripe_subscription_id"] == "sub_ag"
    assert stored["activation_fee_collected_at"] and stored["commission_start_at"] and not stored["commission_end_at"]
    # Month 2: no fee line; the upcharge and the residual continue.
    _sync(_invoice("evt_2", "in_2", [("base", 1499), ("upcharge", 500)], agreement_id=agreement["id"]))
    assert len(_ledger("partner_upcharge")) == 2 and len(_ledger("partner_activation_fee")) == 1 and len(_ledger("recurring")) == 2


def test_add_ons_are_anyaicam_revenue_and_earn_the_residual(pa):
    agreement = _agree(pa, upcharge=500, fee=0)
    _sync(_invoice("evt_1", "in_1", [("base", 1499), ("addon", 2499), ("upcharge", 500)], agreement_id=agreement["id"]))
    assert _ledger("recurring")[0]["basis_cents"] == 3998 and _ledger("recurring")[0]["amount_cents"] == 800


def test_a_settled_invoice_is_never_counted_twice(pa):
    agreement = _agree(pa)
    event = _invoice("evt_1", "in_1", [("base", 1499), ("upcharge", 500), ("fee", 9900)], agreement_id=agreement["id"])
    _sync(event)
    _sync(event)
    assert sorted(e["kind"] for e in _ledger()) == ["partner_activation_fee", "partner_processing_fee", "partner_upcharge", "recurring"]


def test_residual_stops_after_12_paid_months_but_the_upcharge_continues(pa):
    agreement = _agree(pa, upcharge=500, fee=0)
    for month in range(1, 15):
        _sync(_invoice(f"evt_{month}", f"in_{month}", [("base", 1499), ("upcharge", 500)], agreement_id=agreement["id"]))
    assert sorted(e["paid_month_index"] for e in _ledger("recurring")) == list(range(1, 13))
    assert len(_ledger("partner_upcharge")) == 14
    assert pa.in_force("cust-1")["commission_end_at"]


def test_failed_invoices_earn_nothing(pa):
    agreement = _agree(pa)
    _sync(_invoice("evt_0", "in_0", [], agreement_id=agreement["id"]))
    _sync({"id": "evt_f", "type": "invoice.payment_failed", "data": {"object": {"id": "in_f"}}})
    assert _ledger() == []


def test_a_refund_appends_proportional_reversals_and_never_changes_the_originals(pa):
    agreement = _agree(pa, upcharge=500, fee=9900)
    _sync(_invoice("evt_1", "in_1", [("base", 1499), ("upcharge", 500), ("fee", 9900)], agreement_id=agreement["id"]))
    half = {"id": "evt_r", "type": "charge.refunded", "data": {"object": {"object": "charge", "id": "ch_in_1", "payment_intent": "pi_in_1",
                                                                         "invoice": "in_1", "amount": 11899, "amount_refunded": 5950}}}
    _sync(half)
    import sales_commissions
    originals = {e["kind"]: e for e in _ledger() if not e["reverses_entry_id"] and e["kind"] != "partner_processing_fee"}
    assert {k: e["amount_cents"] for k, e in originals.items()} == {"recurring": 300, "partner_upcharge": 500, "partner_activation_fee": 9900}
    assert {k: sales_commissions.net_amount(e) for k, e in originals.items()} == {"recurring": 150, "partner_upcharge": 250,
                                                                                  "partner_activation_fee": 4950}
    _sync({"id": "evt_d", "type": "charge.dispute.created", "data": {"object": {"object": "dispute", "charge": "ch_in_1",
                                                                              "payment_intent": "pi_in_1", "amount": 11899}}})
    assert all(sales_commissions.net_amount(e) == 0 for e in _originals("recurring") + _originals("partner_upcharge"))
    # Stripe keeps its processing fee on a refund, so the partner's share stays deducted.
    assert sales_commissions.summarize(_ledger())["earned_cents"] == -partner_fee(10400, 11899)


def test_cancelling_the_subscription_ends_the_agreement_and_keeps_what_was_earned(pa):
    agreement = _agree(pa, upcharge=500, fee=0)
    _sync(_invoice("evt_1", "in_1", [("base", 1499), ("upcharge", 500)], agreement_id=agreement["id"]))
    result = _sync({"id": "evt_c", "type": "customer.subscription.deleted", "data": {"object": {"id": "sub_ag", "object": "subscription"}}})
    assert result["status"] == "agreement_ended"
    from partner_db import row
    assert row("SELECT status FROM partner_agreements WHERE id=?", (agreement["id"],))["status"] == "ended"
    assert [e["amount_cents"] for e in _ledger("recurring")] == [300]


def test_a_running_subscription_cannot_be_repriced_by_a_new_agreement(pa):
    agreement = _agree(pa, upcharge=500, fee=0)
    _sync(_invoice("evt_1", "in_1", [("base", 1499), ("upcharge", 500)], agreement_id=agreement["id"]))
    with pytest.raises(pa.AgreementError, match="already running"):
        _agree(pa, upcharge=900, fee=0)
    with pytest.raises(pa.AgreementError, match="Cancel the subscription"):
        pa.cancel(ADMIN, agreement["id"], "test")


# ================================================================ partner billing

def test_partner_billed_collections_create_amounts_owed_and_remittances_earn_the_residual(pa):
    agreement = _agree(pa, upcharge=500, fee=5000, billing=pc.BILLING_PARTNER)
    due = pa.record_collection(SALES, agreement["id"], service_month="2026-01")
    assert (due["kind"], due["amount_cents"], due["collected_cents"], due["service_month"]) == ("partner_base_due", -1499, 1999, "2026-01")
    with pytest.raises(pa.AgreementError, match="already recorded"):
        pa.record_collection(SALES, agreement["id"], service_month="2026-01")
    with pytest.raises(pa.AgreementForbidden):
        pa.record_remittance(SALES, agreement["id"], service_month="2026-01", amount_cents=1499, reference="wire-1")
    with pytest.raises(pa.AgreementError, match="still owed"):
        pa.record_remittance(ADMIN, agreement["id"], service_month="2026-01", amount_cents=1500, reference="wire-1")
    with pytest.raises(pa.AgreementError, match="collection for 2026-02 first"):
        pa.record_remittance(ADMIN, agreement["id"], service_month="2026-02", amount_cents=100, reference="wire-x")
    result = pa.record_remittance(ADMIN, agreement["id"], service_month="2026-01", amount_cents=1499, reference="wire-1")
    assert result["remittance"]["amount_cents"] == 1499 and result["commission"]["amount_cents"] == 300
    assert result["commission"]["basis_cents"] == 1499 and result["paid_month_index"] == 1
    with pytest.raises(pa.AgreementError, match="already recorded"):
        pa.record_remittance(ADMIN, agreement["id"], service_month="2026-01", amount_cents=1, reference="wire-1")
    import sales_commissions
    # Owed -14.99, paid +14.99, residual +3.00: the partner is owed $3.00 net.
    assert sales_commissions.summarize(_ledger())["earned_cents"] == 300


def test_partner_billed_residual_also_stops_after_12_paid_months(pa):
    agreement = _agree(pa, upcharge=0, fee=0, billing=pc.BILLING_PARTNER)
    for month in range(1, 14):
        service_month = f"2025-{month:02d}" if month <= 12 else "2026-01"
        pa.record_collection(SALES, agreement["id"], service_month=service_month)
        pa.record_remittance(ADMIN, agreement["id"], service_month=service_month, amount_cents=1499, reference=f"wire-{month}")
    assert len(_ledger("recurring")) == 12 and len(_ledger("partner_base_remitted")) == 13


def test_a_bounced_remittance_is_reversed_with_its_residual(pa):
    agreement = _agree(pa, upcharge=0, fee=0, billing=pc.BILLING_PARTNER)
    pa.record_collection(SALES, agreement["id"], service_month="2026-01")
    remitted = pa.record_remittance(ADMIN, agreement["id"], service_month="2026-01", amount_cents=1499, reference="wire-1")
    with pytest.raises(pa.AgreementForbidden):
        pa.reverse_partner_entry(SALES, remitted["remittance"]["id"], reason="bounced")
    reversals = pa.reverse_partner_entry(ADMIN, remitted["remittance"]["id"], reason="bounced")
    assert sorted(r["amount_cents"] for r in reversals) == [-1499, -300]
    import sales_commissions
    assert sales_commissions.summarize(_ledger())["earned_cents"] == -1499  # the month is owed again
    with pytest.raises(pa.AgreementError, match="already reversed"):
        pa.reverse_partner_entry(ADMIN, remitted["remittance"]["id"], reason="again")


def test_a_month_the_partner_refunded_to_its_customer_is_no_longer_owed(pa):
    agreement = _agree(pa, upcharge=500, fee=0, billing=pc.BILLING_PARTNER)
    due = pa.record_collection(SALES, agreement["id"], service_month="2026-01")
    reversals = pa.reverse_partner_entry(ADMIN, due["id"], reason="partner refunded the customer")
    assert [r["amount_cents"] for r in reversals] == [1499] and reversals[0]["reverses_entry_id"] == due["id"]
    import sales_commissions
    assert sales_commissions.net_amount(due) == 0 and sales_commissions.summarize(_ledger())["earned_cents"] == 0
    assert due["amount_cents"] == -1499  # the original never changes


def test_service_months_cannot_be_in_the_future_or_malformed(pa):
    agreement = _agree(pa, billing=pc.BILLING_PARTNER)
    for bad in ("2026-13", "26-01", "2999-01", ""):
        with pytest.raises(pa.AgreementError):
            pa.record_collection(SALES, agreement["id"], service_month=bad)


def test_anyaicam_billed_customers_are_not_recorded_by_hand(pa):
    agreement = _agree(pa)
    with pytest.raises(pa.AgreementError, match="recorded from Stripe"):
        pa.record_collection(SALES, agreement["id"], service_month="2026-01")


# ================================================================ quarterly payouts

def _date(db_path, when):
    _sql(db_path, "UPDATE commission_ledger SET earned_at=? WHERE earned_at > '2026-06-01'", (when,))


def test_quarterly_statements_carry_a_negative_balance_forward(pa, db_path):
    import partner_payouts as pp
    agreement = _agree(pa, upcharge=500, fee=9900)
    _sync(_invoice("evt_1", "in_1", [("base", 1499), ("upcharge", 500), ("fee", 9900)], agreement_id=agreement["id"]))
    _date(db_path, "2026-02-10T10:00:00")
    with pytest.raises(pp.PayoutError, match="has not ended"):
        pp.close_quarter(ADMIN, "2026-Q1", today=date(2026, 3, 31))
    with pytest.raises(pa.AgreementForbidden):
        pp.close_quarter(OWNER_P1, "2026-Q1", today=date(2026, 4, 1))
    q1 = pp.close_quarter(ADMIN, "2026-Q1", today=date(2026, 4, 1))
    assert len(q1) == 1 and q1[0]["payee_key"] == "partner-1"
    fee1 = partner_fee(10400, 11899)
    q1_total = 300 + 500 + 9900 - fee1
    assert (q1[0]["opening_balance_cents"], q1[0]["period_total_cents"], q1[0]["payable_cents"], q1[0]["status"]) == (0, q1_total, q1_total, "issued")
    assert pp.close_quarter(ADMIN, "2026-Q1", today=date(2026, 4, 1)) == []  # idempotent
    # A full refund in Q2, after Q1 was issued: the Q1 statement never changes.
    _sync({"id": "evt_r", "type": "charge.refunded", "data": {"object": {"object": "charge", "id": "ch_in_1", "payment_intent": "pi_in_1",
                                                                         "invoice": "in_1", "amount": 11899, "amount_refunded": 11899}}})
    _date(db_path, "2026-05-01T09:00:00")
    q2 = pp.close_quarter(ADMIN, "2026-Q2", today=date(2026, 7, 1))[0]
    assert (q2["opening_balance_cents"], q2["period_total_cents"], q2["payable_cents"], q2["carried_forward_cents"], q2["status"]) == \
        (0, -10700, 0, -10700, "nothing_payable")
    assert _sql(db_path, "SELECT payable_cents FROM partner_payouts WHERE quarter='2026-Q1'")[0]["payable_cents"] == q1_total
    # Q3: new earnings are first applied to the carried-forward balance.
    _sync(_invoice("evt_2", "in_2", [("base", 1499), ("upcharge", 500)], agreement_id=agreement["id"]))
    _date(db_path, "2026-08-01T09:00:00")
    q3 = pp.close_quarter(ADMIN, "2026-Q3", today=date(2026, 10, 1))[0]
    q3_total = 300 + 500 - partner_fee(500, 1999)
    assert (q3["opening_balance_cents"], q3["period_total_cents"], q3["payable_cents"], q3["carried_forward_cents"]) ==         (-10700, q3_total, 0, -10700 + q3_total)
    # Every entry is on exactly one statement, with its payment-level detail.
    items = _sql(db_path, "SELECT payout_id, COUNT(*) AS n FROM partner_payout_items GROUP BY payout_id")
    assert sum(i["n"] for i in items) == len(_ledger())
    detail = pp.statement(ADMIN, q2["id"])
    assert {e["kind"] for e in detail["entries"]} == {"recurring", "partner_upcharge", "partner_activation_fee"}
    assert all(e["reverses_entry_id"] for e in detail["entries"])


def test_payouts_are_marked_paid_once_with_a_reference(pa, db_path):
    import partner_payouts as pp
    agreement = _agree(pa, upcharge=500, fee=0)
    _sync(_invoice("evt_1", "in_1", [("base", 1499), ("upcharge", 500)], agreement_id=agreement["id"]))
    _date(db_path, "2026-02-10T10:00:00")
    statement = pp.close_quarter(ADMIN, "2026-Q1", today=date(2026, 4, 1))[0]
    with pytest.raises(pp.PayoutError, match="reference"):
        pp.mark_paid(ADMIN, statement["id"], reference="")
    with pytest.raises(pp.PayoutError, match="no verified payout bank account"):
        pp.mark_paid(ADMIN, statement["id"], reference="ACH-123")
    _verified_bank(db_path, "partner-1")
    paid = pp.mark_paid(ADMIN, statement["id"], reference="ACH-123")
    assert paid["status"] == "paid" and paid["payment_reference"] == "ACH-123"
    with pytest.raises(pp.PayoutError, match="Only an issued statement"):
        pp.mark_paid(ADMIN, statement["id"], reference="ACH-124")


def test_payout_statements_are_scoped_to_the_payee(pa, db_path):
    import partner_payouts as pp
    agreement = _agree(pa, upcharge=500, fee=0)
    _sync(_invoice("evt_1", "in_1", [("base", 1499), ("upcharge", 500)], agreement_id=agreement["id"]))
    _date(db_path, "2026-02-10T10:00:00")
    statement = pp.close_quarter(ADMIN, "2026-Q1", today=date(2026, 4, 1))[0]
    assert [s["id"] for s in pp.statements(OWNER_P1)] == [statement["id"]]
    assert pp.statements({"email": "x@example.test", "role": "partner_owner", "partner_id": "partner-2"}) == []
    with pytest.raises(LookupError):
        pp.statement({"email": "x@example.test", "role": "partner_owner", "partner_id": "partner-2"}, statement["id"])


def _verified_bank(db_path, payee):
    _sql(db_path, "INSERT INTO partner_payout_accounts(payee_key,provider,provider_account_id,status,bank_name,last4,payouts_enabled,"
                  "details_submitted,created_at,created_by,updated_at,updated_by) VALUES(?,'stripe_connect','acct_test','verified',"
                  "'Test Bank','6789',1,1,'2026-01-01','t','2026-01-01','t')", (payee,))


@pytest.mark.parametrize("quarter, start, end", [
    ("2026-Q1", date(2026, 1, 1), date(2026, 4, 1)), ("2026-Q4", date(2026, 10, 1), date(2027, 1, 1)),
])
def test_payout_periods_are_calendar_quarters(quarter, start, end):
    import partner_payouts as pp
    assert pp.bounds(quarter) == (start, end) and pp.quarter_of(start) == quarter


# ================================================================ schema and pages

def test_the_migration_is_additive_and_idempotent(db_path):
    with override_target(sqlite_path=str(db_path)):
        from db_migrations import apply_migrations
        apply_migrations()
        apply_migrations()
    tables = {r["name"] for r in _sql(db_path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"partner_agreements", "partner_payouts", "partner_payout_items", "commission_ledger"} <= tables
    columns = {r["name"] for r in _sql(db_path, "PRAGMA table_info(commission_ledger)")}
    assert {"agreement_id", "reverses_entry_id", "billing_party", "commission_rate_bps", "collected_cents", "collected_at",
            "service_month"} <= columns


def test_partner_portal_pages_show_the_breakdown_and_terms(portal):
    import partner_agreements as pa
    client, captured = portal
    sales = _cookie("sales@example.test", "salesperson", partner_id="partner-1")
    page = client.get("/partner-agreements", cookies=sales)
    assert page.status_code == 200
    for text in ("AnyAiCam base price", "Your monthly upcharge", "Your activation / setup fee", "Customer pays",
                 "AnyAiCam bills the customer", "We bill the customer", "20% of the AnyAiCam plan", "Real Customer"):
        assert text in page.text, text
    assert "Other Customer" not in page.text
    agreement = _agree(pa, upcharge=500, fee=9900)
    detail = client.get(f"/partner-agreements/{agreement['id']}", cookies=sales)
    assert detail.status_code == 200 and "$19.99/month" in detail.text and "$99.00 one time" in detail.text
    assert "Next payout" in detail.text and 'id="approve"' not in detail.text
    rival = _cookie("rival@example.test", "salesperson", partner_id="partner-2")
    assert client.get(f"/partner-agreements/{agreement['id']}", cookies=rival).status_code == 403
    assert client.get("/partner-agreements", cookies=CUSTOMER).status_code == 403
    assert client.get("/partner-payouts", cookies=sales).status_code == 200


def test_partner_proposal_and_admin_approval_through_the_api(portal):
    client, captured = portal
    sales = _cookie("sales@example.test", "salesperson", partner_id="partner-1")
    admin = _cookie("admin@example.test", "administrator")
    csrf = {"anyaicam_csrf": "t"}
    headers = {"X-CSRF-Token": "t"}
    body = {"customer_id": "cust-1", "plan_type": "local", "tier_label": "1-8", "upcharge_cents": 500,
            "activation_fee_cents": 9900, "billing_party": "ANYAICAM_BILLED", "base_price_cents": 1}
    proposed = client.post("/api/partner/agreements", json=body, cookies={**sales, **csrf}, headers=headers)
    assert proposed.status_code == 200, proposed.text
    assert proposed.json()["base_price_cents"] == 1499 and proposed.json()["status"] == "pending_approval"
    agreement_id = proposed.json()["id"]
    refused = client.post(f"/api/admin/partner-agreements/{agreement_id}/approve", cookies={**sales, **csrf}, headers=headers)
    assert refused.status_code == 403
    approved = client.post(f"/api/admin/partner-agreements/{agreement_id}/approve", cookies={**admin, **csrf}, headers=headers)
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    detail = client.get(f"/api/partner/agreements/{agreement_id}", cookies=admin).json()
    assert detail["financials"]["next_payout"]["quarter"] == pp_quarter_today()


def pp_quarter_today():
    import partner_payouts as pp
    return pp.quarter_of(date.today())


# ================================================================ owner decisions, 2026-10-04 (second round)

def test_stripe_fees_on_partner_money_are_deducted_and_auditable(pa):
    agreement = _agree(pa, upcharge=500, fee=9900)
    _sync(_invoice("evt_1", "in_1", [("base", 1499), ("upcharge", 500), ("fee", 9900)], agreement_id=agreement["id"]))
    fee_row = _ledger("partner_processing_fee")[0]
    # Stripe fee 30 + 2.9% of $118.99 = $3.75; partner share 104.00/118.99 of it, rounded half up.
    assert stripe_fee(11899) == 375 and fee_row["amount_cents"] == -partner_fee(10400, 11899) == -328
    assert fee_row["basis_cents"] == 10400 and fee_row["collected_cents"] == 11899 and fee_row["agreement_id"] == agreement["id"]
    assert "Stripe fee 375 cents on charge ch_in_1 of 11899 cents" in fee_row["note"] and "10400/11899 = 328" in fee_row["note"]
    import sales_commissions
    assert sales_commissions.settle_processing_fee("in_1") is None  # written once
    # The AnyAiCam residual is never reduced by the partner's fee share.
    assert _ledger("recurring")[0]["amount_cents"] == 300


def test_an_invoice_without_partner_money_carries_no_fee_row(pa):
    agreement = _agree(pa, upcharge=0, fee=0)
    _sync(_invoice("evt_1", "in_1", [("base", 1499)], agreement_id=agreement["id"]))
    assert _ledger("partner_processing_fee") == []


def test_an_unknown_fee_holds_the_invoice_until_stripe_reports_it(pa, db_path, stripe_fees):
    import partner_payouts as pp
    stripe_fees["available"] = False
    agreement = _agree(pa, upcharge=500, fee=0)
    _sync(_invoice("evt_1", "in_1", [("base", 1499), ("upcharge", 500)], agreement_id=agreement["id"]))
    assert _ledger("partner_processing_fee") == []
    _date(db_path, "2026-02-10T10:00:00")
    assert pp.close_quarter(ADMIN, "2026-Q1", today=date(2026, 4, 1)) == []  # held: never paid gross of the fee
    held = _sql(db_path, "SELECT details_json FROM audit_logs WHERE action='partner_payout.held_unsettled_fees'")
    assert held and "in_1" in held[0]["details_json"]
    stripe_fees["available"] = True
    q2 = pp.close_quarter(ADMIN, "2026-Q2", today=date(2026, 7, 1))[0]
    assert q2["period_total_cents"] == 300 + 500 - partner_fee(500, 1999)


def test_friends_family_is_never_combined_with_partner_pricing(pa, db_path):
    import friends_family
    request, _ = friends_family.create_request(customer_id="cust-1", email="owner@example.test")
    friends_family.decide(request["id"], approve=True, decided_by="admin@example.test")
    for kwargs in ({"upcharge": 500, "fee": 0}, {"upcharge": 0, "fee": 9900}, {"upcharge": 0, "fee": 0, "billing": pc.BILLING_PARTNER}):
        with pytest.raises(pa.AgreementError, match="Friends & Family"):
            _agree(pa, **kwargs)
    assert _agree(pa, upcharge=0, fee=0)["status"] == "approved"  # attribution only: no partner pricing


def test_friends_family_cannot_be_approved_for_a_partner_priced_customer(pa, db_path):
    import friends_family
    _agree(pa, identity=SALES, seller=None, upcharge=500, fee=0)  # a pending proposal is enough
    request, _ = friends_family.create_request(customer_id="cust-1", email="owner@example.test")
    with pytest.raises(ValueError, match="cannot be combined"):
        friends_family.decide(request["id"], approve=True, decided_by="admin@example.test")
    assert friends_family.decide(request["id"], approve=False, decided_by="admin@example.test")["status"] != "approved"


HARDWARE_ORDER = ("INSERT INTO hardware_orders(id,customer_id,sku,product_name,stripe_price_id,stripe_checkout_session_id,quantity,amount_cents,"
                  "currency,status,fulfillment_status,created_at,updated_at,stripe_payment_intent_id) VALUES('hw-1','cust-1',"
                  "'AIC-APPLIANCE-RYZEN-STARTER','AnyAiCam Starter','price_hw','cs_hw',1,124999,'usd','paid','paid','2026-01-01','2026-01-01','pi_hw')")


def test_hardware_earns_no_commission_and_no_residual(pa, db_path):
    _sql(db_path, HARDWARE_ORDER)
    result = _sync({"id": "evt_hw", "type": "checkout.session.completed",
                    "data": {"object": {"id": "cs_hw", "mode": "payment", "payment_status": "paid"}}})
    assert result["status"] == "ignored" and _ledger() == []


def test_a_historical_hardware_commission_is_kept_and_still_reversed_by_appending(pa, db_path):
    _sql(db_path, HARDWARE_ORDER)
    _sql(db_path, "INSERT INTO commission_ledger(id,kind,source_ref,salesperson_user_id,partner_id,customer_id,hardware_order_id,basis_cents,"
                  "original_amount_cents,amount_cents,status,earned_at) VALUES('old-hw','hardware','hardware:hw-1','sales-1','partner-1','cust-1',"
                  "'hw-1',124999,5000,5000,'earned','2026-09-30T10:00:00')")
    _sync({"id": "evt_r", "type": "charge.refunded", "data": {"object": {"object": "charge", "id": "ch_hw", "payment_intent": "pi_hw",
                                                                      "amount": 124999, "amount_refunded": 124999}}})
    original = [e for e in _ledger("hardware") if e["id"] == "old-hw"][0]
    assert original["amount_cents"] == 5000 and original["status"] == "reversed"  # history kept, never rewritten
    assert [e["amount_cents"] for e in _ledger("hardware") if e["reverses_entry_id"] == "old-hw"] == [-5000]


def test_payout_recipient_is_the_partner_company_or_the_independent_salesperson(pa, db_path):
    import partner_banking as pb
    import partner_payouts as pp
    indie = {"email": "indie@example.test", "role": "salesperson", "partner_id": None}
    _sql(db_path, "INSERT INTO partner_users(id,email,name,role,password_hash,approved,created_at) "
                  "VALUES('indie-1','indie@example.test','Indy','salesperson','x',1,'2026-01-01')")
    _sql(db_path, "INSERT INTO customers(id,partner_id,name,email,status,created_at,created_by) "
                  "VALUES('cust-3','anyaicam-primary','Indie Customer','c3@example.test','active','2026-01-01','indie@example.test')")
    assert pb.payee_of(OWNER_P1)["payee_key"] == "partner-1"
    assert pb.payee_of(indie)["payee_key"] == "user:indie-1"
    with pytest.raises(pa.AgreementForbidden, match="partner company"):
        pb.payee_of(SALES)
    agreement = _agree(pa, customer="cust-3", seller="indie-1", upcharge=0, fee=0, billing=pc.BILLING_PARTNER)
    pa.record_collection(indie, agreement["id"], service_month="2026-01")
    pa.record_remittance(ADMIN, agreement["id"], service_month="2026-01", amount_cents=1499, reference="wire-i1")
    _date(db_path, "2026-02-10T10:00:00")
    statements = pp.close_quarter(ADMIN, "2026-Q1", today=date(2026, 4, 1))
    assert [(s["payee_key"], s["partner_id"], s["salesperson_user_id"], s["payable_cents"]) for s in statements] == \
        [("user:indie-1", None, "indie-1", 300)]


def test_partner_billed_service_is_activated_only_after_anyaicam_receives_the_base_payment(pa, db_path):
    from customer_entitlements import get_entitlements_for_customer, total_camera_slots
    agreement = _agree(pa, upcharge=500, fee=0, billing=pc.BILLING_PARTNER)
    with pytest.raises(pa.AgreementError, match="base payment received"):
        pa.activate_service(ADMIN, agreement["id"])
    pa.record_collection(SALES, agreement["id"], service_month="2026-01")  # the partner says it collected
    with pytest.raises(pa.AgreementError, match="base payment received"):
        pa.activate_service(ADMIN, agreement["id"])
    assert total_camera_slots("cust-1") == 0
    pa.record_remittance(ADMIN, agreement["id"], service_month="2026-01", amount_cents=1499, reference="wire-1")
    with pytest.raises(pa.AgreementForbidden):
        pa.activate_service(OWNER_P1, agreement["id"])
    activated = pa.activate_service(ADMIN, agreement["id"])
    assert activated["service_activated_at"] and activated["status"] == "active"
    plan = [e for e in get_entitlements_for_customer("cust-1") if e["product"] == "camera_slots_local"][0]
    assert plan["status"] == "active" and plan["camera_slot_quantity"] == 8 and not plan.get("stripe_subscription_id")
    assert total_camera_slots("cust-1") == 8
    suspended = pa.suspend_service(ADMIN, agreement["id"], "partner stopped paying")
    assert suspended["service_suspended_at"]
    assert [e for e in get_entitlements_for_customer("cust-1") if e["product"] == "camera_slots_local"][0]["status"] == "suspended"
    assert pa.activate_service(ADMIN, agreement["id"])["service_suspended_at"] is None
    actions = [r["action"] for r in _sql(db_path, "SELECT action FROM audit_logs WHERE entity_id=? ORDER BY id", (agreement["id"],))]
    assert actions.count("partner_agreement.service_activated") == 2 and "partner_agreement.service_suspended" in actions


def test_a_bounced_only_remittance_does_not_allow_activation(pa):
    agreement = _agree(pa, upcharge=0, fee=0, billing=pc.BILLING_PARTNER)
    pa.record_collection(SALES, agreement["id"], service_month="2026-01")
    remitted = pa.record_remittance(ADMIN, agreement["id"], service_month="2026-01", amount_cents=1499, reference="wire-1")
    pa.reverse_partner_entry(ADMIN, remitted["remittance"]["id"], reason="bounced")
    with pytest.raises(pa.AgreementError, match="base payment received"):
        pa.activate_service(ADMIN, agreement["id"])


def test_an_existing_subscription_is_never_repriced_by_a_new_agreement(pa, db_path):
    _sync(_invoice("evt_1", "in_1", [("base", 1499)], sub="sub_old"))  # subscribed before any agreement
    agreement = _agree(pa, upcharge=500, fee=9900)
    _sync(_invoice("evt_2", "in_2", [("base", 1499)], sub="sub_old"))
    assert _ledger("partner_upcharge") == [] and _ledger("partner_activation_fee") == []
    stored = pa.in_force("cust-1")
    assert stored["id"] == agreement["id"] and stored["status"] == "approved" and not stored["stripe_subscription_id"]


# ================================================================ banking and the payout dashboard

@pytest.fixture()
def provider(monkeypatch):
    """A fake payout provider (Stripe Connect): account creation, onboarding
    links and account reads."""
    import main
    calls = {"posts": [], "account": {"id": "acct_123", "details_submitted": False, "payouts_enabled": False,
                                      "requirements": {"currently_due": ["external_account"]}, "external_accounts": {"data": []}}}

    def post(path, fields, idempotency_key=None):
        calls["posts"].append((path, dict(fields), idempotency_key))
        if path == "/v1/accounts":
            return {"id": "acct_123"}
        if path == "/v1/account_links":
            return {"url": "https://connect.stripe.test/setup/abc"}
        raise AssertionError(path)

    get_before = main.stripe_api_get

    def get(path):
        if path.startswith("/v1/accounts/acct_123"):
            return calls["account"]
        return get_before(path)

    monkeypatch.setattr(main, "stripe_api_post", post)
    monkeypatch.setattr(main, "stripe_api_get", get)
    return calls


def test_bank_onboarding_is_provider_hosted_and_stores_no_bank_numbers(pa, db_path, provider):
    import partner_banking as pb
    url = pb.start_onboarding(OWNER_P1, return_url="https://app.example.test/r", refresh_url="https://app.example.test/x")
    assert url == "https://connect.stripe.test/setup/abc"
    assert pb.start_onboarding(OWNER_P1, return_url="https://app.example.test/r", refresh_url="https://app.example.test/x")
    assert [c[0] for c in provider["posts"]].count("/v1/accounts") == 1  # one provider account per payee
    created = provider["posts"][0]
    assert created[1]["type"] == "express" and created[2] == "anyaicam-payout-account-partner-1"
    assert pb.account("partner-1")["status"] == "onboarding"
    # The provider's account read includes sensitive bank fields; only safe ones are kept.
    provider["account"] = {"id": "acct_123", "details_submitted": True, "payouts_enabled": True, "requirements": {"currently_due": []},
                           "external_accounts": {"data": [{"object": "bank_account", "bank_name": "First Test Bank", "last4": "6789",
                                                           "routing_number": "110000000", "account_number": "000123456789",
                                                           "currency": "usd", "default_for_currency": True}]}}
    view = pb.refresh(OWNER_P1)
    assert (view["status"], view["bank_name"], view["last4"], view["currency"]) == ("verified", "First Test Bank", "6789", "usd")
    assert "routing_number" not in view and "provider_account_id" not in view
    dump = str(_sql(db_path, "SELECT * FROM partner_payout_accounts")) + str(_sql(db_path, "SELECT details_json FROM audit_logs"))
    assert "110000000" not in dump and "000123456789" not in dump and "123456789" not in dump
    assert pb.is_verified("partner-1")
    actions = [r["action"] for r in _sql(db_path, "SELECT action FROM audit_logs WHERE entity_type='partner_payout_account' ORDER BY id")]
    assert actions[0] == "payout_account.created" and "payout_account.status_changed" in actions


@pytest.mark.parametrize("account, status", [
    ({"details_submitted": False, "payouts_enabled": False}, "onboarding"),
    ({"details_submitted": True, "payouts_enabled": False}, "verification_pending"),
    ({"details_submitted": True, "payouts_enabled": False, "requirements": {"disabled_reason": "requirements.past_due"}}, "restricted"),
    ({"details_submitted": True, "payouts_enabled": True, "external_accounts": {"data": [{"object": "bank_account", "last4": "1"}]}}, "verified"),
    ({"details_submitted": True, "payouts_enabled": True}, "verification_pending"),  # no bank account yet
])
def test_payout_account_status_model(account, status):
    import partner_banking as pb
    assert pb._derive(account)["status"] == status


def test_only_the_payee_manages_its_bank_account(pa, provider):
    import partner_banking as pb
    with pytest.raises(pa.AgreementForbidden):
        pb.start_onboarding(SALES, return_url="https://a/r", refresh_url="https://a/x")
    with pytest.raises(pa.AgreementForbidden):
        pb.start_onboarding(ADMIN, return_url="https://a/r", refresh_url="https://a/x")
    assert provider["posts"] == []


def test_provider_outage_is_reported_without_saving_anything(pa, db_path, monkeypatch):
    import main
    import partner_banking as pb

    def down(path, fields, idempotency_key=None):
        raise RuntimeError("provider down")

    monkeypatch.setattr(main, "stripe_api_post", down)
    with pytest.raises(pb.BankingError) as raised:
        pb.start_onboarding(OWNER_P1, return_url="https://a/r", refresh_url="https://a/x")
    assert raised.value.status == 503 and _sql(db_path, "SELECT * FROM partner_payout_accounts") == []


def test_the_payout_dashboard_splits_residual_markup_and_adjustments(pa, db_path):
    import partner_payouts as pp
    agreement = _agree(pa, upcharge=500, fee=9900)
    _sync(_invoice("evt_1", "in_1", [("base", 1499), ("upcharge", 500), ("fee", 9900)], agreement_id=agreement["id"]))
    _sync({"id": "evt_r", "type": "charge.refunded", "data": {"object": {"object": "charge", "id": "ch_in_1", "payment_intent": "pi_in_1",
                                                                         "invoice": "in_1", "amount": 11899, "amount_refunded": 5950}}})
    d = pp.dashboard("partner-1", "sales-1", today=date(2026, 10, 4))
    assert d["residual_cents"] == 300 and d["markup_cents"] == 500 + 9900 - partner_fee(10400, 11899)
    assert d["adjustments_cents"] == -150 - 250 - 4950  # the half refund
    assert d["accrued_cents"] == d["residual_cents"] + d["markup_cents"] + d["adjustments_cents"]
    assert (d["quarter"], d["statement_after"], d["carried_forward_cents"]) == ("2026-Q4", "2027-01-01", 0)
    assert d["estimated_next_payout_cents"] == d["accrued_cents"]


def test_statements_download_as_csv_with_every_line_and_no_formulas(portal, db_path):
    import partner_agreements as pa
    import partner_payouts as pp
    _sql(db_path, "UPDATE customers SET name='=HYPERLINK(\"x\")' WHERE id='cust-1'")
    agreement = _agree(pa, upcharge=500, fee=0)
    _sync(_invoice("evt_1", "in_1", [("base", 1499), ("upcharge", 500)], agreement_id=agreement["id"]))
    _date(db_path, "2026-02-10T10:00:00")
    statement = pp.close_quarter(ADMIN, "2026-Q1", today=date(2026, 4, 1))[0]
    client, _ = portal
    owner = _cookie("boss@example.test", "partner_owner", partner_id="partner-1")
    response = client.get(f"/partner-payouts/{statement['id']}/statement.csv", cookies=owner)
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="anyaicam-payout-2026-Q1-partner-1.csv"'
    body = response.text
    assert "Payable" in body and "20% residual" in body and "Partner monthly upcharge" in body and "Stripe processing fee" in body
    assert "'=HYPERLINK" in body and "\n=HYPERLINK" not in body and ",=HYPERLINK" not in body
    rival = _cookie("x@example.test", "partner_owner", partner_id="partner-2")
    assert client.get(f"/partner-payouts/{statement['id']}/statement.csv", cookies=rival).status_code == 404


def test_the_payouts_page_shows_earnings_banking_and_history(portal, db_path):
    import partner_agreements as pa
    client, _ = portal
    agreement = _agree(pa, upcharge=500, fee=0)
    _sync(_invoice("evt_1", "in_1", [("base", 1499), ("upcharge", 500)], agreement_id=agreement["id"]))
    owner = _cookie("boss@example.test", "partner_owner", partner_id="partner-1")
    page = client.get("/partner-payouts", cookies=owner)
    assert page.status_code == 200
    for text in ("Earnings not yet paid", "20% residual earned", "Partner markup owed", "Adjustments", "Carried from the last statement",
                 "Estimated next payout", "Current quarter", "Payouts / Banking", "Not set up", "Set up bank account",
                 "never sees or stores your routing or account number", "Payout history"):
        assert text in page.text, text
    _verified_bank(db_path, "partner-1")
    page = client.get("/partner-payouts", cookies=owner)
    assert "Verified" in page.text and "Test Bank ending 6789" in page.text and "Update bank account" in page.text
    sales = client.get("/partner-payouts", cookies=_cookie("sales@example.test", "salesperson", partner_id="partner-1"))
    assert "go to your partner company" in sales.text and "Set up bank account" not in sales.text
