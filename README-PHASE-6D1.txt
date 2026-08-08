ANY AI CAM — PHASE 6D.1
CLOUD ID ASSIGNMENT AND VERIFICATION

WHAT THIS PHASE ADDS
- New Cloud ID tab in the customer portal.
- Customer can enter the Cloud ID from:
  * Windows software bridge
  * Hardware cloud adapter
- Cloud ID is attached to the logged-in customer account.
- Duplicate Cloud IDs are blocked.
- Every new or changed Cloud ID becomes Pending.
- Master administrator can verify or reject Cloud IDs.
- Customer sees status:
  * Not submitted
  * Pending verification
  * Verified and connected
  * Rejected
- Cloud ID is also included in Customer Onboarding.
- Every submission and review is written to onboarding activity and audit logs.

INSTALL
1. Back up your current main.py.
2. Replace it with this main.py.
3. Rebuild and restart Docker.
4. Customer test:
   - Sign in as an approved customer.
   - Open /cloud-id.
   - Select Software Bridge or Hardware Adapter.
   - Enter a Cloud ID and submit.
5. Master administrator test:
   - Sign in as the master admin.
   - Open /cloud-id.
   - Verify or reject the submitted Cloud ID.
6. Sign back in as the customer and confirm the status changed.

IMPORTANT
- A Cloud ID does not activate billing by itself.
- A verified Cloud ID identifies which bridge or adapter belongs to the customer.
- Stripe entitlements, camera assignments, analytics, and notifications remain separate controls.
