# Cloud Gateway paid-binding verification: isolated test strategy

Status: PLANNED. Prepared by Claude, 2026-11-05, for Codex and owner
review. Nothing here has been run against Stripe, AWS or a VM.

## Why this is needed

A successful paid Cloud Gateway upload has never been demonstrated.
Billing is still disabled in code:
- `CLOUD_GATEWAY_BILLING_SOURCE_READY = False`
  (`cloud_gateway_camera_entitlements.py`);
- `WEBHOOK_PRODUCER_READY = False`
  (`cloud_gateway_subscription_evidence.py`);
- `REFRESH_WORKER_READY = False`, `PAID_ACTIVATION_READY = False` and
  related gates.

While the billing constant is `False`:
- `produce_paid_binding` returns `unavailable`;
- `verify_paid_binding` returns `unavailable`;
- every Cloud Gateway upload is refused.

Today's route tests (`test_gateway_media_credential_authorization.py`)
prove the guard and catalog logic, but they patch two things:
- the billing constant, to `True`;
- `_current_verified_cloud_subscription`, which returns a synthetic
  record.

The real evidence path has therefore not been exercised end to end:
- the webhook-written evidence row;
- its single matching `stripe-webhook` audit row;
- the 300-second freshness window;
- the hold table;
- the seat count against real evidence.

## Levels

### Level 1: offline route tests (COMPLETED)

These exist now. With the billing constant and evidence store patched,
they cover:
- edition;
- binding;
- the camera mapping;
- the seat count;
- operational retention readiness;
- capture-time retention attribution;
- refusal before STS.

### Level 2: offline, real evidence reader (PLANNED, no approval needed)

A new test module, still SQLite and no network. Only
`CLOUD_GATEWAY_BILLING_SOURCE_READY` is patched; the evidence reader is
the real one.
1. Seed the inputs:
   - a `cloud_gateway_subscription_evidence` row through the real
     reducer entry point (no hand-written row);
   - its `cloud_gateway.subscription_evidence_updated` audit row.
2. Bind with `produce_paid_binding`. Then request credentials, catalog a
   clip and run a sweep tick with an injected `now`.
3. Negative cases. Each must fail closed before STS:
   - stale evidence (more than 300 s);
   - a hold row;
   - two matching audit rows, or none;
   - the wrong price, amount, currency or interval;
   - `payment_verified=0`;
   - a reversal;
   - a period end in the past;
   - the seat count exceeded;
   - a camera number or device key mismatch;
   - another customer's subscription.
4. Retention:
   - the clip carries `cloud_gateway`, 14 days and the binding id;
   - after a refund or binding revocation, no new upload is accepted;
   - the stored clip still expires on its frozen 14 days.

This is the next offline step. It proves the paid-binding path everywhere
except the Stripe and AWS boundaries.

### Level 3: isolated staging (PLANNED, needs owner approval)

Nothing here may touch production billing, production AWS or customer
data.

- **Stripe:**
  - test mode only, with a separate test-mode API key held by the owner;
  - a test `cloud_gateway` price ($9.99, monthly, USD);
  - webhooks forwarded to the isolated host only (Stripe CLI
    `listen --forward-to`), with a test webhook secret.
- **AWS:**
  - a separate test bucket;
  - upload, read and delete-only lifecycle roles scoped to that bucket;
  - never the production bucket or roles.
- **Host:**
  - an isolated cloud instance (or local container) on the candidate
    commit;
  - a fresh database;
  - `ANYAICAM_RUNTIME_ROLE=cloud`;
  - the sweep enabled with the test lifecycle role.
- **Gateway:** a disposable acceptance VM
  (`New-GatewayAcceptanceVM.ps1`, internal switch only), running an
  unsigned test build, setup `1.4.0.9x`.
- **Billing constants:**
  - turned on only on a test branch that is never merged or shipped;
  - the branch name and commit are recorded in the evidence;
  - customer builds keep them `False` until Codex and the owner clear
    activation.

Evidence to record (hashes and timestamps; no secrets):
1. `--diagnostics` reports operational ready (deletion probe and tick).
2. Test checkout completes:
   - the webhook is received;
   - the evidence row and its single audit row are written;
   - the binding is produced.
3. Credential request: STS is called with the narrow event-media policy.
4. The clip is uploaded and cataloged with `retention_policy=cloud_gateway`,
   14 days and the binding id.
5. Change retention to 7 days: the stored clip keeps 14 and a new clip
   gets 7. With an injected `now`, the sweep deletes each clip only after
   its own period. The S3 object is gone, and the catalog row is removed
   after that.
6. Refund or cancel in test mode: `verify_paid_binding` fails, new
   uploads are refused, and stored clips still expire on their frozen
   days.
7. Local Gateway on the same VM: no credentials are ever issued.
8. Teardown:
   - delete the test bucket objects;
   - cancel the test subscriptions;
   - destroy the VM.

## Owner approvals needed for Level 3

- Stripe test-mode account, key custody and the webhook endpoint.
- Test AWS bucket and roles (account and region).
- VM creation, with the clean-disk and isolation evidence in
  `gateway-windows-release-readiness.md` section 4.
- The test branch that turns the billing constants on, and its scope.
