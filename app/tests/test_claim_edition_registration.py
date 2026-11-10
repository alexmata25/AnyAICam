"""Owner 2026-11-10: a claimed appliance's edition comes from verified paid entitlements, never from the claim itself.

Real migrated SQLite (initialize_database), no mocks of the code under test. Covers what records Full VMS (a
Stripe-backed camera plan plus a paid licence), what never does (the claim alone, unpaid or manually granted rows,
an unrecorded payment, a refunded order, any Gateway involvement), and that the claim and poll paths call it.
"""
import json
from pathlib import Path

import pytest

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_claim_edition_registration_import.db"):
    import claim_edition_registration as registration
    from partner_db import connection, initialize_database

NOW = "2026-11-10T00:00:00"
APP = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db(tmp_path):
    with override_target(sqlite_path=str(tmp_path / "claim-edition.db")):
        initialize_database()
        with connection() as con:
            con.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p-1','Partner','approved','real',?)", (NOW,))
            con.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-1','p-1','C','c@example.test','active','real',?)", (NOW,))
            con.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Site',?)", (NOW,))
            con.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at,activated_via) VALUES('appl-1','cust-1','site-1','AIC-CLAIM-1',?,'claim')", (NOW,))
        yield


def _entitlement(product, quantity, *, stripe=True, status="active"):
    with connection() as con:
        con.execute("INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,stripe_subscription_id,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)", (f"ent-{product}", "cust-1", product, quantity, status, "sub_test_1" if stripe else None, NOW, NOW))


def _appliance_order(*, intent="pi_test_1", status="paid"):
    with connection() as con:
        con.execute("INSERT INTO hardware_orders(id,customer_id,sku,product_name,stripe_price_id,stripe_payment_intent_id,quantity,amount_cents,currency,status,created_at,updated_at) "
                    "VALUES('order-1','cust-1','AIC-APPLIANCE-MINI','Appliance','price_test',?,1,10000,'usd',?,?,?)", (intent, status, NOW, NOW))


def _marker():
    with connection() as con:
        return con.execute("SELECT gateway_edition FROM appliances WHERE id='appl-1'").fetchone()["gateway_edition"]


def _audits():
    with connection() as con:
        return [dict(item) for item in con.execute("SELECT * FROM audit_logs WHERE action='appliance.edition_designated' AND entity_id='appl-1'")]


def test_a_claim_alone_records_no_edition(db):
    assert registration.register_claimed_appliance_edition("appl-1") == "unpaid"
    assert _marker() is None and _audits() == []


def test_a_paid_plan_and_a_paid_appliance_order_record_full_vms_with_an_audit(db):
    _entitlement("camera_slots_local", 4)
    _appliance_order()
    assert registration.register_claimed_appliance_edition("appl-1") == "recorded"
    assert _marker() == "full_vms"
    (audit,) = _audits()
    details = json.loads(audit["details_json"])
    assert audit["actor_email"] == "system:claim-edition-registration"
    assert details["basis"] == "paid_entitlement" and details["edition"] == "full_vms"
    assert details["plan"] == ["ent-camera_slots_local"] and details["licence"] == ["hardware_order:order-1"]
    # Idempotent: a second call changes nothing and writes no second audit row.
    assert registration.register_claimed_appliance_edition("appl-1") == "already_recorded"
    assert len(_audits()) == 1


def test_a_paid_plan_and_a_paid_software_licence_record_full_vms(db):
    _entitlement("camera_slots_hybrid", 2)
    _entitlement("vms_license", 8)
    assert registration.register_claimed_appliance_edition("appl-1") == "recorded"
    assert _marker() == "full_vms"


def test_rows_without_a_stripe_payment_reference_are_not_paid_evidence(db):
    _entitlement("camera_slots_local", 4, stripe=False)
    _entitlement("vms_license", 8, stripe=False)
    assert registration.register_claimed_appliance_edition("appl-1") == "unpaid"
    assert _marker() is None


def test_an_inactive_plan_is_not_evidence(db):
    _entitlement("camera_slots_local", 4, status="cancelled")
    _appliance_order()
    assert registration.register_claimed_appliance_edition("appl-1") == "unpaid"


def test_a_plan_without_a_licence_is_not_enough(db):
    _entitlement("camera_slots_local", 4)
    assert registration.register_claimed_appliance_edition("appl-1") == "unpaid"


@pytest.mark.parametrize("intent,status", [("", "paid"), ("pi_test_1", "refunded"), ("pi_test_1", "disputed")])
def test_an_unrecorded_or_reversed_appliance_payment_is_not_a_licence(db, intent, status):
    _entitlement("camera_slots_local", 4)
    _appliance_order(intent=intent, status=status)
    assert registration.register_claimed_appliance_edition("appl-1") == "unpaid"
    assert _marker() is None


@pytest.mark.parametrize("reason", ["refunded", "disputed"])
def test_a_paid_order_whose_payment_was_reversed_is_not_a_licence(db, reason):
    import billing_status
    _entitlement("camera_slots_local", 4)
    _appliance_order()  # still 'paid' locally; the reversal is recorded against its payment intent
    billing_status.record_reversal("ch_test_1", "pi_test_1", "", reason)
    assert registration.register_claimed_appliance_edition("appl-1") == "unpaid"
    assert _marker() is None


def test_customer_gateway_activity_blocks_automatic_full_vms(db):
    _entitlement("camera_slots_local", 4)
    _appliance_order()
    with connection() as con:
        con.execute("INSERT INTO gateway_checkout_attempts(id,customer_id,appliance_id,claim_id,edition,decision_id,authorization_reference,"
                    "authorization_version,stripe_customer_id,stripe_price_id,quantity,camera_ids_json,status,created_at,updated_at) "
                    "VALUES('gca-1','cust-1','appl-other','claim-other','cloud_gateway','d-1','a-1','1','cus_test','price_test',1,'[]','expired',?,?)",
                    (NOW, NOW))
    assert registration.register_claimed_appliance_edition("appl-1") == "gateway"
    assert _marker() is None and _audits() == []


def test_an_already_recorded_gateway_edition_is_never_changed(db):
    _entitlement("camera_slots_local", 4)
    _appliance_order()
    with connection() as con:
        con.execute("UPDATE appliances SET gateway_edition='local_gateway' WHERE id='appl-1'")
    assert registration.register_claimed_appliance_edition("appl-1") == "already_recorded"
    assert _marker() == "local_gateway"


def test_only_claim_activated_appliances_are_registered_here(db):
    _entitlement("camera_slots_local", 4)
    _appliance_order()
    with connection() as con:
        con.execute("UPDATE appliances SET activated_via='token' WHERE id='appl-1'")
    assert registration.register_claimed_appliance_edition("appl-1") == "not_claim"
    assert _marker() is None


def test_claim_completion_records_no_edition_itself_and_defers_to_paid_entitlements():
    source = (APP / "appliance_claims.py").read_text(encoding="utf-8")
    start = source.index("    def claim_complete(")
    complete = source[start:source.index("\n    @app.", start)]
    assert "gateway_edition" not in complete
    assert "INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at,activated_via)" in complete
    assert complete.index("claim_edition_registration.register_claimed_appliance_edition(appliance_id)") > \
        complete.index("'appliance_claim.completed'")


def test_the_configuration_poll_retries_registration_for_an_unrecorded_claimed_appliance():
    source = (APP / "appliance_cloud.py").read_text(encoding="utf-8")
    start = source.index("    def appliance_configuration(request: Request)")
    head = source[start:start + 800]
    assert "appliance.get('activated_via')=='claim'" in head and "appliance['gateway_edition'] is None" in head
    assert "claim_edition_registration.register_claimed_appliance_edition(appliance['id'])" in head
