"""Opt-in isolated HTTP acceptance; not run without separate owner approval."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.tests.local_gateway_http_harness import full_vms_app, isolated_client


@unittest.skipUnless(os.environ.get("ANYAICAM_APPROVED_HTTP_ACCEPTANCE") == "1",
                     "isolated HTTP acceptance requires separate owner approval")
class LocalGatewayHttpAcceptance(unittest.IsolatedAsyncioTestCase):
    async def test_checkout_stays_disabled_for_unauthenticated_request(self):
        from fastapi import FastAPI
        from app.local_gateway_remote_billing import register_routes
        # The endpoint imports these before it checks the default-off flag.
        # Explicit fakes prevent production app, database and provider imports.
        modules = {name: SimpleNamespace() for name in
                   ("partner_db", "checkout_guard", "customer_entitlements", "main", "per_camera_billing")}
        modules["partner_portal"] = SimpleNamespace(partner_identity=lambda request: None)
        with patch.dict(sys.modules, modules), patch.dict(os.environ, {"ANYAICAM_LOCAL_GATEWAY_CHECKOUT_ENABLED": ""}):
            def factory(fixtures):
                app = FastAPI()
                register_routes(app)
                return app
            async with isolated_client(factory) as (client, fixtures):
                for caller, target in ((None, fixtures.appliances[0]),
                                       ("synthetic-owner-a", fixtures.appliances[0]),
                                       ("synthetic-owner-a", fixtures.appliances[1]),
                                       ("synthetic-viewer-a", fixtures.appliances[0]),
                                       (fixtures.admin, fixtures.appliances[0])):
                    headers = {"x-synthetic-caller": caller} if caller else {}
                    with self.subTest(caller=caller, target=target):
                        response = await client.post("/api/v2/customer/local-gateway/checkout",
                                                     json={"appliance_id": target}, headers=headers)
                        self.assertEqual(response.status_code, 503)
                self.assertEqual(fixtures.stripe.calls, [])


    async def test_full_vms_checkout_identity_and_webhook_signature(self):
        import hashlib
        import hmac
        import json
        import time
        async with isolated_client(full_vms_app) as (client, fixtures):
            checkout = "/api/v2/customer/local-gateway/checkout"
            body = {"appliance_id": fixtures.appliances[0]}
            anonymous = await client.post(checkout, json=body)
            self.assertNotEqual(anonymous.status_code, 503)
            self.assertIn(anonymous.status_code, (401, 403))
            owner_a = {"anyaicam_partner_session": fixtures.cookies["a"]}
            owner_b = {"anyaicam_partner_session": fixtures.cookies["b"]}
            foreign = await client.post(checkout, json=body, cookies=owner_b)
            self.assertNotEqual(foreign.status_code, 503)
            self.assertEqual(foreign.status_code, 403)
            owned = await client.post(checkout, json=body, cookies=owner_a)
            self.assertNotEqual(owned.status_code, 503)
            self.assertEqual(owned.status_code, 200, owned.text)
            admin = {"anyaicam_partner_session": fixtures.cookies["admin"]}
            onboarding = "/api/partner/customers/onboard"
            request = {"gateway_edition": "local_gateway", "sites": [], "email": "synthetic-new@example.test"}
            non_admin = await client.post(onboarding, json=request, cookies=owner_a)
            self.assertIn(non_admin.status_code, (401, 403))
            # Administrator authentication reaches validation rather than being denied.
            administrator = await client.post(onboarding, json=request, cookies=admin)
            self.assertEqual(administrator.status_code, 400, administrator.text)
            import partner_workspace
            import partner_db
            quote = {"quantity": 1, "resolution": "2mp", "recording": "motion", "retention_days": 1,
                     "addons": [], "monthly_customer_revenue": 0, "monthly_partner_charge": 0,
                     "monthly_recurring_profit": 0, "annual_total": 0}
            provisioning = SimpleNamespace(provision=lambda order, **kw: {
                "cloud_id": "AIC-SYNTHETIC-NEW", "appliance_id": "synthetic-new-appliance",
                "activation_token": "synthetic-activation-token", "provisioning_qr_payload": "synthetic-qr"})
            complete_request = {**request, "name": "Synthetic customer", "partner_id": "synthetic-partner",
                                "sites": [{"name": "Synthetic site"}], "pricing": {}}
            with patch.object(partner_workspace, "calculate_partner_quote", return_value=quote), \
                    patch.object(partner_workspace, "get_provisioning_backend", return_value=provisioning):
                created = await client.post(onboarding, json=complete_request, cookies=admin)
            self.assertEqual(created.status_code, 200, created.text)
            with partner_db.connection() as db:
                row = db.execute("SELECT customer_id,gateway_edition FROM appliances WHERE id=?",
                                 ("synthetic-new-appliance",)).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["gateway_edition"], "local_gateway")
                self.assertNotIn(row["customer_id"], fixtures.customers)
            event = {"id": "evt_synthetic_noop", "type": "synthetic.unhandled", "data": {"object": {}}}
            payload = json.dumps(event, separators=(",", ":")).encode()
            timestamp = int(time.time())
            digest = hmac.new(b"synthetic-webhook-secret", str(timestamp).encode() + b"." + payload, hashlib.sha256).hexdigest()
            webhook = "/api/payments/stripe/webhook"
            invalid = await client.post(webhook, content=payload, headers={"stripe-signature": f"t={timestamp},v1=invalid"})
            self.assertEqual(invalid.status_code, 400)
            signed = {"stripe-signature": f"t={timestamp},v1={digest}"}
            first = await client.post(webhook, content=payload, headers=signed)
            self.assertEqual(first.status_code, 200, first.text)
            repeat = await client.post(webhook, content=payload, headers=signed)
            self.assertEqual(repeat.status_code, 200, repeat.text)
            self.assertTrue(repeat.json().get("duplicate"))

            # Seed one appliance's paid binding in the disposable database.
            # The other appliance has a purchased licence but no remote plan.
            from datetime import datetime, timezone
            import secrets
            import appliance_identity
            import local_gateway_remote_lease
            price, sub_id, invoice_id, intent_id = "price_synthetic_999", "sub_synthetic_a", "in_synthetic_a", "pi_synthetic_a"
            subscription = {"id": sub_id, "status": "active", "customer": "cus_synthetic-customer-a",
                            "metadata": {"anyaicam_customer_id": fixtures.customers[0],
                                         "anyaicam_appliance_id": fixtures.appliances[0],
                                         "anyaicam_product": "local_gateway_remote"},
                            "items": {"data": [{"id": "si_synthetic", "price": {"id": price}, "quantity": 1}]},
                            "latest_invoice": invoice_id}
            fixtures.subscriptions[sub_id] = subscription
            charge = {"id": "ch_synthetic_a", "disputed": False, "refunded": False,
                      "amount": 999, "amount_refunded": 0}
            fixtures.stripe.responses["/v1/invoices/" + invoice_id] = {
                "id": invoice_id, "status": "paid", "amount_paid": 999, "payment_intent": intent_id}
            fixtures.stripe.responses["/v1/payment_intents/" + intent_id + "?expand[]=latest_charge"] = {"latest_charge": charge}
            stamp = datetime.now(timezone.utc).isoformat()
            with partner_db.connection() as db:
                db.execute("INSERT INTO local_gateway_remote_subscriptions "
                           "(appliance_id,customer_id,stripe_customer_id,stripe_subscription_id,"
                           "stripe_subscription_item_id,stripe_price_id,status,stripe_status,created_at,updated_at) "
                           "VALUES(?,?,?,?,?,?,'active','active',?,?)",
                           (fixtures.appliances[0], fixtures.customers[0], "cus_synthetic-customer-a",
                            sub_id, "si_synthetic", price, stamp, stamp))
            async def config(suffix):
                nonce = secrets.token_hex(16)
                headers = {"x-appliance-id": "synthetic-appliance-" + suffix,
                           "x-request-timestamp": str(int(time.time())), "x-request-nonce": nonce,
                           "authorization": "Bearer synthetic-credential-" + suffix}
                response = await client.get("/api/appliance/configuration", headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                return response.json(), nonce
            invalid_headers = {"x-appliance-id": fixtures.appliances[0],
                               "x-request-timestamp": str(int(time.time())),
                               "x-request-nonce": secrets.token_hex(16),
                               "authorization": "Bearer synthetic-credential-b"}
            foreign_appliance = await client.get("/api/appliance/configuration", headers=invalid_headers)
            self.assertEqual(foreign_appliance.status_code, 403)
            config_a, nonce_a = await config("a")
            config_b, _ = await config("b")
            self.assertTrue(config_a["local_gateway_remote_access"])
            self.assertFalse(config_b["local_gateway_remote_access"])
            self.assertEqual(config_a["feature_entitlements"]["camera_capacity"]["purchased_vms_license_capacity"], 4)
            self.assertEqual(config_b["feature_entitlements"]["camera_capacity"]["purchased_vms_license_capacity"], 4)
            lease = config_a["local_gateway_remote_lease"]
            identity = {"appliance_id": fixtures.appliances[0], "customer_id": fixtures.customers[0], "cloud_id": "AIC-A"}
            expires = datetime.fromisoformat(lease["expires_at"])
            self.assertEqual(local_gateway_remote_lease.verify_lease(
                lease, config_a["signing_public_keys"], identity=identity, nonce=nonce_a,
                now=datetime.fromisoformat(lease["issued_at"])), None)
            self.assertIsNotNone(local_gateway_remote_lease.verify_lease(
                lease, config_a["signing_public_keys"], identity=identity, nonce=nonce_a, now=expires))
            self.assertLessEqual((expires - datetime.fromisoformat(lease["issued_at"])).total_seconds(), 86400)

            async def webhook_event(event_id, event_type):
                data = {"id": event_id, "type": event_type,
                        "data": {"object": {"id": sub_id, "metadata": subscription["metadata"]}}}
                raw = json.dumps(data, separators=(",", ":")).encode()
                ts = int(time.time())
                signature = hmac.new(b"synthetic-webhook-secret", str(ts).encode() + b"." + raw, hashlib.sha256).hexdigest()
                return await client.post(webhook, content=raw, headers={"stripe-signature": f"t={ts},v1={signature}"})
            # A transient failure must return a retryable response, then replay safely.
            with partner_db.connection() as db:
                db.execute("CREATE TRIGGER synthetic_reject_write BEFORE UPDATE ON local_gateway_remote_subscriptions "
                           "BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END")
            try:
                failed = await webhook_event("evt_synthetic_retry", "customer.subscription.updated")
                self.assertEqual(failed.status_code, 503, failed.text)
            finally:
                with partner_db.connection() as db:
                    db.execute("DROP TRIGGER synthetic_reject_write")
            recovered = await webhook_event("evt_synthetic_retry", "customer.subscription.updated")
            self.assertEqual(recovered.status_code, 200, recovered.text)
            subscription["status"] = "past_due"
            failure = await webhook_event("evt_synthetic_payment_failure", "customer.subscription.updated")
            self.assertEqual(failure.status_code, 200, failure.text)
            unpaid, _ = await config("a")
            self.assertFalse(unpaid["local_gateway_remote_access"])
            subscription["status"] = "active"
            payment_recovered = await webhook_event("evt_synthetic_payment_recovered", "customer.subscription.updated")
            self.assertEqual(payment_recovered.status_code, 200, payment_recovered.text)
            paid_again, _ = await config("a")
            self.assertTrue(paid_again["local_gateway_remote_access"])
            subscription["status"] = "canceled"
            canceled = await webhook_event("evt_synthetic_cancel", "customer.subscription.deleted")
            self.assertEqual(canceled.status_code, 200, canceled.text)
            after_cancel, _ = await config("a")
            self.assertFalse(after_cancel["local_gateway_remote_access"])
            self.assertEqual(after_cancel["feature_entitlements"]["camera_capacity"]["purchased_vms_license_capacity"], 4)
            stale = await webhook_event("evt_synthetic_stale", "customer.subscription.updated")
            self.assertEqual(stale.status_code, 200, stale.text)
            after_stale, _ = await config("a")
            self.assertFalse(after_stale["local_gateway_remote_access"])


if __name__ == "__main__":
    unittest.main()
