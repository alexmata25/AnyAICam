from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
PARTNER_DB = (ROOT / "app" / "partner_db.py").read_text(encoding="utf-8")
TENANT_ROUTES = (ROOT / "app" / "tenancy" / "routes.py").read_text(encoding="utf-8")
CUSTOMER_EXPERIENCE = (ROOT / "app" / "customer_experience" / "routes.py").read_text(encoding="utf-8")


class MultiTenantIntegrationTests(unittest.TestCase):
    def test_tenant_migration_runs_after_existing_database_migrations(self):
        existing = PARTNER_DB.index("apply_migrations()")
        tenancy = PARTNER_DB.index("apply_tenant_migration()")
        self.assertLess(existing, tenancy)

    def test_main_login_accepts_database_identity_without_replacing_csrf(self):
        self.assertIn("database_user, _reason = authenticate_detailed(normalized_email, password)", MAIN)
        self.assertIn('create_session(user["id"], remember_me == "true", identity_source)', MAIN)
        self.assertIn("'X-CSRF-Token':token", MAIN)

    def test_tenant_routes_are_registered_outside_main(self):
        self.assertIn("from tenancy.routes import register_tenant_routes", MAIN)
        self.assertIn("tenant_onboarding = register_tenant_routes(", MAIN)
        self.assertNotIn("def onboard_tenant(", MAIN)

    def test_customer_experience_is_registered_as_a_module(self):
        self.assertIn("from customer_experience import register_customer_experience_routes", MAIN)
        self.assertIn("register_customer_experience_routes(", MAIN)
        for route in ("/customer-portal", "/customer-admin/users", "/customer-admin/sites", "/customer-admin/cameras", "/customer-admin/permissions"):
            self.assertIn(route, CUSTOMER_EXPERIENCE)

    def test_tenant_onboarding_uses_configured_invitation_email_service(self):
        self.assertIn('get_email_service().send(', TENANT_ROUTES)
        self.assertIn('"invitation"', TENANT_ROUTES)
        self.assertIn('invitation["delivery_status"]', TENANT_ROUTES)

    def test_domain_boundary_is_enforced_in_authentication_middleware(self):
        self.assertIn("allowed, reason = identity_route_allowed(user, path)", MAIN)
        self.assertIn("PLATFORM_ONLY_PATH_PREFIXES", MAIN)
        self.assertIn("CUSTOMER_CAMERA_PATH_PREFIXES", MAIN)

    def test_customer_camera_sharing_uses_tenant_scoped_routes(self):
        self.assertIn('@app.get("/tenant/camera-sharing"', TENANT_ROUTES)
        self.assertIn('@app.put("/api/tenants/{tenant_id}/users/{user_id}/cameras/{camera_id}"', TENANT_ROUTES)
        self.assertIn("User does not belong to this tenant.", (ROOT / "app" / "tenancy" / "service.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
