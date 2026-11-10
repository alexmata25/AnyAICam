"""Deterministic mocked webhook ordering; no app startup, migrations or providers."""
from contextlib import contextmanager
from copy import deepcopy
import sys
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import local_gateway_remote_billing as billing


class WebhookConcurrencyTests(unittest.TestCase):
    def test_simultaneous_new_checkout_bindings_and_redelivery(self):
        gate = patch.object(billing, 'CHECKOUT_AUTHORIZATION_ATOMICALLY_ENFORCED', True)
        gate.start(); self.addCleanup(gate.stop)
        evidence = patch.dict(sys.modules, {'gateway_internal_provisioning':SimpleNamespace(edition_authorization_effective=lambda *args:True)})
        evidence.start(); self.addCleanup(evidence.stop)
        for conflicting in (False, True):
            with self.subTest(conflicting=conflicting):
                db = sqlite3.connect(":memory:", check_same_thread=False)
                self.addCleanup(db.close)
                db.executescript("""
                    CREATE TABLE appliances(id TEXT,customer_id TEXT,gateway_edition TEXT);
                    CREATE TABLE checkout_pending(customer_id TEXT,purchase TEXT,session_id TEXT,price_id TEXT,quantity INTEGER,status TEXT);
                    CREATE TABLE customer_entitlements(id TEXT,product TEXT,status TEXT);
                    CREATE TABLE local_gateway_remote_subscriptions(appliance_id TEXT PRIMARY KEY,customer_id TEXT,stripe_customer_id TEXT,
                        stripe_subscription_id TEXT UNIQUE,stripe_subscription_item_id TEXT,stripe_price_id TEXT,status TEXT,stripe_status TEXT,
                        current_period_end INTEGER,cancel_at_period_end INTEGER,payment_failed_at TEXT,suspended_reason TEXT,created_at TEXT,updated_at TEXT);
                    INSERT INTO appliances VALUES('a','c','local_gateway');
                    INSERT INTO checkout_pending VALUES('c','local_gateway_remote:a','cs-1','price',1,'open');
                    INSERT INTO checkout_pending VALUES('c','local_gateway_remote:a','cs-2','price',1,'open');
                    INSERT INTO customer_entitlements VALUES('license','vms_license','active');
                """)
                lock = threading.Lock()
                barrier = threading.Barrier(2, timeout=3)
                decisions, errors = [], []
                def bind(number):
                    sub_id = 'sub-2' if conflicting and number == 2 else 'sub-1'
                    session_id = 'cs-2' if conflicting and number == 2 else 'cs-1'
                    subscription = {'id':sub_id,'customer':'cus','status':'active',
                        'metadata':{'anyaicam_customer_id':'c','anyaicam_appliance_id':'a','anyaicam_product':'local_gateway_remote'},
                        'items':{'data':[{'id':'si-'+sub_id,'price':{'id':'price'},'quantity':1}]}}
                    session = {'id':session_id,'mode':'subscription','payment_status':'paid','customer':'cus','subscription':sub_id}
                    try:
                        barrier.wait()
                        with lock, db:
                            decisions.append(billing.bind_verified_paid_checkout(db, session, subscription,
                                customer_id='c',appliance_id='a',stripe_customer_id='cus',price_id='price',
                                payment_verified=True,verified_at='stamp',observed_binding=None,protect_observed_binding=True))
                    except Exception as exc:
                        errors.append(exc)
                workers = [threading.Thread(target=bind,args=(n,)) for n in (1,2)]
                for worker in workers: worker.start()
                for worker in workers: worker.join(5)
                self.assertFalse(any(worker.is_alive() for worker in workers))
                self.assertEqual(errors, [])
                self.assertEqual(sorted(d.state for d in decisions), ['active','denied'])
                self.assertEqual(next(d.reason for d in decisions if d.state == 'denied'), 'binding_changed')
                self.assertEqual(db.execute('SELECT count(*) FROM local_gateway_remote_subscriptions').fetchone()[0], 1)
                winner, state, stripe_state, stamp = db.execute('SELECT stripe_subscription_id,status,stripe_status,updated_at FROM local_gateway_remote_subscriptions').fetchone()
                if not conflicting:
                    subscription = {'id':winner,'customer':'cus','status':'active',
                        'metadata':{'anyaicam_customer_id':'c','anyaicam_appliance_id':'a','anyaicam_product':'local_gateway_remote'},
                        'items':{'data':[{'id':'si-'+winner,'price':{'id':'price'},'quantity':1}]}}
                    session = {'id':'cs-1','mode':'subscription','payment_status':'paid','customer':'cus','subscription':winner}
                    observed = {'stripe_subscription_id':winner,'status':state,'stripe_status':stripe_state,'updated_at':stamp}
                    replay = billing.bind_verified_paid_checkout(db,session,subscription,customer_id='c',appliance_id='a',
                        stripe_customer_id='cus',price_id='price',payment_verified=True,verified_at='replayed',
                        observed_binding=observed,protect_observed_binding=True)
                    self.assertEqual(replay.state, 'active')
                else:
                    loser = 'sub-2' if winner == 'sub-1' else 'sub-1'
                    session_id = 'cs-2' if loser == 'sub-2' else 'cs-1'
                    subscription = {'id':loser,'customer':'cus','status':'active',
                        'metadata':{'anyaicam_customer_id':'c','anyaicam_appliance_id':'a','anyaicam_product':'local_gateway_remote'},
                        'items':{'data':[{'id':'si-'+loser,'price':{'id':'price'},'quantity':1}]}}
                    session = {'id':session_id,'mode':'subscription','payment_status':'paid','customer':'cus','subscription':loser}
                    observed = {'stripe_subscription_id':winner,'status':state,'stripe_status':stripe_state,'updated_at':stamp}
                    retry = billing.bind_verified_paid_checkout(db,session,subscription,customer_id='c',appliance_id='a',
                        stripe_customer_id='cus',price_id='price',payment_verified=True,verified_at='retry',
                        observed_binding=observed,protect_observed_binding=True)
                    self.assertEqual(retry.reason, 'previous_subscription_not_inactive')
                    self.assertEqual(db.execute('SELECT stripe_subscription_id FROM local_gateway_remote_subscriptions').fetchone()[0], winner)
                self.assertEqual(db.execute("SELECT status FROM customer_entitlements WHERE id='license'").fetchone()[0], 'active')

    def test_current_invoice_reversal_suspends_only_its_subscription(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE local_gateway_remote_subscriptions(appliance_id TEXT,customer_id TEXT,stripe_customer_id TEXT,
                stripe_subscription_id TEXT,stripe_price_id TEXT,status TEXT,stripe_status TEXT,suspended_reason TEXT,updated_at TEXT);
            CREATE TABLE customer_entitlements(id TEXT,product TEXT,status TEXT);
            INSERT INTO customer_entitlements VALUES('license','vms_license','active');
            INSERT INTO local_gateway_remote_subscriptions VALUES('a','c','cus','sub-a','p','active','active',NULL,'old');
            INSERT INTO local_gateway_remote_subscriptions VALUES('b','c','cus','sub-b','p','active','active',NULL,'old');
        """)
        @contextmanager
        def transaction():
            with db:
                yield db
        def row(sql, args):
            found = db.execute(sql, args).fetchone()
            return dict(found) if found else None
        provider = SimpleNamespace(RetryableStripeEventError=RuntimeError, bound_elsewhere=lambda *args:False,
            current_subscription=lambda sub:{"id":sub,"status":"active","latest_invoice":"in-current"},
            _id=lambda value:value, stripe_get=lambda *args:{"id":"in-current","status":"paid"},
            subscription_payment_reversal=lambda sub:"disputed")
        event = {"type":"customer.subscription.updated", "data":{"object":{"id":"sub-a",
                 "metadata":{"anyaicam_product":"local_gateway_remote"}}}}
        with patch.dict(sys.modules, {"partner_db":SimpleNamespace(row=row,connection=transaction,transaction=transaction),
                                      "stripe_state":provider}):
            self.assertEqual(billing.sync_from_verified_webhook(event), {"status":"suspended","reason":"disputed"})
        self.assertEqual([tuple(row) for row in db.execute("SELECT stripe_subscription_id,status FROM local_gateway_remote_subscriptions ORDER BY stripe_subscription_id")],
                         [("sub-a","suspended"),("sub-b","active")])
        self.assertEqual(db.execute("SELECT status FROM customer_entitlements WHERE id='license'").fetchone()[0], 'active')

    def test_delayed_checkout_does_not_roll_back_renewed_period(self):
        old_read, renewed = threading.Event(), threading.Event()
        lock = threading.RLock()
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE appliances(id TEXT, customer_id TEXT, gateway_edition TEXT);
            CREATE TABLE checkout_pending(customer_id TEXT,purchase TEXT,session_id TEXT,price_id TEXT,quantity INTEGER,status TEXT);
            CREATE TABLE local_gateway_remote_subscriptions(appliance_id TEXT,customer_id TEXT,stripe_customer_id TEXT,
                stripe_subscription_id TEXT,stripe_subscription_item_id TEXT,stripe_price_id TEXT,status TEXT,stripe_status TEXT,
                current_period_end INTEGER,cancel_at_period_end INTEGER,updated_at TEXT);
            INSERT INTO appliances VALUES('appliance-1','customer-1','local_gateway');
            INSERT INTO checkout_pending VALUES('customer-1','local_gateway_remote:appliance-1','cs-1','price-1',1,'open');
            INSERT INTO local_gateway_remote_subscriptions VALUES('appliance-1','customer-1','cus-1','sub-1','si-1','price-1','active','active',50,0,'original');
        """)
        @contextmanager
        def transaction():
            with lock, db:
                yield db
            if threading.current_thread().name == "renewal":
                renewed.set()
        def row(sql, args):
            with lock:
                found = db.execute(sql, args).fetchone()
                return dict(found) if found else None
        def current(_):
            old = threading.current_thread().name == "old-checkout"
            if old:
                old_read.set()
                if not renewed.wait(3):
                    raise TimeoutError("renewal did not complete")
            return {"id":"sub-1", "customer":"cus-1", "status":"active", "latest_invoice":"in-1",
                    "current_period_end":100 if old else 200,
                    "metadata":{"anyaicam_product":"local_gateway_remote", "anyaicam_customer_id":"customer-1", "anyaicam_appliance_id":"appliance-1"},
                    "items":{"data":[{"id":"si-1", "price":{"id":"price-1"}, "quantity":1}]}}
        provider = SimpleNamespace(RetryableStripeEventError=RuntimeError, bound_elsewhere=lambda *args:False,
            current_subscription=current, _id=lambda value:value,
            stripe_get=lambda *args:{"id":"in-1","status":"paid"}, subscription_payment_reversal=lambda sub:None)
        partner = SimpleNamespace(row=row, connection=transaction, transaction=transaction)
        checkout = {"type":"checkout.session.completed", "data":{"object":{"id":"cs-1","mode":"subscription",
            "payment_status":"paid","customer":"cus-1","subscription":"sub-1", "metadata":{
                "anyaicam_product":"local_gateway_remote","anyaicam_customer_id":"customer-1","anyaicam_appliance_id":"appliance-1"}}}}
        renewal = {"type":"customer.subscription.updated", "data":{"object":{"id":"sub-1", "metadata":{"anyaicam_product":"local_gateway_remote"}}}}
        errors = []
        def invoke(event):
            try:
                billing.sync_from_verified_webhook(event)
            except Exception as exc:
                errors.append(exc); renewed.set()
        with patch.dict(sys.modules, {"partner_db":partner,"stripe_state":provider,
                "gateway_internal_provisioning":SimpleNamespace(edition_authorization_effective=lambda *args:True)}), \
                patch.object(billing, "_verified_unreversed_invoice", return_value=True):
            old = threading.Thread(target=invoke, args=(checkout,), name="old-checkout")
            old.start(); self.assertTrue(old_read.wait(3))
            new = threading.Thread(target=invoke, args=(renewal,), name="renewal")
            new.start(); new.join(5); old.join(5)
            self.assertFalse(new.is_alive() or old.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIn("not provisioned", str(errors[0]))
            self.assertEqual(db.execute("SELECT current_period_end FROM local_gateway_remote_subscriptions").fetchone()[0], 200)
            self.assertEqual(billing.sync_from_verified_webhook(checkout)["status"], "active")

    def test_stale_refund_or_dispute_does_not_suspend_new_paid_invoice(self):
        for reason in ("refunded", "disputed"):
            with self.subTest(reason=reason):
                self._stale_reversal_after_renewal(reason)

    def _stale_reversal_after_renewal(self, reason):
        old_read = threading.Event()
        renewed = threading.Event()
        lock = threading.RLock()
        db = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE local_gateway_remote_subscriptions (
                appliance_id TEXT, customer_id TEXT, stripe_customer_id TEXT,
                stripe_subscription_id TEXT, stripe_price_id TEXT, status TEXT,
                stripe_status TEXT, current_period_end INTEGER, cancel_at_period_end INTEGER,
                suspended_reason TEXT, updated_at TEXT);
            CREATE TABLE appliances(id TEXT, customer_id TEXT, gateway_edition TEXT);
            INSERT INTO appliances VALUES ('appliance-1', 'customer-1', 'local_gateway');
            INSERT INTO local_gateway_remote_subscriptions VALUES
                ('appliance-1','customer-1','cus-1','sub-1','price-1',
                 'active','active',NULL,0,NULL,'original');
        """)
        @contextmanager
        def transaction():
            with lock, db:
                yield db
            if threading.current_thread().name == "renewal":
                renewed.set()
        def row(sql, args):
            with lock:
                result = db.execute("SELECT status,stripe_status,updated_at FROM local_gateway_remote_subscriptions WHERE stripe_subscription_id=?", args).fetchone()
            return {"customer_id":"customer-1", "appliance_id":"appliance-1", "stripe_customer_id":"cus-1",
                    "stripe_price_id":"price-1", "status":result[0], "stripe_status":result[1], "updated_at":result[2]}
        def current(_):
            old = threading.current_thread().name == "old-reversal"
            if old:
                old_read.set()
                if not renewed.wait(3):
                    raise TimeoutError("renewal did not complete")
            return {"id":"sub-1", "customer":"cus-1", "status":"active", "latest_invoice":"in-old" if old else "in-new",
                    "metadata":{"anyaicam_product":"local_gateway_remote", "anyaicam_customer_id":"customer-1", "anyaicam_appliance_id":"appliance-1"},
                    "items":{"data":[{"price":{"id":"price-1"}, "quantity":1}]}}
        provider = SimpleNamespace(RetryableStripeEventError=RuntimeError, bound_elsewhere=lambda *args: False,
            current_subscription=current, _id=lambda value:value,
            stripe_get=lambda path, what:{"id":path.rsplit('/',1)[-1], "status":"paid"},
            subscription_payment_reversal=lambda sub: reason if sub["latest_invoice"]["id"] == "in-old" else None)
        partner = SimpleNamespace(row=row, connection=transaction, transaction=transaction)
        event = {"type":"customer.subscription.updated", "data":{"object":{"id":"sub-1", "metadata":{"anyaicam_product":"local_gateway_remote"}}}}
        errors = []
        def invoke():
            try:
                billing.sync_from_verified_webhook(event)
            except Exception as exc:
                errors.append(exc)
                renewed.set()
        with patch.dict(sys.modules, {"partner_db":partner, "stripe_state":provider}), \
                patch.object(billing, "_table_available", return_value=True), \
                patch.object(billing, "_verified_unreversed_invoice", return_value=True):
            old = threading.Thread(target=invoke, name="old-reversal")
            old.start()
            self.assertTrue(old_read.wait(3))
            new = threading.Thread(target=invoke, name="renewal")
            new.start()
            new.join(5); old.join(5)
            self.assertFalse(new.is_alive() or old.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIn("binding changed", str(errors[0]))
            self.assertEqual(db.execute("SELECT status,suspended_reason FROM local_gateway_remote_subscriptions").fetchone(), ("active", None))
            self.assertEqual(billing.sync_from_verified_webhook(event)["status"], "active")

    def test_older_cancel_read_cannot_overwrite_newer_renewal(self):
        """A stale provider read is rejected and can be retried."""
        renewal_committed = threading.Event()
        binding = {"customer_id": "customer-1", "appliance_id": "appliance-1",
                   "stripe_customer_id": "cus-1", "stripe_price_id": "price-1"}
        db = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE local_gateway_remote_subscriptions (
                appliance_id TEXT, customer_id TEXT, stripe_customer_id TEXT,
                stripe_subscription_id TEXT, stripe_price_id TEXT, status TEXT,
                stripe_status TEXT, current_period_end INTEGER,
                cancel_at_period_end INTEGER, updated_at TEXT);
            CREATE TABLE appliances(id TEXT, customer_id TEXT, gateway_edition TEXT);
            INSERT INTO appliances VALUES ('appliance-1', 'customer-1', 'local_gateway');
            INSERT INTO local_gateway_remote_subscriptions VALUES
                ('appliance-1', 'customer-1', 'cus-1', 'sub-1', 'price-1',
                 'active', 'active', NULL, 0, 'original');
        """)
        lock = threading.Lock()
        current = {"id": "sub-1", "customer": "cus-1", "status": "active",
                   "latest_invoice": "in-1", "metadata": {
                       "anyaicam_product": "local_gateway_remote", "anyaicam_customer_id": "customer-1",
                       "anyaicam_appliance_id": "appliance-1"},
                   "items": {"data": [{"price": {"id": "price-1"}, "quantity": 1}]}}
        event = {"type": "customer.subscription.updated", "data": {"object": {
            "id": "sub-1", "metadata": {"anyaicam_product": "local_gateway_remote"}}}}
        @contextmanager
        def transaction():
            with lock, db:
                yield db
            if threading.current_thread().name == "newer-renewal":
                renewal_committed.set()
        def fetch(subscription_id):
            result = deepcopy(current)
            if threading.current_thread().name == "older-cancel":
                result["status"] = "canceled"
                old_read.set()
                if not renewal_committed.wait(3):
                    raise TimeoutError("renewal did not finish")
            return result
        old_read = threading.Event()
        errors = []
        def invoke():
            try:
                billing.sync_from_verified_webhook(event)
            except Exception as exc:
                errors.append(exc)
                renewal_committed.set()
        def row(sql, args):
            with lock:
                status, stripe_status, updated_at = db.execute(
                    "SELECT status,stripe_status,updated_at FROM local_gateway_remote_subscriptions "
                    "WHERE stripe_subscription_id=?", args).fetchone()
            return {**binding, "status": status, "stripe_status": stripe_status, "updated_at": updated_at}
        provider = SimpleNamespace(RetryableStripeEventError=RuntimeError,
                                   bound_elsewhere=lambda *args: False, current_subscription=fetch,
                                   _id=lambda value: value, stripe_get=lambda *args: {"id": "in-1", "status": "paid"},
                                   subscription_payment_reversal=lambda value: None)
        partner = SimpleNamespace(row=row, connection=transaction, transaction=transaction)
        with patch.dict(sys.modules, {"partner_db": partner, "stripe_state": provider}), \
                patch.object(billing, "_verified_unreversed_invoice", return_value=True):
            old = threading.Thread(target=invoke, name="older-cancel")
            old.start()
            self.assertTrue(old_read.wait(3))
            new = threading.Thread(target=invoke, name="newer-renewal")
            new.start()
            new.join(5)
            old.join(5)
            self.assertFalse(old.is_alive() or new.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], RuntimeError)
            self.assertIn("binding changed", str(errors[0]))
            stored = db.execute("SELECT status FROM local_gateway_remote_subscriptions").fetchone()[0]
            self.assertEqual(stored, "active", "stale cancellation overwrote newer renewal")
            self.assertEqual(billing.sync_from_verified_webhook(event)["status"], "active")
