# Commercial backend restructure -- Local one-time / Hybrid+addons recurring (2026-09-21)

## Correction, same day (2026-09-21)

Item 4 below ("Confirm the Advanced Analytics bundling model") is now resolved: **Advanced Analytics is ONE recurring, customer-billed add-on** -- a single Stripe Price -- that grants smart_motion, people_counting, lpr, and ppe together, not four separately purchasable add-ons. Face Access (facial_recognition) is unaffected and remains its own separate recurring add-on. AACO remains unsellable, also unaffected.

This changed `analytics_entitlements.ANALYTICS_CATALOG` from one row per `analytic_key` to one row per purchasable `addon_key`, where each row carries a tuple of the one or more internal `analytic_key` feature flags it grants (`advanced_analytics` is the one row with four; every other row still grants exactly its own single analytic_key, unchanged). `resolve_analytic()` was renamed `resolve_addon()` to return `{"addon_key", "label", "analytic_keys"}`; `POST /api/customer/analytics/checkout` now takes `addon_key` instead of `analytic_key`; the webhook sync functions fan a single Stripe event out into one `analytics_subscriptions` row per granted `analytic_key`, all updated/cancelled together. `ADDON_CATEGORIES` (the presentation-only grouping mentioned in item 4's original text) was removed since the bundling is now the catalog's actual structure, not a layer on top of it. Env var `ANYAICAM_STRIPE_PRICE_ADVANCED_ANALYTICS` replaces the four separate `_SMART_MOTION`/`_PEOPLE_COUNTING`/`_LPR`/`_PPE` price env vars -- none of which were ever set in any real environment, so this is a pre-launch correction, not a migration of live data. Tests and this doc's items 3 and 8 below are updated to match.

## What was confirmed and built

Product structure, confirmed 2026-09-21:

| Product | Billing | Status after tonight |
|---|---|---|
| Local (camera-slot capacity) | **One-time purchase** | Checkout mode fixed to `payment`; dollar amounts are placeholders (see below) |
| Hybrid (camera-slot capacity) | Recurring subscription | Unchanged -- already correct |
| Advanced Analytics (Smart Motion, People Counting, LPR, PPE bundled as ONE add-on) | Recurring add-on | New checkout endpoint built; no price configured yet |
| Face Access (Facial Recognition) | Recurring add-on | New checkout endpoint built; no price configured yet |
| AACO | Future add-on | Deliberately not built -- see below |

### Code changes

- `app/customer_entitlements.py`: `PLAN_TIERS` now carries a `billing_type` field per tier (`"one_time"` for all four Local tiers, `"recurring"` for all four Hybrid tiers). `resolve_tier()`, `_plan_tier_rows()`, and `pricing_table_for_website()` all now expose it.
- `app/main.py`, `create_camera_slot_checkout()` (`POST /api/customer/camera-slots/checkout`): Stripe Checkout `mode` is now derived from the tier's `billing_type` -- `payment` for Local, `subscription` for Hybrid -- instead of being hardcoded to `subscription` for both. `subscription_data[...]` metadata fields (which Stripe rejects outright in `payment` mode) are now only sent for recurring tiers.
- `app/main.py`, new `POST /api/customer/analytics/checkout` (`create_analytics_addon_checkout()`): this endpoint did not exist before tonight. `analytics_entitlements.py` already had a fully working, tested webhook side (it grants a recurring `analytics_subscriptions` row from a Stripe event) but nothing ever *created* a Checkout Session against one of its prices -- the exact same gap `create_camera_slot_checkout()` closed for camera slots in an earlier phase. One `analytic_key` per session, `mode="subscription"` always (every entry in that catalog is recurring).
- `app/analytics_entitlements.py`: added `ADDON_CATEGORIES` (a presentation-only grouping: `advanced_analytics` -> the 4 existing analytics keys, `face_access` -> `facial_recognition`) and `aaco_product_status()` (returns `{"sellable": False, ...}`, no catalog entry, no price env var, no checkout/webhook wiring -- a future website page can call this to render "coming soon" honestly instead of guessing).
- Tests: 39 new/updated tests across `test_camera_slot_checkout.py`, `test_local_hybrid_pricing_tiers.py`, `test_analytics_addon_checkout.py` (new), `test_addon_category_grouping.py` (new). All pass. No live Stripe price was created, modified, or charged -- every test monkeypatches `stripe_api_post`.

Nothing was deployed to Ryzen or staging tonight. This is pure backend/checkout-logic code; every price env var involved is still unset in every real environment, so deploying it changes no live behavior. I'd rather you review the pricing decisions below first, since several of them require replacing already-referenced (if unset) env vars.

## Decisions I need from you

### 1. Blocking: 4 new one-time Stripe Price objects must be created for Local

`test_local_hybrid_pricing_tiers.py` already documents 8 real, live-mode Stripe Price IDs that exist in your Stripe account today (not yet deployed -- they live in `ecs-task-definition.production.json`'s environment block). The 4 Local ones were created as **recurring-monthly** Price objects under the old model. A Stripe Price's recurring/one-time nature is fixed permanently at creation and cannot be edited afterward. Now that Local checkout uses `mode="payment"`, those 4 existing Local Price IDs cannot be used at all -- Stripe will reject the Checkout Session if one is set as `ANYAICAM_STRIPE_PRICE_LOCAL_*`.

**You need to:** create 4 new one-time Price objects in Stripe for Local (1-8, 9-16, 17-32, 33-64), once the dollar amounts below are decided, and point the same 4 env vars at the new Price IDs. The 4 Hybrid Price IDs are unaffected and remain valid.

### 2. Local one-time dollar amounts are not real prices yet

`PLAN_TIERS`' four Local figures ($14.99 / $19.99 / $29.99 / $49.99) are carried over unchanged from the old monthly-subscription model. A fair one-time price and a fair monthly price aren't the same number by any simple conversion -- I did not invent a one-time figure, since that's a pricing decision, not an engineering one. These four numbers currently only exist so the (not-yet-connected) website pricing table has a non-empty placeholder.

**You need to:** decide the actual one-time price for each of the 4 Local tiers.

### 3. Advanced Analytics / Face Access have no prices configured at all

Every env var in `analytics_entitlements.ANALYTICS_CATALOG` (`ANYAICAM_STRIPE_PRICE_ADVANCED_ANALYTICS`, `_FACIAL_RECOGNITION`) is unset in every environment -- these add-ons are not purchasable yet, by design (fail-closed).

Separately, `pricing_config.py` (the self-serve marketing quote calculator) already shows different placeholder monthly prices for the 4 individual analytics that now make up the Advanced Analytics bundle ($1.79 / $10.99 / $18.99 / $17.99) for its own quote-estimate purpose. I did not touch or reconcile that file tonight -- it's a different, older subsystem (a quote estimator, not a checkout path) and reconciling the two wasn't in tonight's scope.

**You need to:** decide the real Stripe price for the bundled Advanced Analytics add-on and for Facial Recognition, and decide whether `pricing_config.py`'s existing per-analytic placeholder figures should inform that one bundled price or be treated as stale/unrelated.

### 4. Advanced Analytics bundling model -- resolved (see correction above)

Confirmed 2026-09-21 (same day, before this ever shipped): Advanced Analytics is ONE bundled SKU with one Stripe Price covering Smart Motion, People Counting, LPR, and PPE together, granting all four `analytics_subscriptions` rows from one webhook event. This superseded the original implementation below this line, which treated the four as separately priced, separately purchasable add-ons -- see the "Correction, same day" section at the top of this document for what changed in code and tests.

### 5. AACO scope, price, and Face Access dependency

Per your instruction ("AACO = future premium add-on"), I built no catalog entry, no price env var, and no checkout/webhook code for it -- only a `aaco_product_status()` stub that reports "not sellable yet" for a future website page to call honestly. This matches the standing note from an earlier phase that AACO's pricing is not finalized.

**You need to:** decide AACO's scope (is it a software add-on like the others, or does it require the AAC hardware/relay bundle it's been discussed alongside?), its relationship to Face Access (does a customer need Face Access first?), and its price, before any code gets built for it.

### 6. Refund/cancellation gap for one-time Local purchases (a real gap, not new tonight)

Hybrid and the analytics add-ons revoke entitlement automatically when Stripe reports `customer.subscription.deleted` (cancellation). A one-time Local purchase has no subscription at all, so there is no equivalent automatic revocation path today if a Local purchase is ever refunded -- no `charge.refunded` handler exists anywhere in this webhook pipeline for any product line, one-time or recurring.

**You need to:** decide whether a refunded Local purchase should automatically revoke camera-slot entitlement (requiring a new `charge.refunded`/`payment_intent`-based handler) or whether that stays a manual admin action for now. I did not build this tonight since it's a new decision, not part of "make Local one-time."

### 7. Website pricing page still shows the old structure

Per the standing instruction to preserve the existing working website unless a change is explicitly required, I did not touch any live pricing page copy tonight. `customer_entitlements.pricing_table_for_website()` now correctly reports `billing_type` per tier, but nothing on the actual website calls it yet -- it was already unconnected before tonight.

**You need to:** decide when/how the live pricing page should be updated to actually show "one-time" for Local vs "/month" for Hybrid and the add-ons (today it isn't wired to any real price data at all, per the earlier website audit).

### 8. Optional: multi-item analytics checkout

`POST /api/customer/analytics/checkout` accepts exactly one `addon_key` per Checkout Session, matching the existing webhook's single-price-id-metadata design -- `addon_key="advanced_analytics"` already covers the 4-analytic-key bundle case via the same-day correction above, so this item now only concerns a customer wanting to buy, e.g., Advanced Analytics AND Face Access in one cart/session. That would require extending the webhook to read multiple line items from the session object (not present in a webhook payload by default -- would need `line_items` expansion) -- a larger, separate change not made yet to avoid touching already-tested webhook logic without your go-ahead.
