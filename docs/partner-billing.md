# Partner billing, upcharges, activation fees, residuals and payouts

Owner rules, 2026-10-04. Code: `app/partner_agreements.py`, `app/partner_payouts.py`,
`app/sales_commissions.py`, `app/partner_billing_routes.py`. Tests:
`app/tests/test_partner_agreements_billing.py`.

## Rules

| Rule | Value |
|---|---|
| Company-paid activation commission | None. The old $40 / $60 / $90 / $125 was removed. Rows already written stay as history. |
| Partner activation/setup fee | Set by the partner, approved by AnyAiCam, belongs to the partner. |
| Partner monthly upcharge | Added to the AnyAiCam base price and approved by AnyAiCam. Customer price = base + upcharge. All three are stored separately. |
| Billing party | `ANYAICAM_BILLED` or `PARTNER_BILLED`, per agreement. |
| Residual | 20% of the AnyAiCam portion actually collected (plan and add-on lines; never the upcharge, the fee or tax), for the customer's first 12 paid months. |
| Partner-billed residual | 20% of the base amount AnyAiCam actually receives from the partner, for 12 paid months. |
| Payouts | Calendar quarters (Jan–Mar, Apr–Jun, Jul–Sep, Oct–Dec), one statement per payee. |
| Reversal after a payout | Carries forward as a negative balance into the next statement. |

## Data

- **`partner_agreements`** (new): the customer, partner and salesperson; the plan, tier and catalog base price; the upcharge, customer price and activation fee; the billing party; the residual rate and month limit; the residual start and end dates; status and the linked Stripe subscription. A change is a new version (`previous_agreement_id`) and never an edit. Statuses are `pending_approval`, `approved`, `active`, `superseded`, `rejected`, `cancelled` and `ended`.
- **`commission_ledger`** (existing, extended): every amount between AnyAiCam and the partner, one row per payment:
  - `recurring`: the 20% residual;
  - `partner_upcharge` and `partner_activation_fee`: the partner's money, collected by AnyAiCam;
  - `partner_base_due`: a negative amount, the base the partner owes for a month it billed;
  - `partner_base_remitted`: base received from the partner;
  - `hardware`: unchanged.

  New columns: `agreement_id`, `billing_party`, `commission_rate_bps`, `collected_cents`, `collected_at`, `service_month`, `reverses_entry_id`, `created_by` and `note`.
- **Reversals:** refunds, disputes, bounced remittances and partner refunds are new negative rows (status `reversal`, `reverses_entry_id` set). An original row's amount never changes.
- **`partner_payouts`** and **`partner_payout_items`** (new): the quarterly statement (opening balance, period total, payable, carried forward, status, payment reference). Each items row attaches exactly one ledger entry to exactly one statement.
- **Audit:** proposals, approvals, rejections, cancellations, collections, remittances, reversals, statements and payments are written to `audit_logs`, with the money terms before and after.

The migration `20261004_partner_agreements_payouts` is additive. To roll it back, drop the three new tables. Older code ignores the new ledger columns.

## How money moves

**AnyAiCam bills the customer.** Camera-plan checkout, `create_camera_slot_checkout`, builds the session as follows:

1. Line 0 is the catalog Stripe Price, verified against the catalog as before.
2. Line 1 is the partner upcharge, an inline `price_data` monthly line.
3. Line 2 is the activation fee, an inline one-time line on the first invoice only.

Every amount comes from the approved agreement and nothing from the browser. No Stripe Price object is created for each partner markup.

When an `invoice.paid` webhook arrives, its lines are split into the AnyAiCam portion (the 20% basis), the upcharge (the partner's) and the fee (the partner's).

Checkout is refused in these cases:
- the customer chooses a different plan from the agreed one;
- the catalog price has changed since the agreement;
- the customer is partner-billed;
- a Friends & Family approval coincides with partner charges.

**The partner bills the customer.**
1. The partner records each month it billed. The partner now owes AnyAiCam that month's base price.
2. A global administrator records the money received from the partner, with a unique payment reference. That clears the amount owed and earns the 20% residual on what was received.
3. An administrator can reverse a refunded month or a bounced remittance, together with its residual.

**Payouts.** After a quarter ends, a global administrator issues the statements on Quarterly payouts. Each takes every unattached entry recorded before the quarter's end. Then:

- payable = max(0, carried + period total);
- carried forward = min(0, carried + period total).

Statements are marked paid with a payment reference.

## Pages

- **`/partner-agreements`:** the list and the new or changed agreement form. It shows the catalog base price, upcharge, activation fee, live final price, billing party and residual terms.
- **`/partner-agreements/{id}`:** the price breakdown, residual months, accrued amounts, next payout, payment-level detail and version history. Administrators also see approve, reject, cancel, remittance and reversal actions.
- **`/partner-payouts`:** the statements with their payment-level detail. Administrators can issue a quarter and mark statements paid.

Partners see only their own customers. Salespeople see only agreements where they earn the residual. Global administrators see everything.
