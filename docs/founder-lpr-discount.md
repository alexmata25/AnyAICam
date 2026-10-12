# Founder LPR testing discount

The Founder testing discount is server-authorized and initially applies only
to the recurring `vehicle_intelligence` add-on (LPR). The customer browser
cannot supply or override the authorized account list, Stripe coupon, SKU,
discount percentage, or duration. Eligibility is keyed by AnyAiCam customer
account ID, not email address.

## Required server configuration

Configure these values in the same Stripe mode used by the cloud checkout:

| Variable | Required value |
|---|---|
| `ANYAICAM_FOUNDER_LPR_CUSTOMER_IDS` | Comma-separated exact customer account IDs authorized by the owner. Leave unset for no eligible accounts. |
| `ANYAICAM_STRIPE_COUPON_FOUNDER_LPR` | ID of a pre-created Stripe coupon in the configured Stripe mode. It must be valid, 100% off, have no fixed-amount discount, and apply only to the Stripe Product behind the configured Vehicle Intelligence Price. |
| `ANYAICAM_FOUNDER_LPR_COUPON_DURATION` | Explicit Stripe coupon duration: `once`, `repeating`, or `forever`. There is no default. |
| `ANYAICAM_FOUNDER_LPR_COUPON_DURATION_MONTHS` | Positive month count required only for `repeating`; unset for `once` or `forever`. |

The application does not create or modify Stripe coupons. At checkout it
retrieves the configured Price and Coupon from Stripe and fails closed unless
the coupon's percentage, validity, duration, and exact Product restriction
match configuration. The server attaches the coupon to the existing
subscription Checkout Session and disables customer-entered promotion codes.

## Eligibility and fulfillment

Only an authenticated `customer_owner` session can start analytics checkout.
An allowlisted account receives Founder pricing only for
`vehicle_intelligence`; other accounts and add-ons continue through the
existing Friends & Family or standard path. Founder and Friends & Family
discounts do not stack. Existing Friends & Family percentages and approval
workflow are unchanged; a pending Friends & Family request still holds that
account's checkout.

Stripe's signed webhook remains the source of fulfillment. The existing
analytics webhook verifies current subscription state, account association,
reversals, and payment status, and accepts Stripe's `no_payment_required`
state for a legitimate 100%-discounted checkout. It creates or updates the
normal analytics subscription and add-on records; it does not grant LPR merely
because an account is on the allowlist. The customer then assigns LPR to the
intended camera through the existing camera analytics workflow.

## Deployment sequence (owner approval required)

1. Create the coupon in the intended Stripe mode with the exact product scope
   and explicitly approved duration.
2. Configure the four server variables for the approved customer account IDs.
3. Deploy this cloud checkout/webhook source and restart only the approved
   cloud application service as its normal release procedure requires.
4. Verify an ineligible test account receives no Founder coupon and an
   authorized test account sees the intended $0 amount in Stripe Checkout.
5. Complete one authorized test checkout and verify webhook-created
   `vehicle_intelligence`/`lpr` subscription state and the assigned camera
   entitlement before beginning physical LPR validation.

No appliance installer or camera configuration change is required for the
discount itself. LPR model/runtime availability is a separate appliance
readiness gate.
