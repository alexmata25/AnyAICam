"""Cloud ID handoff verification (pre-Phase-3-Samsung-deployment check).

Confirms, at the source level (app/appliance_cloud.py imports FastAPI and
cannot be imported in this dev environment -- see
test_provisioning_service.py's module docstring for the same constraint),
that the appliance-agent's Cloud ID, the Wizard-A provisioning-backend-
issued Cloud ID, the customer/site/appliance SQL association, and camera
scan jobs all resolve to exactly one appliances row -- not four
independent identities that could drift apart.

No mismatch bug was found; this file documents and locks in the design
that prevents one:

1. onboard_customer() (partner_workspace.py, Phase 1) is the only place
   that INSERTs into `appliances` -- one row, with cloud_id from
   get_provisioning_backend().provision() and a matching row in
   appliance_activation_tokens.
2. /api/appliance/activate (appliance_cloud.py) requires the exact
   cloud_id AND a token that verifies against that same appliance_id's
   appliance_activation_tokens row -- an unknown cloud_id or a token that
   doesn't match is rejected (403), never silently accepted or used to
   create a second appliance identity. Only on success does it mint the
   appliance's one permanent credential (appliance_credentials), tied to
   that same appliance_id, and return the same cloud_id/customer_id/
   site_id back to the caller.
3. authenticate_appliance() (used by heartbeat, scan-jobs, and everything
   else the agent calls afterward) verifies the bearer credential against
   appliance_credentials scoped to the X-Appliance-ID header's row -- the
   only rows that step 2 ever created.
4. secure_scan_jobs()/secure_scan_results() additionally re-check the
   URL's cloud_id against the authenticated appliance's own cloud_id
   before touching camera_scan_jobs -- belt-and-suspenders on top of (3).

There is exactly one appliances row per physical appliance, and every one
of these four references it by the same id -- structurally, not by
convention.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPLIANCE_CLOUD_SOURCE = (ROOT / "app" / "appliance_cloud.py").read_text(encoding="utf-8")
PARTNER_WORKSPACE_SOURCE = (ROOT / "app" / "partner_workspace.py").read_text(encoding="utf-8")


class OnboardingCreatesExactlyOneApplianceIdentityTests(unittest.TestCase):
    def test_onboard_customer_is_the_only_appliances_insert_in_partner_workspace(self):
        self.assertEqual(PARTNER_WORKSPACE_SOURCE.count("INSERT INTO appliances("), 1)

    def test_cloud_id_and_activation_token_both_come_from_the_provisioning_backend(self):
        onboard_start = PARTNER_WORKSPACE_SOURCE.index("def onboard_customer")
        onboard_end = PARTNER_WORKSPACE_SOURCE.index("@app.post('/api/partner/appliances/{appliance_id}/activation-token')")
        body = PARTNER_WORKSPACE_SOURCE[onboard_start:onboard_end]
        self.assertIn("provisioning=get_provisioning_backend().provision(order,idempotency_key=", body)
        self.assertIn("cloud_id=provisioning['cloud_id']; activation=provisioning['activation_token']", body)
        # the SAME cloud_id/activation feed both the appliances row and its
        # activation-token row -- not two independently generated values
        self.assertIn("INSERT INTO appliances(id,customer_id,site_id,cloud_id,", body)
        self.assertIn("INSERT INTO appliance_activation_tokens(id,appliance_id,token_hash,", body)


class ActivationRequiresExactMatchTests(unittest.TestCase):
    def test_activate_appliance_looks_up_the_row_by_the_submitted_cloud_id(self):
        start = APPLIANCE_CLOUD_SOURCE.index("def activate_appliance")
        body = APPLIANCE_CLOUD_SOURCE[start:start + 800]
        self.assertIn("appliance=row('SELECT * FROM appliances WHERE cloud_id=?',(cloud_id,))", body)
        self.assertIn("if not appliance or not token: raise HTTPException(status_code=403", body)

    def test_activation_token_must_verify_against_that_specific_appliance_id(self):
        start = APPLIANCE_CLOUD_SOURCE.index("def activate_appliance")
        body = APPLIANCE_CLOUD_SOURCE[start:start + 1600]
        self.assertIn("SELECT * FROM appliance_activation_tokens WHERE appliance_id=?", body)
        self.assertIn("verify_password(token,candidate['token_hash'])", body)
        self.assertIn("if not match: raise HTTPException(status_code=403", body)

    def test_successful_activation_returns_the_same_cloud_id_customer_id_and_site_id(self):
        start = APPLIANCE_CLOUD_SOURCE.index("def activate_appliance")
        body = APPLIANCE_CLOUD_SOURCE[start:start + 2500]
        self.assertIn("'cloud_id':cloud_id,", body)
        self.assertIn("'customer_id':appliance['customer_id'],'site_id':appliance['site_id']", body)

    def test_activation_token_is_single_use(self):
        start = APPLIANCE_CLOUD_SOURCE.index("def activate_appliance")
        body = APPLIANCE_CLOUD_SOURCE[start:start + 1600]
        self.assertIn("SET used_at=? WHERE id=? AND used_at IS NULL", body)
        self.assertIn("if changed!=1: raise HTTPException(status_code=409", body)


class ScanJobsAuthenticateAgainstTheSameApplianceIdentityTests(unittest.TestCase):
    def test_authenticate_appliance_scopes_credentials_to_the_headers_appliance_id(self):
        start = APPLIANCE_CLOUD_SOURCE.index("def authenticate_appliance")
        body = APPLIANCE_CLOUD_SOURCE[start:start + 1500]
        self.assertIn("SELECT * FROM appliance_credentials WHERE appliance_id=? AND revoked_at IS NULL", body)
        self.assertIn("if not matched: raise HTTPException(status_code=403", body)

    def test_scan_job_routes_cross_check_the_url_cloud_id_against_the_authenticated_appliance(self):
        for fn_name in ("secure_scan_jobs", "secure_scan_results"):
            start = APPLIANCE_CLOUD_SOURCE.index(f"def {fn_name}")
            body = APPLIANCE_CLOUD_SOURCE[start:start + 400]
            self.assertIn("appliance=authenticate_appliance(request)", body)
            self.assertIn("if appliance['cloud_id'].upper()!=cloud_id.upper(): raise HTTPException(status_code=403", body)

    def test_appliance_credentials_are_only_ever_created_by_activation(self):
        # If some other route also inserted into appliance_credentials, an
        # appliance could end up with a credential the activate_appliance
        # flow never validated -- confirm activate_appliance is the only
        # writer.
        self.assertEqual(APPLIANCE_CLOUD_SOURCE.count("INSERT INTO appliance_credentials("), 1)


class OnvifDiscoveryNotOnTheActivePathTests(unittest.TestCase):
    """app/onvif_discovery.py (written during Phase 3 investigation, kept
    as real/tested code but not wired in -- see the Phase 3 report) must
    stay unreferenced so there is exactly one discovery implementation
    actually running: the appliance-agent's real ONVIF WS-Discovery via
    the camera_scan_jobs pipeline."""

    def test_onvif_discovery_module_is_not_imported_by_any_active_app_file(self):
        import re
        for path in sorted((ROOT / "app").glob("*.py")):
            if path.name in ("onvif_discovery.py",) or "before" in path.name or "backup" in path.name:
                continue
            source = path.read_text(encoding="utf-8")
            self.assertNotRegex(source, r"\bonvif_discovery\b", f"{path.name} references onvif_discovery -- it must stay unwired")


if __name__ == "__main__":
    unittest.main()
