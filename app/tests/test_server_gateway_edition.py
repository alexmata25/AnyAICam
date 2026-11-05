"""Focused server-owned edition checks; no migration or external services."""
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server_gateway_edition import onboarding_edition
from local_gateway_remote_billing import remote_access_for_appliance


class ServerEditionTests(unittest.TestCase):
    def test_only_global_admin_can_assign_onboarding_edition(self):
        # A server-side order without a Gateway choice records Full VMS explicitly.
        self.assertEqual(onboarding_edition({}, is_global_admin=False), "full_vms")
        self.assertEqual(onboarding_edition({"gateway_edition": "full_vms"}, is_global_admin=False), "full_vms")
        self.assertEqual(onboarding_edition({"gateway_edition": "local_gateway"}, is_global_admin=True), "local_gateway")
        with self.assertRaises(PermissionError):
            onboarding_edition({"gateway_edition": "local_gateway"}, is_global_admin=False)
        with self.assertRaises(ValueError):
            onboarding_edition({"gateway_edition": "cloud_gateway"}, is_global_admin=True)

    def test_remote_requires_owned_local_appliance_and_current_binding(self):
        appliance = {"customer_id": "customer-1", "gateway_edition": "local_gateway"}
        binding = {"customer_id": "customer-1", "status": "active"}
        db = SimpleNamespace(row=lambda sql, args: appliance if "FROM appliances" in sql else binding, transaction=lambda:nullcontext(object()))
        edition_gate = SimpleNamespace(edition_authorization_effective=lambda *args:True)
        with patch.dict(sys.modules, {"partner_db": db, "gateway_internal_provisioning":edition_gate, "appliance_readiness":SimpleNamespace(appliance_activated=lambda appliance:True)}):
            self.assertTrue(remote_access_for_appliance("customer-1", "appliance-1"))
            self.assertFalse(remote_access_for_appliance("customer-2", "appliance-1"))
            for edition in (None, "cloud_gateway", "invalid"):
                appliance["gateway_edition"] = edition
                self.assertFalse(remote_access_for_appliance("customer-1", "appliance-1"))
            appliance["gateway_edition"] = "local_gateway"
            for state in ("revoked", "retired"):
                appliance["state"] = state
                self.assertFalse(remote_access_for_appliance("customer-1", "appliance-1"))
            appliance["state"] = "online"
            binding["status"] = "inactive"
            self.assertFalse(remote_access_for_appliance("customer-1", "appliance-1"))

    def test_missing_gateway_schema_or_edition_never_grants_gateway_access(self):
        appliance = {"customer_id":"customer-1", "gateway_edition":"local_gateway"}
        def broken_transaction():
            raise RuntimeError('schema unavailable')
        db = SimpleNamespace(row=lambda sql,args:appliance,transaction=broken_transaction)
        with patch.dict(sys.modules, {"partner_db":db,"appliance_readiness":SimpleNamespace(appliance_activated=lambda appliance:True)}):
            self.assertFalse(remote_access_for_appliance('customer-1','appliance-1'))
            del appliance['gateway_edition']
            self.assertFalse(remote_access_for_appliance('customer-1','appliance-1'))

if __name__ == "__main__":
    unittest.main()
