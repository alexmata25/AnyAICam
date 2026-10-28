# Windows VMS 1.3.4.1 MFA Security Correction — Independent Review Handoff

Date: 2026-10-28
Branch: `codex/windows-1341-mfa-security`
Baseline: pristine `2b839577a4b80b8575bc26b1dc85f22605286896`
Scope: owner-approved focused MFA, authorization, tenant-scope, and recovery correction only.

## Change summary

- Every partner administrator protected request revalidates the exact local account identity, verified MFA state, role, and current grant. Global-only operations require a live global grant; partner administrators remain bound to their exact partner grant and cannot call global-only APIs.
- Delegated administrator assertions fail closed for protected administrator operations because this implementation has no trusted identity-provider MFA assertion. Matching email or local user ID cannot bind a local MFA factor to a delegated identity.
- Partner administrator password sign-in starts MFA enrollment when needed and yields an enrollment-only session until MFA is verified. Verification rechecks live authorization.
- Added an audited global-admin-initiated MFA reset for an eligible partner administrator. The requester needs verified MFA and a current global grant; target must be an active partner-scoped admin with matching grant. Reset invalidates prior sessions, spends pending challenges, removes old factors/recovery codes, and allows limited re-enrollment without elevating roles.
- Partner customer reads/writes and installation-record writes use existing permissions and partner/customer ownership validation. No permission was added.
- Test Lab and quote-delivery routes use existing authorization helpers. The edge emergency `/login` exception remains unchanged and does not open global-only APIs.
- No recording behavior or production data was changed.

## Route authorization coverage

`tests/test_admin_only_routes_mfa_windows_1341.py` enumerates 24 global-only route cases. Tests exercise every case on edge and cloud profiles and assert:

- Missing MFA, enrollment-only, and re-enrollment sessions are refused without state mutation.
- Verified partner-scoped administrators are refused all 24 routes without state mutation.
- A live global grant revoked externally is refused on every route even when session authorization version has not changed.
- A delegated identity cannot reuse a local factor by matching email or user ID.

A valid global administrator is exercised on protected operations; the endpoint suite asserts that a live global grant is needed at each protected request. Positive paths are additionally covered in the MFA enforcement and recovery tests.

## Security and behavior tests

- MFA enrollment-only sessions cannot access protected APIs; successful enrollment and second-step verification grant only the account's existing scope.
- Global-grant revocation, session revocation, MFA reset audit records, pending challenge invalidation, recovery codes, and no-privilege-elevation are covered.
- Partner/customer ownership mismatch and cross-tenant writes are denied; technician writes without `customer.edit` are denied.
- Delegated administrator access is denied without authoritative MFA. Existing delegated technician behavior is preserved.
- The documented edge emergency sign-in remains available while global-only APIs remain denied.
- Event-only recording and offline playback suites passed; no recording code changed.

## Test results

Focused matrix: **297 collected; 282 passed, 15 failed, 0 skipped, 0 errors** (790 warnings; run time 669.81 seconds).

Pristine baseline comparison:

- The remaining 15 failing cases are all in the unchanged legacy files `tests/test_admin_portal_signin_pass.py` (10) and `tests/test_admin_customer_management.py` (5).
- The pristine baseline had 16 failures across the affected legacy suites. The 15 remaining failures match those inherited sessionless-cookie, outdated MFA/login expectation, and legacy customer-admin fixture cases. The additional baseline stray-identity expectation was corrected to seed a valid grant and now passes.
- No newly introduced failures were identified in the approved security scope.

Event-only coverage included in the same matrix: 14 local recording-mode tests, 16 cloud recording-mode tests, and 6 event-clip playback union tests; all 36 passed.

`git diff --check`: passed.

## Changed source and tests

- `app/analytics_control_center.py`
- `app/cloud_features.py`
- `app/main.py`
- `app/partner_portal.py`
- `app/platform_owner.py`
- `app/tests/test_admin_mfa_enforcement_cloud.py`
- `app/tests/test_admin_mfa_enrollment_recovery.py`
- `app/tests/test_admin_only_routes_mfa_windows_1341.py`
- `app/tests/test_cloud_administrator_bridge.py`
- `app/tests/test_portal_login_delegated_auth.py`
- `app/tests/test_windows_1341_partner_installation_authorization.py` (new)

## Remaining risks and boundaries

- The repository still has 15 inherited legacy test failures requiring their separate fixture/expectation workstream; they were not changed here.
- Delegated administrators remain unable to use protected admin operations until a trustworthy IdP MFA assertion is supported. This is intentional fail-closed behavior.
- The full test matrix is application-level; it does not establish Windows service, installer, signing, or runtime acceptance.
- This correction is prepared for independent source review only. It has not been merged, transferred, built, signed, installed, deployed, or published.
