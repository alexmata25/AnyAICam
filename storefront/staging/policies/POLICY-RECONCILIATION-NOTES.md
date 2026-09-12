# Policy reconciliation notes — staging vs. live

**Status: internal working notes, not customer-facing. Prepared during Phase 8 staging work. The live page named below was NOT edited.**

## The conflict

`shipping-returns.html` is currently **live and published** on the production Bluehost site (linked from the footer of every page, e.g. `index.html`), titled "Shipping, Returns & Refunds," dated effective **July 18, 2026**. It was written for AnyAiCam's older adapter/camera product line and does not reflect the new Ryzen-appliance hardware fulfillment model at all. Specifically, it conflicts with the new staging drafts (`shipping-policy.html`, `hardware-return-refund-policy.html`, `cancellation-policy.html`) in three ways:

| Topic | Live `shipping-returns.html` (old) | New staging drafts (this pass) |
|---|---|---|
| Shipping/preparation timeframe | "no more than 7 calendar days" | "up to 14 days for preparation and shipment" (appliances require software install/config/testing first — the old page describes no such step) |
| Return window / restocking fee | 30-day return window, **no restocking fee** for unopened/unused hardware in original packaging | 30-day return window (same number, different basis), **15% restocking fee** on an eligible return, waived only for hardware defective due to AnyAiCam or damaged on arrival |
| Fulfillment source | Names **"Videoloft or another authorized fulfillment provider"** explicitly | Deliberately never names a supplier — this task's own instruction was to keep the fulfillment source internal |

The two policies describe genuinely different products and processes (drop-shipped, unconfigured adapter/camera hardware vs. an appliance AnyAiCam personally sources, installs software on, configures, and tests before shipping) — so a customer reading the live page today would get **materially wrong expectations** if they ordered a Ryzen appliance instead of a camera/adapter.

## What this session did NOT do

- Did not edit `shipping-returns.html` (the live page) in any way.
- Did not publish any of the new staging drafts.
- Did not pick which of the reconciliation options below is correct — that's a business decision.

## Decision needed

Before any of the new staging drafts can be published, Alejandro needs to decide **how the two policies coexist**:

1. **Replace** `shipping-returns.html` entirely with the new Ryzen-appliance policy, if AnyAiCam no longer sells the old adapter/camera line the way that page describes it, or
2. **Keep both, scoped separately** — the existing page continues to cover adapters/cameras (fulfilled via Videoloft/third party), and a new, separate set of pages (the staging drafts here) covers Ryzen appliances specifically (fulfilled/prepared by AnyAiCam directly) — in which case both pages need to clearly tell a customer which one applies to their specific order, and the site's footer/checkout flows need to link the correct policy for each product type.

Either way, legal review of the new drafts (restocking fee, return window, defective/damaged exception, and the other flagged clauses inside each draft page) should happen before anything is published, and the live page should be updated deliberately as part of that same release rather than left silently out of sync.
