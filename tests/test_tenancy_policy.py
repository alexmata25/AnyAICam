import sys
import unittest
from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from tenancy.navigation import navigation_keys
from tenancy.policy import authorize, authorize_camera, normalize_role


class TenancyPolicyTests(unittest.TestCase):
    def test_legacy_roles_map_to_independent_identity_domains(self):
        self.assertEqual(normalize_role({"role": "super_admin"}), ("platform", "owner"))
        self.assertEqual(normalize_role({"role": "administrator"}), ("platform", "owner"))
        self.assertEqual(normalize_role({"role": "customer_owner"}), ("customer", "customer_admin"))

    def test_customer_tenant_boundary_runs_before_role_permission(self):
        identity = {"enabled": True, "identity_domain": "customer", "customer_role": "customer_admin", "tenant_id": "tenant-a"}
        decision = authorize(identity, "camera.view", "tenant-b")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "resource belongs to another tenant")

    def test_platform_camera_data_requires_explicit_permission(self):
        support = {"enabled": True, "identity_domain": "platform", "platform_role": "support"}
        owner = {"enabled": True, "identity_domain": "platform", "platform_role": "owner"}
        self.assertFalse(authorize(support, "camera.view", "tenant-a").allowed)
        self.assertTrue(authorize(owner, "camera.view", "tenant-a").allowed)

    def test_customer_admin_can_share_while_viewer_needs_camera_grant(self):
        administrator = {"enabled": True, "identity_domain": "customer", "customer_role": "customer_admin", "tenant_id": "tenant-a"}
        viewer = {"enabled": True, "identity_domain": "customer", "customer_role": "viewer", "tenant_id": "tenant-a"}
        self.assertTrue(authorize(administrator, "camera.share", "tenant-a").allowed)
        self.assertFalse(authorize_camera(viewer, "camera.view", "tenant-a").allowed)
        self.assertTrue(authorize_camera(viewer, "camera.view", "tenant-a", {"can_view_live": 1}).allowed)

    def test_navigation_never_crosses_identity_domains(self):
        customer = navigation_keys({"identity_domain": "customer", "customer_role": "customer_admin"})
        platform = navigation_keys({"identity_domain": "platform", "platform_role": "sales"})
        self.assertIn("live", customer)
        self.assertNotIn("admin-portal", customer)
        self.assertIn("tenant-camera-sharing", customer)
        self.assertNotIn("users", customer)
        self.assertIn("new-customer", platform)
        self.assertNotIn("live", platform)


if __name__ == "__main__":
    unittest.main()
