"""Provider-free checks of the unwired Local Gateway billing boundary."""
import unittest
from contextlib import nullcontext
import ast
import sqlite3
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path
from copy import deepcopy

from app.local_gateway_remote_billing import (evaluate_current_subscription,
                                              reconcile_bound_subscription,
                                              bind_verified_paid_checkout, checkout_fields, register_routes,
                                              remote_access_for_appliance, sync_from_verified_webhook,
                                              _verified_unreversed_invoice, issue_remote_lease)


class LocalGatewayRemoteBillingTests(unittest.TestCase):
    def setUp(self):
        synthetic_handoff = patch('app.local_gateway_remote_billing.CHECKOUT_AUTHORIZATION_ATOMICALLY_ENFORCED', True)
        synthetic_handoff.start(); self.addCleanup(synthetic_handoff.stop)
        readiness = patch.dict(sys.modules, {'appliance_readiness':SimpleNamespace(appliance_activated=lambda appliance: True)})
        readiness.start(); self.addCleanup(readiness.stop)
        verified = patch.dict(sys.modules, {'gateway_internal_provisioning':SimpleNamespace(edition_authorization_effective=lambda *args:True)})
        verified.start(); self.addCleanup(verified.stop)
        self.context = dict(customer_id="customer-1", appliance_id="appliance-1",
                            stripe_customer_id="cus_test_1", price_id="price_test_1",
                            payment_verified=True)
        self.subscription = {
            "id": "sub_test_1", "customer": "cus_test_1", "status": "active",
            "metadata": {"anyaicam_customer_id": "customer-1",
                         "anyaicam_appliance_id": "appliance-1",
                         "anyaicam_product": "local_gateway_remote"},
            "items": {"data": [{"price": {"id": "price_test_1"}, "quantity": 1}]},
        }

    def evaluate(self, subscription=None, **overrides):
        context = {**self.context, **overrides}
        return evaluate_current_subscription(
            self.subscription if subscription is None else subscription, **context)

    def test_additive_migration_starts_empty_and_prevents_duplicate_binding(self):
        source = Path(__file__).resolve().parents[1] / "db_migrations.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        migration_list = next(node.value for node in tree.body
                              if isinstance(node, ast.Assign)
                              and any(isinstance(target, ast.Name) and target.id == "MIGRATIONS"
                                      for target in node.targets))
        migration = next(node for node in migration_list.elts
                         if isinstance(node, ast.Tuple) and isinstance(node.elts[0], ast.Constant)
                         and node.elts[0].value == "20261103_local_gateway_remote_subscriptions")
        sql = ast.literal_eval(migration.elts[1])
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript(sql)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM local_gateway_remote_subscriptions").fetchone()[0], 0)
        row = ("appliance-1", "customer-1", "cus-1", "sub-1", "item-1",
               "price-1", "active", "active", "now", "now")
        statement = ("INSERT INTO local_gateway_remote_subscriptions "
                     "(appliance_id,customer_id,stripe_customer_id,stripe_subscription_id,"
                     "stripe_subscription_item_id,stripe_price_id,status,stripe_status,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)")
        db.execute(statement, row)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(statement, ("appliance-2", *row[1:]))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(statement, row)

    def test_lifecycle_updates_only_the_exact_current_binding(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("""CREATE TABLE local_gateway_remote_subscriptions(
            appliance_id TEXT, customer_id TEXT, stripe_customer_id TEXT,
            stripe_subscription_id TEXT, stripe_price_id TEXT, status TEXT,
            stripe_status TEXT, current_period_end INTEGER, cancel_at_period_end INTEGER,
            updated_at TEXT)""")
        db.execute("CREATE TABLE appliances(id TEXT, customer_id TEXT, gateway_edition TEXT)")
        db.execute("INSERT INTO appliances VALUES('appliance-1','customer-1','local_gateway')")
        db.execute("INSERT INTO local_gateway_remote_subscriptions VALUES(?,?,?,?,?,?,?,?,?,?)",
                   ("appliance-1", "customer-1", "cus_test_1", "sub_new",
                    "price_test_1", "active", "active", None, 0, "old"))
        old = deepcopy(self.subscription)
        old["id"] = "sub_old"
        old["status"] = "canceled"
        result = reconcile_bound_subscription(db, old, **self.context, verified_at="now")
        self.assertEqual(result.reason, "binding_not_current")
        self.assertEqual(db.execute("SELECT status FROM local_gateway_remote_subscriptions").fetchone()[0], "active")
        current = deepcopy(old)
        current["id"] = "sub_new"
        result = reconcile_bound_subscription(db, current, **self.context, verified_at="now")
        self.assertEqual(result.state, "inactive")
        self.assertEqual(db.execute("SELECT status FROM local_gateway_remote_subscriptions").fetchone()[0], "inactive")
        stale_active = deepcopy(current)
        stale_active["status"] = "active"
        self.assertEqual(reconcile_bound_subscription(db, stale_active, **self.context,
                                                       verified_at="later").reason, "binding_not_current")
        self.assertEqual(db.execute("SELECT status FROM local_gateway_remote_subscriptions").fetchone()[0], "inactive")

    def test_reconciliation_rejects_changed_snapshot_and_preserves_suspension(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE local_gateway_remote_subscriptions (
                appliance_id TEXT, customer_id TEXT, stripe_customer_id TEXT,
                stripe_subscription_id TEXT, stripe_price_id TEXT, status TEXT,
                stripe_status TEXT, current_period_end INTEGER,
                cancel_at_period_end INTEGER, updated_at TEXT);
            CREATE TABLE appliances(id TEXT, customer_id TEXT, gateway_edition TEXT);
            INSERT INTO appliances VALUES('appliance-1','customer-1','local_gateway');
            INSERT INTO local_gateway_remote_subscriptions VALUES
                ('appliance-1','customer-1','cus_test_1','sub_test_1','price_test_1',
                 'suspended','active',NULL,0,'newer');
        """)
        snapshot = {"status": "active", "stripe_status": "active", "updated_at": "older"}
        result = reconcile_bound_subscription(db, self.subscription, **self.context,
                                              verified_at="now", observed_binding=snapshot)
        self.assertEqual(result.reason, "binding_changed")
        snapshot.update(status="suspended", updated_at="newer")
        result = reconcile_bound_subscription(db, self.subscription, **self.context,
                                              verified_at="now", observed_binding=snapshot)
        self.assertEqual(result.reason, "binding_not_current")
        self.assertEqual(db.execute("SELECT status FROM local_gateway_remote_subscriptions").fetchone()[0], "suspended")

    def test_postgres_mapping_conflict_requests_retry_without_database_service(self):
        changed = {"status":"active", "stripe_status":"active", "updated_at":"newer"}
        db = Mock()
        db.execute.side_effect = [SimpleNamespace(rowcount=0), SimpleNamespace(fetchone=lambda:changed)]
        snapshot = {"status":"active", "stripe_status":"active", "updated_at":"older"}
        result = reconcile_bound_subscription(db, self.subscription, **self.context,
                                              verified_at="now", observed_binding=snapshot)
        self.assertEqual(result.reason, "binding_changed")
        update_sql, update_args = db.execute.call_args_list[0].args
        self.assertIn("status=? AND stripe_status=? AND updated_at=?", update_sql)
        self.assertEqual(update_args[-3:], ("active", "active", "older"))

    def test_paid_checkout_binds_only_owned_appliance_and_current_session(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE appliances(id TEXT PRIMARY KEY, customer_id TEXT, gateway_edition TEXT);
            INSERT INTO appliances VALUES('appliance-1','customer-1','local_gateway');
            CREATE TABLE checkout_pending(customer_id TEXT, purchase TEXT, session_id TEXT, price_id TEXT, quantity INTEGER, status TEXT);
            INSERT INTO checkout_pending VALUES('customer-1','local_gateway_remote:appliance-1','cs-new','price_test_1',1,'open');
            CREATE TABLE local_gateway_remote_subscriptions(
                appliance_id TEXT PRIMARY KEY, customer_id TEXT, stripe_customer_id TEXT,
                stripe_subscription_id TEXT UNIQUE, stripe_subscription_item_id TEXT,
                stripe_price_id TEXT, status TEXT, stripe_status TEXT, current_period_end INTEGER,
                cancel_at_period_end INTEGER, payment_failed_at TEXT, suspended_reason TEXT,
                created_at TEXT, updated_at TEXT);
        """)
        sub = deepcopy(self.subscription)
        sub["items"]["data"][0]["id"] = "si_1"
        session = {"id": "cs-new", "mode": "subscription", "payment_status": "paid",
                   "customer": "cus_test_1", "subscription": "sub_test_1"}
        db.execute("UPDATE appliances SET gateway_edition=NULL WHERE id='appliance-1'")
        self.assertEqual(bind_verified_paid_checkout(db, session, sub, **self.context,
                                                    verified_at="now").reason, "appliance_not_owned")
        db.execute("UPDATE appliances SET gateway_edition='cloud_gateway' WHERE id='appliance-1'")
        self.assertEqual(bind_verified_paid_checkout(db, session, sub, **self.context,
                                                    verified_at="now").reason, "appliance_not_owned")
        db.execute("UPDATE appliances SET gateway_edition='local_gateway' WHERE id='appliance-1'")
        with patch('app.local_gateway_remote_billing.CHECKOUT_AUTHORIZATION_ATOMICALLY_ENFORCED',False):
            self.assertEqual(bind_verified_paid_checkout(db, session, sub, **self.context, verified_at="now").reason,
                             "checkout_handoff_unavailable")
            self.assertEqual(db.execute('SELECT COUNT(*) FROM local_gateway_remote_subscriptions').fetchone()[0],0)
        with patch.dict(sys.modules, {'gateway_internal_provisioning':SimpleNamespace(edition_authorization_effective=lambda *args:False)}):
            self.assertEqual(bind_verified_paid_checkout(db, session, sub, **self.context, verified_at="now").reason,
                             "edition_authorization_invalid")
            self.assertEqual(db.execute('SELECT COUNT(*) FROM local_gateway_remote_subscriptions').fetchone()[0],0)
        checker=Mock(return_value=True)
        tagged={**session,'metadata':{'anyaicam_gateway_attempt':'attempt-a'}}
        with patch.dict(sys.modules,{'gateway_checkout_attempts':SimpleNamespace(exact_paid_attempt_authorized=checker,PAID_ACTIVATION_READY=False)}):
            self.assertEqual(bind_verified_paid_checkout(db, tagged, sub, **self.context, verified_at='now').reason,
                             'checkout_attempt_review_required')
            checker.assert_called_once()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM local_gateway_remote_subscriptions').fetchone()[0],0)
        result = bind_verified_paid_checkout(db, session, sub, **self.context, verified_at="now")
        self.assertEqual(result.state, "active")
        self.assertEqual(db.execute("SELECT appliance_id,customer_id,status FROM local_gateway_remote_subscriptions").fetchone(),
                         ("appliance-1", "customer-1", "active"))
        sub["id"] = "sub_test_2"
        session["subscription"] = sub["id"]
        self.assertEqual(bind_verified_paid_checkout(db, session, sub, **self.context,
                                                     verified_at="later").reason,
                         "previous_subscription_not_inactive")
        db.execute("UPDATE checkout_pending SET session_id='cs-other'")
        self.assertEqual(bind_verified_paid_checkout(db, session, sub, **self.context,
                                                     verified_at="later").reason,
                         "checkout_not_current")
        db.execute("UPDATE checkout_pending SET session_id='cs-new'")
        db.execute("UPDATE local_gateway_remote_subscriptions SET status='inactive'")
        with patch('app.local_gateway_remote_billing.CHECKOUT_AUTHORIZATION_ATOMICALLY_ENFORCED',False):
            self.assertEqual(bind_verified_paid_checkout(db, session, sub, **self.context,
                verified_at='later', ended_subscription_id='sub_test_1').reason, 'checkout_handoff_unavailable')
            self.assertEqual(db.execute('SELECT stripe_subscription_id FROM local_gateway_remote_subscriptions').fetchone()[0], 'sub_test_1')
        self.assertEqual(bind_verified_paid_checkout(db, session, sub, **self.context,
                                                     verified_at="later").reason,
                         "previous_subscription_not_verified_ended")
        self.assertEqual(bind_verified_paid_checkout(db, session, sub, **self.context,
                                                     verified_at="later", ended_subscription_id="sub_test_1").state, "active")
        old = deepcopy(sub)
        old["id"] = "sub_test_1"
        old["status"] = "canceled"
        self.assertEqual(reconcile_bound_subscription(db, old, **self.context,
                                                       verified_at="later").reason,
                         "binding_not_current")
        self.assertEqual(db.execute("SELECT stripe_subscription_id,status FROM local_gateway_remote_subscriptions").fetchone(),
                         ("sub_test_2", "active"))

    def test_paid_checkout_accepts_mapping_rows_from_postgres_backend(self):
        class Result:
            rowcount = 1
            def __init__(self, value=None):
                self.value = value
            def fetchone(self):
                return self.value
        class Database:
            def execute(self, sql, params):
                if sql.startswith("SELECT stripe_subscription_id"):
                    return Result({"stripe_subscription_id": "sub_test_1", "status": "active"})
                if sql.startswith("SELECT 1"):
                    return Result({"present": 1})
                return Result()
        sub = deepcopy(self.subscription)
        sub["items"]["data"][0]["id"] = "si_1"
        session = {"id": "cs-new", "mode": "subscription", "payment_status": "paid",
                   "customer": "cus_test_1", "subscription": "sub_test_1"}
        decision = bind_verified_paid_checkout(Database(), session, sub, **self.context, verified_at="now")
        self.assertEqual(decision.state, "active")

    def test_checkout_is_one_unit_and_guard_is_per_appliance(self):
        args = dict(customer_id="customer-1", appliance_id="appliance-1",
                    stripe_customer_id="cus_test_1", price_id="price_test_1",
                    success_url="https://example.test/success",
                    cancel_url="https://example.test/cancel")
        key, fields = checkout_fields(**args)
        self.assertEqual(key, "local_gateway_remote:appliance-1")
        self.assertEqual(dict(fields)["line_items[0][quantity]"], "1")
        self.assertEqual(dict(fields)["subscription_data[metadata][anyaicam_appliance_id]"], "appliance-1")
        other_key, _ = checkout_fields(**{**args, "appliance_id": "appliance-2"})
        self.assertNotEqual(key, other_key)
        with self.assertRaises(ValueError):
            checkout_fields(**{**args, "price_id": ""})

    def test_remote_access_never_uses_another_customer_or_legacy_plan(self):
        db = SimpleNamespace(row=lambda sql, args: {"customer_id": "customer-1", "status": "active", "gateway_edition": "local_gateway"},transaction=lambda:nullcontext(object()))
        gate = SimpleNamespace(edition_authorization_effective=lambda *args:True)
        with patch.dict(sys.modules, {"partner_db": db, "gateway_internal_provisioning":gate}):
            self.assertTrue(remote_access_for_appliance("customer-1", "appliance-1"))
            self.assertFalse(remote_access_for_appliance("customer-2", "appliance-1"))
            db.row = lambda sql, args: {"customer_id": "customer-1", "status": "inactive", "gateway_edition": "local_gateway"}
            self.assertFalse(remote_access_for_appliance("customer-1", "appliance-1"))
            db.row = Mock(side_effect=RuntimeError("database unavailable"))
            self.assertFalse(remote_access_for_appliance("customer-1", "appliance-1"))

    def test_completed_checkout_redelivery_never_reopens_service(self):
        binding = {"customer_id": "customer-1", "stripe_customer_id": "cus_test_1",
                   "stripe_subscription_id": "sub_test_1"}
        def row(sql, args):
            return None if "checkout_pending" in sql else binding
        state = SimpleNamespace(RetryableStripeEventError=RuntimeError)
        partner = SimpleNamespace(row=row, connection=lambda: None)
        event = {"type": "checkout.session.completed", "data": {"object": {
            "id": "cs-old", "mode": "subscription", "payment_status": "paid",
            "customer": "cus_test_1", "subscription": "sub_test_1",
            "metadata": {"anyaicam_product": "local_gateway_remote",
                         "anyaicam_customer_id": "customer-1", "anyaicam_appliance_id": "appliance-1"},
        }}}
        with patch.dict(sys.modules, {"stripe_state": state, "partner_db": partner}):
            self.assertEqual(sync_from_verified_webhook(event)["status"], "already_processed")
            binding["stripe_subscription_id"] = "sub_replacement"
            with self.assertRaises(RuntimeError):
                sync_from_verified_webhook(event)

    def test_verified_webhook_uses_current_subscription_and_guard_binding(self):
        from contextlib import contextmanager
        import app.local_gateway_remote_billing as boundary
        sub = deepcopy(self.subscription)
        sub["latest_invoice"] = "in_test"
        sub["items"]["data"][0]["id"] = "si_test"
        invoice = {"id": "in_test", "status": "paid"}
        state = SimpleNamespace(
            RetryableStripeEventError=RuntimeError,
            current_subscription=Mock(return_value=sub),
            bound_elsewhere=lambda stripe_customer, customer: False,
            _id=lambda value: value if isinstance(value, str) else value["id"],
            stripe_get=Mock(return_value=invoice),
            subscription_payment_reversal=Mock(return_value=None),
        )
        @contextmanager
        def connection():
            yield object()
        partner = SimpleNamespace(row=lambda sql, args: ({"price_id": "price_test_1"}
                                                          if "checkout_pending" in sql else None),
                                  connection=connection, transaction=connection)
        event = {"type": "checkout.session.completed", "data": {"object": {
            "id": "cs-test", "mode": "subscription", "payment_status": "paid",
            "customer": "cus_test_1", "subscription": "sub_test_1",
            "metadata": {"anyaicam_product": "local_gateway_remote",
                         "anyaicam_customer_id": "customer-1", "anyaicam_appliance_id": "appliance-1"},
        }}}
        with patch.dict(sys.modules, {"stripe_state": state, "partner_db": partner}), \
                patch.object(boundary, "_verified_unreversed_invoice", return_value=True) as verify_payment, \
                patch.object(boundary, "bind_verified_paid_checkout",
                             return_value=boundary.RemoteSubscriptionDecision("active", "verified_active")) as bind:
            self.assertEqual(sync_from_verified_webhook(event)["status"], "active")
            state.current_subscription.assert_called_once_with("sub_test_1")
            self.assertTrue(bind.call_args.kwargs["payment_verified"])
            verify_payment.return_value = False
            bind.return_value = boundary.RemoteSubscriptionDecision("denied", "payment_not_verified")
            with self.assertRaises(RuntimeError):
                sync_from_verified_webhook(event)
            self.assertFalse(bind.call_args.kwargs["payment_verified"])
            state.stripe_get.return_value = {"id": "in_test", "status": "open"}
            with self.assertRaises(RuntimeError):
                sync_from_verified_webhook(event)
            self.assertEqual(bind.call_count, 2)

    def test_payment_failure_grace_and_recovery_do_not_change_other_appliance(self):
        from contextlib import contextmanager
        import app.local_gateway_remote_billing as boundary
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        # Minimal test fixture only; the project migration is not executed.
        db.execute("CREATE TABLE local_gateway_remote_subscriptions ("
                   "stripe_subscription_id TEXT, status TEXT, stripe_status TEXT, "
                   "suspended_reason TEXT, payment_failed_at TEXT)")
        db.executemany("INSERT INTO local_gateway_remote_subscriptions VALUES(?,?,?,?,?)",
                       [("sub-a", "active", "active", None, None),
                        ("sub-b", "active", "active", None, None)])
        @contextmanager
        def connection():
            yield db
        with patch.dict(sys.modules, {"partner_db": SimpleNamespace(connection=connection)}), \
                patch.object(boundary, "_table_available", return_value=True):
            boundary.set_payment_failed("sub-a", "first")
            boundary.set_payment_failed("sub-a", "later")
            self.assertEqual(db.execute("SELECT payment_failed_at FROM local_gateway_remote_subscriptions "
                                        "WHERE stripe_subscription_id='sub-a'").fetchone()[0], "first")
            self.assertEqual(boundary.suspend_subscription("sub-a", "payment_failed"), 1)
            self.assertEqual(boundary.restore_subscription("sub-a", ("refunded",)), 0)
            boundary.clear_payment_failed("sub-a")
            self.assertEqual(boundary.restore_subscription("sub-a", ("payment_failed",)), 1)
            self.assertEqual(db.execute("SELECT status,payment_failed_at FROM local_gateway_remote_subscriptions "
                                        "WHERE stripe_subscription_id='sub-b'").fetchone(), ("active", None))

    def test_local_recovery_rolls_back_failure_marker_when_restore_write_fails(self):
        from contextlib import contextmanager
        import app.local_gateway_remote_billing as boundary
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE local_gateway_remote_subscriptions ("
                   "stripe_subscription_id TEXT, status TEXT, stripe_status TEXT, "
                   "suspended_reason TEXT, payment_failed_at TEXT)")
        db.execute("INSERT INTO local_gateway_remote_subscriptions VALUES(?,?,?,?,?)",
                   ("sub-a", "suspended", "active", "payment_failed", "original"))
        db.execute("CREATE TRIGGER reject_restore BEFORE UPDATE OF status ON local_gateway_remote_subscriptions "
                   "BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END")
        db.commit()
        @contextmanager
        def connection():
            with db:
                yield db
        with patch.dict(sys.modules, {"partner_db": SimpleNamespace(connection=connection)}), \
                patch.object(boundary, "_table_available", return_value=True):
            with self.assertRaises(sqlite3.IntegrityError):
                boundary.restore_subscription("sub-a", ("payment_failed",), clear_failure=True)
            self.assertEqual(db.execute("SELECT status,payment_failed_at FROM local_gateway_remote_subscriptions")
                             .fetchone(), ("suspended", "original"))
            db.execute("DROP TRIGGER reject_restore")
            db.commit()
            self.assertEqual(boundary.restore_subscription("sub-a", ("payment_failed",), clear_failure=True), 1)
            self.assertEqual(db.execute("SELECT status,payment_failed_at FROM local_gateway_remote_subscriptions")
                             .fetchone(), ("active", None))

    def test_refund_suspension_requires_explicit_allowed_recovery_reason(self):
        from contextlib import contextmanager
        import app.local_gateway_remote_billing as boundary
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE local_gateway_remote_subscriptions ("
                   "stripe_subscription_id TEXT, status TEXT, stripe_status TEXT, suspended_reason TEXT)")
        db.execute("INSERT INTO local_gateway_remote_subscriptions VALUES('sub-a','active','active',NULL)")
        @contextmanager
        def connection():
            yield db
        with patch.dict(sys.modules, {"partner_db": SimpleNamespace(connection=connection)}), \
                patch.object(boundary, "_table_available", return_value=True):
            self.assertEqual(boundary.suspend_subscription("sub-a", "refunded"), 1)
            self.assertEqual(boundary.restore_subscription("sub-a", ("payment_failed",)), 0)
            self.assertEqual(db.execute("SELECT status FROM local_gateway_remote_subscriptions").fetchone()[0], "suspended")

    def test_two_appliances_have_independent_remote_entitlements(self):
        states = {"appliance-1": "active", "appliance-2": "inactive"}
        db = SimpleNamespace(row=lambda sql, args: {
            "customer_id": "customer-1", "gateway_edition": "local_gateway", "status": states[args[0]]
        } if args[0] in states else None, transaction=lambda:nullcontext(object()))
        gate = SimpleNamespace(edition_authorization_effective=lambda *args:True)
        with patch.dict(sys.modules, {"partner_db": db, "gateway_internal_provisioning":gate}):
            self.assertTrue(remote_access_for_appliance("customer-1", "appliance-1"))
            self.assertFalse(remote_access_for_appliance("customer-1", "appliance-2"))
            states["appliance-1"] = "inactive"
            states["appliance-2"] = "active"
            self.assertFalse(remote_access_for_appliance("customer-1", "appliance-1"))
            self.assertTrue(remote_access_for_appliance("customer-1", "appliance-2"))

    def test_checkout_route_requires_owned_appliance_and_one_time_licence(self):
        class HTTPException(Exception):
            def __init__(self, status_code, detail):
                self.status_code = status_code
                self.detail = detail
        class Request:
            cookies={'anyaicam_csrf':'signed'}
            headers={'x-csrf-token':'signed'}
        class Router:
            def get(self, path):
                return lambda fn: fn
            def post(self, path):
                def register(fn):
                    self.path, self.endpoint = path, fn
                    return fn
                return register
        router = Router()
        captured = []
        fastapi = ModuleType("fastapi")
        fastapi.HTTPException, fastapi.Request = HTTPException, Request
        owner = {"role": "customer_owner", "customer_id": "customer-1", "email": "owner@example.test"}
        partner = SimpleNamespace(partner_identity=lambda request: owner)
        appliance = {"id": "appliance-1", "customer_id": "customer-1",
                     "activation_status": "activated", "state": "offline", "gateway_edition": "local_gateway"}
        def row(sql, args):
            return appliance if "FROM appliances" in sql else None
        entitlements = SimpleNamespace(get_entitlements_for_customer=lambda customer: [{"status": "active"}],
                                       _purchased_vms_license_capacity=lambda rows: 4)
        main = SimpleNamespace(PUBLIC_BASE_URL="https://example.test",
                               require_stripe_price_matches_catalog=Mock())
        guard = SimpleNamespace(canonical_stripe_customer=lambda customer, email: "cus_test_1",
                                create_session=lambda customer, purchase, **kwargs:
                                captured.append((customer, purchase, kwargs)) or {"url": "https://checkout.test"})
        billing = SimpleNamespace(own_pc_licence_reversed=lambda customer: False)
        modules = {"fastapi": fastapi, "partner_portal": partner, "partner_db": SimpleNamespace(row=row),
                   "customer_entitlements": entitlements, "checkout_guard": guard,
                   "per_camera_billing": billing, "main": main}
        modules['gateway_checkout_handoff']=SimpleNamespace(create_session=lambda actor,**kw:guard.create_session(
            kw['customer_id'],kw['purchase'],price_id=kw['price_id'],quantity=1,fields=kw['fields']))
        modules['gateway_checkout_attempts']=SimpleNamespace(ATTEMPTS_SERVICE_READY=True)
        modules['token_security']=SimpleNamespace(unsign=lambda token:'csrf' if token=='signed' else None)
        with patch.dict(sys.modules, modules), patch('app.local_gateway_remote_billing._edition_verified',return_value=True), patch('app.local_gateway_remote_billing.CHECKOUT_AUTHORIZATION_ATOMICALLY_ENFORCED',True), patch.dict("os.environ", {
                "ANYAICAM_STRIPE_PRICE_LOCAL_GATEWAY_REMOTE": "price_test_1",
                "ANYAICAM_LOCAL_GATEWAY_CHECKOUT_ENABLED": "true"}):
            register_routes(router)
            with patch.dict("os.environ", {"ANYAICAM_LOCAL_GATEWAY_CHECKOUT_ENABLED": "false"}):
                with self.assertRaises(HTTPException) as disabled:
                    router.endpoint({"appliance_id": "appliance-1"}, Request())
                self.assertEqual(disabled.exception.status_code, 503)
                self.assertEqual(captured, [])
            bad_request=Request(); bad_request.cookies={}
            with self.assertRaises(HTTPException) as csrf_error:
                router.endpoint({"appliance_id":"appliance-1"},bad_request)
            self.assertEqual(csrf_error.exception.status_code,403)
            self.assertEqual(captured,[])
            self.assertEqual(router.endpoint({"appliance_id": "appliance-1"}, Request())["status"], "complete")
            self.assertEqual(captured[0][1], "local_gateway_remote:appliance-1")
            self.assertEqual(captured[0][2]["quantity"], 1)
            main.require_stripe_price_matches_catalog.assert_called_once_with("price_test_1", 999, "month")
            with patch.object(guard, 'create_session', return_value={'url':'http://unsafe.example.test'}):
                with self.assertRaises(HTTPException) as unsafe:
                    router.endpoint({"appliance_id":"appliance-1"}, Request())
                self.assertEqual(unsafe.exception.status_code, 502)
            owner['role'] = 'customer_viewer'
            with self.assertRaises(HTTPException) as viewer:
                router.endpoint({"appliance_id":"appliance-1"}, Request())
            self.assertEqual(viewer.exception.status_code, 403)
            owner['role'] = 'customer_owner'
            with patch.object(billing, 'own_pc_licence_reversed', return_value=True):
                with self.assertRaises(HTTPException) as reversed_license:
                    router.endpoint({"appliance_id":"appliance-1"}, Request())
                self.assertEqual(reversed_license.exception.status_code, 403)
            self.assertEqual(len(captured), 1)
            appliance["gateway_edition"] = None
            with self.assertRaises(HTTPException) as wrong_edition:
                router.endpoint({"appliance_id": "appliance-1"}, Request())
            self.assertEqual(wrong_edition.exception.status_code, 403)
            appliance["gateway_edition"] = "local_gateway"
            appliance["customer_id"] = "other"
            modules["partner_db"].row = lambda sql, args: None
            with self.assertRaises(HTTPException) as raised:
                router.endpoint({"appliance_id": "appliance-1"}, Request())
            self.assertEqual(raised.exception.status_code, 403)
            self.assertEqual(len(captured), 1)

    def test_remote_lease_payment_check_fails_closed_for_dispute_or_unknown_charge(self):
        state = SimpleNamespace(
            subscription_payment_reversal=lambda sub: None,
            stripe_get=lambda path, what: {"latest_charge": {
                "id": "ch_test", "disputed": False, "refunded": False, "amount": 999, "amount_refunded": 0}},
            _id=lambda item: item.get("id", "") if isinstance(item, dict) else (item or ""),
            _full_refund=lambda charge: bool(charge.get("refunded")),
        )
        sales = SimpleNamespace(_invoice_payment_intent=lambda invoice: "pi_test")
        invoice = {"status": "paid", "amount_paid": 999}
        with patch.dict(sys.modules, {"stripe_state": state, "sales_commissions": sales}):
            self.assertTrue(_verified_unreversed_invoice(invoice, {}))
            for amount in (None, -1, True, "0"):
                self.assertFalse(_verified_unreversed_invoice({"status": "paid", "amount_paid": amount}, {}))
            self.assertFalse(_verified_unreversed_invoice({"status": "paid"}, {}))
            self.assertTrue(_verified_unreversed_invoice({"status": "paid", "amount_paid": 0}, {}))
            state.stripe_get = lambda path, what: {"latest_charge": {"id": "ch_test", "disputed": True}}
            self.assertFalse(_verified_unreversed_invoice(invoice, {}))
            state.stripe_get = lambda path, what: {"latest_charge": {"id": "ch_test"}}
            self.assertFalse(_verified_unreversed_invoice(invoice, {}))
            state.subscription_payment_reversal = lambda sub: "refunded"
            self.assertFalse(_verified_unreversed_invoice(invoice, {}))

    def test_incomplete_refund_evidence_cannot_authorize_remote_access(self):
        charge = {"id": "ch_test", "disputed": False, "refunded": False,
                  "amount": 999, "amount_refunded": 0}
        state = SimpleNamespace(subscription_payment_reversal=lambda sub: None,
                                stripe_get=lambda path, what: {"latest_charge": charge},
                                _id=lambda item: item or "",
                                _full_refund=lambda item: item["refunded"] or item["amount_refunded"] >= item["amount"])
        sales = SimpleNamespace(_invoice_payment_intent=lambda invoice: "pi_test")
        with patch.dict(sys.modules, {"stripe_state": state, "sales_commissions": sales}):
            invoice = {"status": "paid", "amount_paid": 999}
            self.assertTrue(_verified_unreversed_invoice(invoice, {}))
            for key, value in (("refunded", None), ("refunded", "false"),
                               ("amount", None), ("amount", True), ("amount", 0),
                               ("amount_refunded", None), ("amount_refunded", -1),
                               ("amount_refunded", "0"), ("amount_refunded", 1000)):
                with self.subTest(key=key, value=value):
                    original = charge[key]
                    charge[key] = value
                    try:
                        self.assertFalse(_verified_unreversed_invoice(invoice, {}))
                    finally:
                        charge[key] = original
            charge["amount_refunded"] = 1  # partial refunds do not terminate service
            self.assertTrue(_verified_unreversed_invoice(invoice, {}))
            charge["amount_refunded"] = 999
            self.assertFalse(_verified_unreversed_invoice(invoice, {}))
            charge.update(amount_refunded=0, disputed=True)
            self.assertFalse(_verified_unreversed_invoice(invoice, {}))

    def test_lease_issuer_requires_current_bound_paid_subscription(self):
        from contextlib import contextmanager
        from datetime import datetime, timedelta, timezone
        import app.local_gateway_remote_billing as boundary
        now = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
        sub = deepcopy(self.subscription)
        sub["latest_invoice"] = "in_test"
        sub["cancel_at_period_end"] = True
        sub["current_period_end"] = int((now + timedelta(hours=2)).timestamp())
        binding = {"customer_id": "customer-1", "status": "active", "payment_failed_at": None,
                   "stripe_subscription_id": "sub_test_1", "stripe_customer_id": "cus_test_1",
                   "stripe_price_id": "price_test_1"}
        @contextmanager
        def connection():
            yield object()
        partner = SimpleNamespace(row=lambda sql, args: binding, connection=connection)
        state = SimpleNamespace(current_subscription=lambda subid: sub,
                                bound_elsewhere=lambda stripe_customer, customer: False,
                                _id=lambda value: value,
                                stripe_get=lambda path, what: {"id": "in_test", "status": "paid", "amount_paid": 0},
                                subscription_payment_reversal=lambda sub: None)
        signer = SimpleNamespace(ensure_signing_key=lambda db: {"key_id": "test", "private_key_b64": "synthetic"},
                                 sign_body=lambda body, **kwargs: {"key_id": "test", "value": "synthetic"})
        appliance = {"id": "appliance-1", "cloud_id": "cloud-1", "customer_id": "customer-1"}
        with patch.dict(sys.modules, {"partner_db": partner, "stripe_state": state,
                                      "appliance_identity": signer,
                                      "sales_commissions": SimpleNamespace()}), \
                patch.object(boundary, "remote_access_for_appliance", return_value=True):
            document = issue_remote_lease(appliance, "synthetic-nonce-long-enough", now=now)
            self.assertEqual(datetime.fromisoformat(document["expires_at"]), now + timedelta(hours=2))
            # A normally renewing subscription is also paid only through its
            # currently verified period; do not sign past that boundary.
            sub["cancel_at_period_end"] = False
            document = issue_remote_lease(appliance, "synthetic-nonce-long-enough", now=now)
            self.assertEqual(datetime.fromisoformat(document["expires_at"]), now + timedelta(hours=2))
            for period in (None, True, 0, int(now.timestamp())):
                sub["current_period_end"] = period
                self.assertIsNone(issue_remote_lease(appliance, "synthetic-nonce-long-enough", now=now))
            sub["current_period_end"] = int((now + timedelta(hours=48)).timestamp())
            document = issue_remote_lease(appliance, "synthetic-nonce-long-enough", now=now)
            self.assertEqual(datetime.fromisoformat(document["expires_at"]), now + timedelta(hours=24))
            sub["cancel_at"] = int((now + timedelta(minutes=30)).timestamp())
            document = issue_remote_lease(appliance, "synthetic-nonce-long-enough", now=now)
            self.assertEqual(datetime.fromisoformat(document["expires_at"]), now + timedelta(minutes=30))
            sub["cancel_at"] = None
            sub["current_period_end"] = int((now + timedelta(hours=2)).timestamp())
            first = dict(binding)
            for changed in ({"payment_failed_at": "2026-11-03T10:00:00"},
                            {"stripe_customer_id": "cus_other"}, {"stripe_price_id": "price_other"},
                            {"stripe_subscription_id": "sub_replacement"}, {"status": "suspended"}):
                with self.subTest(changed=changed):
                    partner.row = Mock(side_effect=[first, {**binding, **changed}])
                    self.assertIsNone(issue_remote_lease(appliance, "synthetic-nonce-long-enough", now=now))
            partner.row = lambda sql, args: binding
            sub["status"] = "canceled"
            self.assertIsNone(issue_remote_lease(appliance, "synthetic-nonce-long-enough", now=now))
            sub["status"] = "active"
            binding["payment_failed_at"] = "2026-11-03T10:00:00"
            self.assertIsNone(issue_remote_lease(appliance, "synthetic-nonce-long-enough", now=now))

    def test_existing_signature_verifier_rejects_invalid_signature(self):
        import hashlib
        import hmac
        import time
        from app.tests.test_gateway_licensing import load_functions
        ns = {"STRIPE_WEBHOOK_SECRET": "synthetic-test-secret",
              "STRIPE_WEBHOOK_TOLERANCE_SECONDS": 300,
              "hashlib": hashlib, "hmac": hmac, "time": time}
        load_functions("main.py", {"parse_stripe_signature", "verify_stripe_webhook_signature"}, ns)
        timestamp = int(time.time())
        payload = b'{"type":"customer.subscription.updated"}'
        signed = str(timestamp).encode() + b"." + payload
        valid = hmac.new(b"synthetic-test-secret", signed, hashlib.sha256).hexdigest()
        self.assertTrue(ns["verify_stripe_webhook_signature"](payload, f"t={timestamp},v1={valid}"))
        self.assertFalse(ns["verify_stripe_webhook_signature"](payload, f"t={timestamp},v1=invalid"))
        self.assertFalse(ns["verify_stripe_webhook_signature"](payload + b"x", f"t={timestamp},v1={valid}"))

    def test_one_system_is_active_only_after_payment_verification(self):
        self.assertEqual(self.evaluate().state, "active")
        self.assertEqual(self.evaluate(payment_verified=False).reason, "payment_not_verified")
        for quantity in (0, 2, True, "1"):
            with self.subTest(quantity=quantity):
                sub = deepcopy(self.subscription)
                sub["items"]["data"][0]["quantity"] = quantity
                self.assertEqual(self.evaluate(sub).reason, "invalid_system_quantity")

    def test_payment_verification_requires_explicit_boolean_true(self):
        for value in (None, 0, 1, "true", [], {}):
            with self.subTest(value=value):
                self.assertEqual(self.evaluate(payment_verified=value).reason, "payment_not_verified")

    def test_subscription_with_unrelated_extra_item_is_not_eligible(self):
        sub = deepcopy(self.subscription)
        sub["items"]["data"].append({"price": {"id": "price_unrelated"}, "quantity": 1})
        self.assertEqual(self.evaluate(sub).reason, "price_not_unique")

    def test_cross_customer_or_appliance_never_grants_access(self):
        self.assertEqual(self.evaluate(customer_id="other").reason, "customer_mismatch")
        self.assertEqual(self.evaluate(stripe_customer_id="cus_other").reason, "customer_mismatch")
        self.assertEqual(self.evaluate(appliance_id="other").reason, "appliance_mismatch")

    def test_cancellation_is_inactive_without_asserting_local_licence(self):
        self.subscription["status"] = "canceled"
        self.assertEqual(self.evaluate().state, "inactive")

    def test_malformed_provider_data_fails_closed(self):
        for value in (None, [], {}, 123):
            with self.subTest(value=value):
                sub = deepcopy(self.subscription)
                sub["status"] = value
                self.assertEqual(self.evaluate(sub).state, "denied")
        self.subscription["id"] = {"unexpected": "value"}
        self.assertEqual(self.evaluate().state, "denied")

    def test_unknown_status_and_duplicate_or_wrong_price_fail_closed(self):
        self.subscription["status"] = "past_due"
        self.assertEqual(self.evaluate().state, "denied")
        self.subscription["status"] = "active"
        self.assertEqual(self.evaluate(price_id="price_other").reason, "price_not_unique")
        self.subscription["items"]["data"].append(deepcopy(self.subscription["items"]["data"][0]))
        self.assertEqual(self.evaluate().reason, "price_not_unique")


if __name__ == "__main__":
    unittest.main()
