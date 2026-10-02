"""AWS-authoritative onboarding rework, Phase 2: make the existing Partner
Wizard A + Customer Wizard B the primary onboarding flow, labeled as one
7-step lifecycle (steps 1-5 partner-run, 6-7 customer-run -- see
app/partner_workspace.py and the session's Phase 2 report for the chosen
handoff model). Both wizards keep their own existing UI/pagination
unchanged; this phase only adds outer step labeling, wires the Phase 1
provisioning backend all the way through, and retires the thin
/customer-onboarding checklist's fake "Cameras" item and its own nav entry.

Same FastAPI-not-installed constraint as the rest of this suite (see
test_provisioning_service.py's module docstring) -- partner_workspace.py
and main.py are read as source text, not imported.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARTNER_WORKSPACE_SOURCE = (ROOT / "app" / "partner_workspace.py").read_text(encoding="utf-8")
MAIN_SOURCE = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
BUSINESS_PORTAL_SOURCE = (ROOT / "app" / "business_portal.py").read_text(encoding="utf-8")


class WizardAStepProgressionTests(unittest.TestCase):
    """Steps 1-5 of 7 -- Wizard A is unchanged internally (still 5 tabs,
    still autosaves via /api/partner/onboarding/drafts); this phase only
    adds outer "Step N of 7" labeling and the deployment-mode field the
    provisioning contract requires."""

    def test_all_five_internal_tabs_still_exist(self):
        for label in ("1 Customer", "2 Sites", "3 What you&#x27;s".replace("&#x27;s", "'re buying"),
                      "4 Pricing", "5 Review &amp; send to AWS"):
            self.assertIn(label, PARTNER_WORKSPACE_SOURCE)

    def test_outer_step_badge_present_and_updates_per_tab(self):
        self.assertIn('id="onboarding-outer-step"', PARTNER_WORKSPACE_SOURCE)
        self.assertIn("data-outer-steps=\"1,2,3,3,3\"", PARTNER_WORKSPACE_SOURCE)
        self.assertIn("document.getElementById('onboarding-outer-step').innerHTML", PARTNER_WORKSPACE_SOURCE)

    def test_deployment_mode_field_exists_and_is_submitted(self):
        self.assertIn('id="w-deployment"', PARTNER_WORKSPACE_SOURCE)
        for option in ('value="local"', 'value="hybrid"', 'value="cloud"'):
            self.assertIn(option, PARTNER_WORKSPACE_SOURCE)
        self.assertIn("deployment_mode:document.getElementById('w-deployment').value", PARTNER_WORKSPACE_SOURCE)

    def test_autosave_draft_endpoints_unchanged(self):
        self.assertIn("@app.post('/api/partner/onboarding/drafts')", PARTNER_WORKSPACE_SOURCE)
        self.assertIn("@app.get('/api/partner/onboarding/drafts/{draft_id}')", PARTNER_WORKSPACE_SOURCE)
        self.assertIn("fetch('/api/partner/onboarding/drafts'", PARTNER_WORKSPACE_SOURCE)


class WizardBStepProgressionTests(unittest.TestCase):
    """Steps 6-7 of 7 -- Wizard B's own 7 internal steps (Welcome, Add
    appliance, Status, Discover, Cameras, Review, Confirm) are preserved
    exactly as before; only an outer badge is added on top."""

    def test_all_seven_internal_steps_still_exist(self):
        for step_number in range(1, 8):
            self.assertIn(f'data-step="{step_number}"', PARTNER_WORKSPACE_SOURCE)

    def test_outer_step_badge_present_and_maps_to_6_or_7(self):
        self.assertIn('id="customer-setup-outer-step"', PARTNER_WORKSPACE_SOURCE)
        self.assertIn("setupStep<=3?6:7", PARTNER_WORKSPACE_SOURCE)

    def test_qr_scan_link_button_and_status_rendering_all_present(self):
        for marker in ("customer-qr-file", "BarcodeDetector", "link-customer-appliance",
                       "start-camera-scan", "save-camera-setup", "confirm-customer-setup"):
            self.assertIn(marker, PARTNER_WORKSPACE_SOURCE)

    def test_progress_autosave_endpoint_unchanged(self):
        self.assertIn("@app.post('/api/customer/setup/progress')", PARTNER_WORKSPACE_SOURCE)
        self.assertIn("fetch('/api/customer/setup/progress'", PARTNER_WORKSPACE_SOURCE)

    def test_confirm_endpoint_always_returns_a_redirect_on_success(self):
        # This is the exact pattern ("location.href=r.redirect" fed from a
        # field that could be missing) that can produce a /undefined
        # redirect -- confirm the 200 response always sets it.
        confirm_start = PARTNER_WORKSPACE_SOURCE.index("def confirm_customer_setup")
        confirm_body = PARTNER_WORKSPACE_SOURCE[confirm_start:confirm_start + 1600]
        self.assertIn("'redirect':", confirm_body)
        self.assertIn("location.href=r.redirect", PARTNER_WORKSPACE_SOURCE)


class RoleAccessBoundaryTests(unittest.TestCase):
    def test_onboard_customer_requires_partner_access_and_permissions(self):
        # _dual_mode_identity() (partner_workspace.py) replaced the direct
        # require_partner_access(request) call here -- it still enforces
        # the exact same role check for a direct Partner Portal session
        # (require_partner_access()'s own contract, unchanged), and
        # additionally accepts a legacy Admin Portal session with a live,
        # already-linked admin_partner_links bridge (see
        # admin_partner_bridge.py) so an authorized administrator can
        # reuse this same onboarding workflow without a second manual
        # Partner Portal login. Neither the permission checks below nor
        # require_partner_access() itself were weakened or bypassed.
        start = PARTNER_WORKSPACE_SOURCE.index("def onboard_customer")
        body = PARTNER_WORKSPACE_SOURCE[start:start + 400]
        self.assertIn("_dual_mode_identity(request)", body)
        self.assertIn("require_permission(identity,'customer.create')", body)

    def test_customer_setup_page_requires_customer_owner_role(self):
        start = PARTNER_WORKSPACE_SOURCE.index("def customer_first_setup")
        body = PARTNER_WORKSPACE_SOURCE[start:start + 400]
        self.assertIn("if identity.get('role')!='customer_owner': raise HTTPException(status_code=403", body)

    def test_link_endpoint_requires_appliance_self_link_permission(self):
        start = PARTNER_WORKSPACE_SOURCE.index("def link_customer_appliance")
        body = PARTNER_WORKSPACE_SOURCE[start:start + 400]
        self.assertIn("require_permission(identity,'appliance.self.link')", body)


class RetiredChecklistTests(unittest.TestCase):
    """/customer-onboarding's self-service checklist is no longer the
    primary path; its fake, self-reported "Cameras" completion item is
    retired from both places it appeared."""

    def test_cameras_removed_from_onboarding_steps(self):
        start = MAIN_SOURCE.index("ONBOARDING_STEPS = [")
        end = MAIN_SOURCE.index("\n]", start)
        block = MAIN_SOURCE[start:end]
        self.assertNotIn('"cameras"', block)
        for step in ("organization", "site", "deployment", "cloud_recording", "team", "terms"):
            self.assertIn(f'"{step}"', block)

    def test_cameras_removed_from_admin_activation_checklist(self):
        self.assertNotIn('("Cameras", "cameras" in completed),', MAIN_SOURCE)

    def test_customer_onboarding_removed_from_sidebar(self):
        start = MAIN_SOURCE.index("NAV_ITEMS = [")
        end = MAIN_SOURCE.index("\n]", start)
        block = MAIN_SOURCE[start:end]
        self.assertNotIn('"customer-onboarding"', block)

    def test_customer_onboarding_route_still_exists_for_direct_access(self):
        # Retired from nav/primary path, not deleted -- a direct link or
        # bookmark should not 404.
        self.assertIn('@app.get("/customer-onboarding"', MAIN_SOURCE)


class CloudAdaptersViewTests(unittest.TestCase):
    """The Partner Portal's own "Cloud Adapters" tab used to read
    account_management.json (business_portal.py's separate Appliance
    model), which the real onboarding path never wrote to -- so a
    Wizard-A-created customer's appliance never showed up there."""

    def test_cloud_adapters_reads_the_sql_appliances_table(self):
        self.assertNotIn("appliances=account.get('appliances',[])", PARTNER_WORKSPACE_SOURCE)
        self.assertIn(
            "appliances=rows('SELECT a.* FROM appliances a JOIN customers c ON c.id=a.customer_id WHERE c.partner_id=? ORDER BY a.created_at DESC',(partner_id,))",
            PARTNER_WORKSPACE_SOURCE,
        )

    def test_business_portal_legacy_appliance_model_still_exists_untouched(self):
        # Not retired this phase -- see the session report for why (it is
        # live, just disconnected from the primary flow) and what a real
        # retirement would require.
        self.assertIn("class Appliance(BaseModel):", BUSINESS_PORTAL_SOURCE)
        self.assertIn("cloud_id: str = Field(default_factory=lambda: 'AIC-' + uuid4().hex[:8].upper())", BUSINESS_PORTAL_SOURCE)


class NoDuplicateIdentityInActiveFlowTests(unittest.TestCase):
    """The active onboarding path (Wizard A -> Wizard B) must mint Cloud
    ID/activation token exactly one way: through the Phase 1 provisioning
    backend. business_portal.py's separate generator is untouched but is
    not reachable from this path (see test_customer_experience-adjacent
    business_portal tests for its own, unrelated /setup path)."""

    def test_active_flow_never_calls_secrets_token_hex_for_cloud_id(self):
        onboard_start = PARTNER_WORKSPACE_SOURCE.index("def onboard_customer")
        onboard_end = PARTNER_WORKSPACE_SOURCE.index("@app.post('/api/partner/appliances/{appliance_id}/activation-token')")
        onboard_body = PARTNER_WORKSPACE_SOURCE[onboard_start:onboard_end]
        self.assertNotIn("'AIC-'", onboard_body)
        self.assertNotIn("token_urlsafe(24)", onboard_body)
        self.assertIn("get_provisioning_backend().provision(order,idempotency_key=", onboard_body)


if __name__ == "__main__":
    unittest.main()
