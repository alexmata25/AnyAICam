# Storefront staging E2E checklist (prepared 2026-10-04, not yet run)

Scope: `store-staging.anyaicam.com` (Stripe **test** mode, account
`acct_1U87e9GllhK80H2n`) → Stripe Checkout → the staging portal webhook
(`/stripe/webhook`) → portal records. Production, Bluehost, Samsung and Ryzen
are out of scope. No real money is charged; use Stripe test cards only.

The source gate is the branch `fix/storefront-authoritative-prices-20261004`:
every storefront Price ID equals the one the staging portal recognizes
(`deploy/staging-stripe-test-prices.json`), and the server re-reads each Price
from Stripe before checkout and refuses it unless it is active, USD, at the
published amount, and one-time or monthly as expected.

## 0. Preconditions (needs explicit deploy authorization)

- [ ] Owner authorized deploying this storefront commit to staging.
- [ ] Storefront image built from a clean export (`git -c core.autocrlf=false archive`),
      not from the Windows working tree.
- [ ] Current storefront container kept as rollback (`deploy-storefront:dce063f`).
- [ ] After deploy, these return **404**: `/staging/tests/test-checkout-catalog.php`,
      `/staging/tests/integration-test-checkout-session.php`,
      `/staging/policies/POLICY-RECONCILIATION-NOTES.md`.
- [ ] `/staging/stripe-checkout.php` returns **410** to any POST.
- [ ] The storefront container's `STRIPE_SECRET_KEY` is a **test** key in the
      same Stripe account as the portal (report mode + same-account yes/no only;
      never print the key).
- [ ] The staging portal's Stripe webhook endpoint is enabled for
      `checkout.session.completed` and `invoice.paid` in test mode.
- [ ] Note the portal database path read-only (from the running container's
      configuration) and take a pre-test backup with the usual
      `staging-pre-…` naming.

## 1. Page checks (browser, desktop 1366px and mobile 390px)

- [ ] Build page shows Starter $1,249.99, Professional $1,749.99,
      Enterprise $2,249.99, Numato relay $149.99.
- [ ] Local $14.99 / $24.99 / $39.99 / $69.99; Hybrid $24.99 / $39.99 / $69.99 / $99.99.
- [ ] VMS licence 8/16/32/64 at $49.99 / $79.99 / $129.99 / $199.99.
- [ ] Add-ons: AI Essentials $7.99, AI Professional $14.99, Vehicle
      Intelligence $14.99, Advanced Analytics $24.99, Talk Down $4.99.
- [ ] No cameras, PoE switches or adapters are offered for checkout.
- [ ] No "staging"/"sandbox" wording; the test-mode banner is shown.
- [ ] No horizontal scroll at 390px; policy pages fit the phone width.
- [ ] Header logo stays on the build page (no unstyled page).

## 2. Purchases (one fresh buyer email per journey)

Use card `4242 4242 4242 4242`, any future expiry, any CVC. Record every
Checkout Session ID (`cs_test_…`) from the Stripe Dashboard or the success URL.

| # | Journey | Selection | Expect in Stripe |
|---|---------|-----------|------------------|
| A | Appliance buyer | Starter + Local 1–8 + AI Essentials + relay | 4 sessions in sequence: Starter $1,249.99 one-time, Local $14.99/mo, AI Essentials $7.99/mo, relay $149.99 one-time |
| B | Appliance buyer | Enterprise + Hybrid 17–32 + Vehicle Intelligence + Talk Down | Enterprise $2,249.99, Hybrid $69.99/mo, $14.99/mo, $4.99/mo |
| C | DIY (own Linux PC) | No appliance + Local 9–16 + VMS licence 16 | Local $24.99/mo, licence $79.99 one-time |
| D | DIY | No appliance + Hybrid 33–64 + VMS licence 64 + AI Professional + Advanced Analytics | $99.99/mo, $199.99 one-time, $14.99/mo, $24.99/mo |
| E | Professional appliance | Professional + Local 33–64 | $1,749.99 one-time, $69.99/mo |
| F | Remaining licences | VMS licence 8, then 32 (DIY, Local 1–8 / Local 17–32) | $49.99 and $129.99 one-time |

For each session confirm in Stripe test mode: amount, currency **USD**,
mode (payment vs subscription), and the line item Price ID equals
`deploy/staging-stripe-test-prices.json`. Metadata must include
`anyaicam_stripe_price_id` (and `anyaicam_hardware_sku` for hardware,
`anyaicam_product_class=vms_license` + capacity for licences).

## 3. Safety checks during the run

- [ ] Double-click "Proceed" once: Stripe shows **one** session and **one**
      customer for that journey (Idempotency-Key per journey).
- [ ] Abandon a journey at the Stripe page and press Back: no portal records
      for that session.
- [ ] Decline card `4000 0000 0000 0002`: no portal records.
- [ ] Reload `checkout-continue.html` after the last leg: no extra session.

## 4. Portal recognition (read-only)

On the staging host, against the portal database (read-only, `mode=ro`):

```
python3 tools/storefront_e2e_verify.py --db <portal.db> \
  --session cs_test_A1 --session cs_test_A2 ... --email <journey email>
```

Expected (`RESULT: PASS`, exit 0):

- Hardware sessions → `hardware_orders` (registered buyer) or
  `pending_hardware_order_links` (not yet registered) with the SKU and the
  published `amount_cents`.
- Local/Hybrid and VMS licence sessions → `customer_entitlements` or
  `pending_customer_links` with the matching product and camera capacity.
- Add-on sessions → `addon_subscriptions` or `pending_analytics_links`.
- `RESULT: FAIL` / `MISSING:` lines mean a paid session the portal did not
  record: stop and investigate the webhook delivery before continuing.

## 5. Customer-side completion (browser)

- [ ] Register at the portal with each journey's email: pending links resolve
      to the new account (re-run step 4 and expect resolved rows).
- [ ] My Subscription shows the plan, add-ons and, for C/D/F, the VMS licence.
- [ ] Hardware orders appear for A/B/E.
- [ ] Customer email(s) for the purchase arrive.

## 6. Clean-up and sign-off

- [ ] Cancel the test subscriptions in Stripe test mode (or leave them and note it).
- [ ] Record session IDs, results and screenshots in the hand-off notes.
- [ ] Gate lines: `STOREFRONT E2E: PASSED/BLOCKED`, `DEPLOYMENT PERFORMED: …`.
