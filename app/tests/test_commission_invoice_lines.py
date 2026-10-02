"""Multi-line invoices earn the same commission whatever the line order
(2026-10-02, Codex finding).

sales_commissions classified an invoice from lines[0] alone while the
commission basis was the whole invoice: a base-plan + add-on invoice was a
base invoice (activation, paid-month counted, 20% of everything) when the
plan came first and an add-on invoice (no activation, month not counted)
when the add-on came first; an unknown price line inflated the basis. Now
classification is per line and order-independent, and only known AnyAiCam
plan/add-on lines count toward the basis. Percentages and timing rules are
unchanged.
"""
import pytest

from test_pricing_ff_commission import _entries, _stripe_maps, _seed, db_path, ledger  # noqa: F401 -- fixtures and helpers


def _invoice(event_id, invoice_id, lines, *, amount=None, sub="sub_base"):
    return {"id": event_id, "type": "invoice.paid", "data": {"object": {
        "id": invoice_id, "object": "invoice", "customer": "cus_1", "subscription": sub,
        "amount_paid": amount if amount is not None else sum(cents for _, cents in lines),
        "tax": 0, "currency": "usd", "charge": f"ch_{invoice_id}", "payment_intent": f"pi_{invoice_id}",
        "subscription_details": {"metadata": {"anyaicam_customer_id": "cust-1"}},
        "lines": {"data": [{"price": {"id": price}, "amount": cents} for price, cents in lines]}}}}


BASE, ADDON, UNKNOWN = ("price_local_8", 1499), ("price_adv", 1499), ("price_somebody_elses", 5000)


def _outcome(sc):
    return sorted((e["kind"], e["amount_cents"], e["basis_cents"], e["camera_slot_tier"]) for e in _entries(sc))


@pytest.mark.parametrize("order", [[BASE, ADDON], [ADDON, BASE]])
def test_base_and_add_on_lines_earn_the_same_whatever_their_order(ledger, order):
    sc = ledger
    sc.sync_commissions_from_stripe_event(_invoice("evt_1", "in_1", order))
    assert _outcome(sc) == [("activation", 4000, 0, 8), ("recurring", 600, 2998, 8)]  # $40 activation + 20% of $29.98


def test_the_two_orders_produce_identical_ledgers(db_path, monkeypatch, tmp_path):
    results = []
    for order in ([BASE, ADDON], [ADDON, BASE]):
        from database_backend import override_target
        from partner_db import initialize_database
        path = tmp_path / f"order-{len(results)}.db"
        with override_target(sqlite_path=str(path)):
            initialize_database()
            _seed(path)
            _stripe_maps(monkeypatch, ANYAICAM_STRIPE_PRICE_LOCAL_1_8="price_local_8", ANYAICAM_STRIPE_PRICE_ADVANCED_ANALYTICS="price_adv")
            import sales_commissions as sc
            sc.sync_commissions_from_stripe_event(_invoice("evt_1", "in_1", order))
            sc.sync_commissions_from_stripe_event(_invoice("evt_2", "in_2", list(reversed(order))))
            results.append(_outcome(sc))
    assert results[0] == results[1]


def test_an_unknown_price_line_earns_nothing(ledger):
    sc = ledger
    sc.sync_commissions_from_stripe_event(_invoice("evt_1", "in_1", [UNKNOWN, BASE]))
    recurring = [e for e in _entries(sc) if e["kind"] == "recurring"]
    assert len(recurring) == 1 and recurring[0]["basis_cents"] == 1499 and recurring[0]["amount_cents"] == 300


def test_an_invoice_with_only_unknown_lines_is_ignored(ledger):
    sc = ledger
    result = sc.sync_commissions_from_stripe_event(_invoice("evt_1", "in_1", [UNKNOWN, ("price_other", 100)]))
    assert _entries(sc) == []
    assert "ignored" in str(result)


def test_mixed_eligible_and_ineligible_lines_count_only_the_eligible_share(ledger):
    sc = ledger
    # $14.99 plan + $14.99 add-on + $50.00 unknown, paid in full: basis is the eligible $29.98
    sc.sync_commissions_from_stripe_event(_invoice("evt_1", "in_1", [ADDON, UNKNOWN, BASE]))
    recurring = [e for e in _entries(sc) if e["kind"] == "recurring"][0]
    assert recurring["basis_cents"] == 2998 and recurring["amount_cents"] == 600


def test_an_add_on_only_invoice_keeps_its_existing_treatment(ledger):
    """Add-on timing before the first base-plan invoice is an owner decision:
    unchanged here (no base month counted, no activation)."""
    sc = ledger
    sc.sync_commissions_from_stripe_event(_invoice("evt_1", "in_1", [ADDON]))
    assert [e for e in _entries(sc) if e["kind"] == "activation"] == []
