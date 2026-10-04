"""Partner Portal pages and API for customer agreements and quarterly
payouts (2026-10-04). Business rules live in partner_agreements.py and
partner_payouts.py; this module only authenticates, renders and maps errors.

Partners (partner owner, salesperson) see and propose their own customers'
agreements; a global administrator sees every agreement, approves partner
proposals, records partner remittances and reversals, and issues payouts.
Every amount shown comes from the server; the browser's final-price figure
is a preview, and the server recomputes everything from the catalog."""
from __future__ import annotations

import json
from html import escape
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

import partner_agreements as pa
import partner_banking as pb
import partner_payouts as pp
import pricing_catalog
from partner_db import connection, rows, tenant_owns_partner

PARTNER_ROLES = {"administrator", "partner_owner", "salesperson"}
KIND_LABELS = {"recurring": "20% residual", "partner_upcharge": "Partner monthly upcharge",
               "partner_activation_fee": "Partner activation / setup fee", "partner_base_due": "AnyAiCam base owed by partner",
               "partner_base_remitted": "Base paid to AnyAiCam", "partner_processing_fee": "Stripe processing fee on your charges",
               "hardware": "Hardware commission (discontinued)",
               "activation": "Activation commission (discontinued)"}
STATUS_LABELS = {"pending_approval": "Waiting for AnyAiCam approval", "approved": "Approved", "active": "Active",
                 "superseded": "Replaced by a newer version", "rejected": "Rejected", "cancelled": "Cancelled", "ended": "Ended",
                 "issued": "Issued", "nothing_payable": "Nothing payable", "paid": "Paid"}
BILLING_LABELS = {pricing_catalog.BILLING_ANYAICAM: "AnyAiCam bills the customer", pricing_catalog.BILLING_PARTNER: "Partner bills the customer"}
CSRF_JS = ("function csrf(){const c=document.cookie.split('; ').find(x=>x.startsWith('anyaicam_csrf='));return c?decodeURIComponent(c.substring(14)):'';}"
           "async function send(url,body){const r=await fetch(url,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json',"
           "'X-CSRF-Token':csrf()},body:JSON.stringify(body||{})});let p={};try{p=await r.json();}catch(e){}"
           "if(!r.ok)throw new Error(p.detail||'Something went wrong. Try again.');return p;}"
           "function act(btn,url,body,status){btn.disabled=true;status.textContent='Saving…';send(url,body).then(()=>location.reload())"
           ".catch(e=>{status.textContent=e.message;btn.disabled=false;});}"
           "document.querySelectorAll('[data-local-time]').forEach(el=>{const d=new Date(el.dataset.localTime);if(!isNaN(d))el.textContent=d.toLocaleString();});")


def money(cents) -> str:
    cents = int(cents or 0)
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"


def _when(value) -> str:
    return f'<span data-local-time="{escape(str(value), quote=True)}">{escape(str(value)[:10])}</span>' if value else "—"


def _identity(request: Request) -> dict:
    from partner_portal import partner_identity
    identity = partner_identity(request)
    if not identity or identity.get("role") not in PARTNER_ROLES:
        raise HTTPException(status_code=403, detail="Partner authorization required.")
    return identity


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except pa.AgreementForbidden as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (pa.AgreementError, pp.PayoutError, pb.BankingError) as error:
        raise HTTPException(status_code=error.status, detail=str(error)) from error


def _customers_for(identity: dict) -> list[dict]:
    if pa.is_global_admin(identity):
        return rows("SELECT id,name,partner_id FROM customers ORDER BY name")
    if not identity.get("partner_id"):
        return []
    return rows("SELECT id,name,partner_id FROM customers WHERE partner_id=? ORDER BY name", (identity["partner_id"],))


def _names(customer_ids) -> dict:
    ids = sorted(set(customer_ids))
    if not ids:
        return {}
    return {c["id"]: c["name"] for c in rows(f"SELECT id,name FROM customers WHERE id IN ({','.join('?' for _ in ids)})", tuple(ids))}


def _plan_label(agreement: dict) -> str:
    plan = pricing_catalog.find_base_plan(agreement["plan_type"], agreement["tier_label"])
    return plan["label"] if plan else f"{agreement['plan_type']} {agreement['tier_label']}"


def _reverse_button(entry: dict) -> str:
    """Partner-billed records an administrator can undo (refund, bounced payment)."""
    if entry.get("reverses_entry_id") or entry["kind"] not in (pa.BASE_DUE, pa.BASE_REMITTED) or entry["status"] != "earned":
        return ""
    return f'<button class="ghost-button" data-reverse="{escape(entry["id"], quote=True)}">Reverse</button>'


def _paid_button(statement: dict) -> str:
    if statement["status"] != pp.ISSUED:
        return ""
    return f'<button class="ghost-button" data-paid="{escape(statement["id"], quote=True)}">Mark paid</button>'


def _residual_terms() -> str:
    return (f"<p class=\"health-detail\">Residual: {pricing_catalog.RECURRING_COMMISSION_PERCENT}% of the AnyAiCam plan and add-on "
            f"amounts actually collected, for the customer's first {pricing_catalog.RECURRING_COMMISSION_MAX_PAID_MONTHS} paid months. "
            "Your upcharge and activation fee are yours and earn no residual. Partner-billed customers: the residual is on the base "
            "price AnyAiCam receives from you. Earnings are paid every calendar quarter; refunds and chargebacks reduce the next payout.</p>")


def register_partner_billing_routes(app: FastAPI, shell: Callable) -> None:

    # ---------------------------------------------------------------- API
    @app.get("/api/partner/agreement-terms")
    def agreement_terms_api(request: Request, plan_type: str, tier_label: str, upcharge_cents: int = 0, activation_fee_cents: int = 0):
        _identity(request)
        return _call(pa.terms, plan_type, tier_label, upcharge_cents, activation_fee_cents)

    @app.get("/api/partner/agreements")
    def agreements_api(request: Request):
        return {"agreements": pa.visible(_identity(request))}

    @app.post("/api/partner/agreements")
    def propose_api(request: Request, payload: dict):
        identity = _identity(request)
        return _call(pa.propose, identity, customer_id=payload.get("customer_id"), plan_type=payload.get("plan_type"),
                     tier_label=payload.get("tier_label"), upcharge_cents=payload.get("upcharge_cents", 0),
                     activation_fee_cents=payload.get("activation_fee_cents", 0), billing_party=payload.get("billing_party"),
                     salesperson=payload.get("salesperson"))

    @app.get("/api/partner/agreements/{agreement_id}")
    def agreement_api(request: Request, agreement_id: str):
        agreement = _call(pa.get, _identity(request), agreement_id)
        return {"agreement": agreement, "financials": pa.financials(agreement)}

    @app.post("/api/admin/partner-agreements/{agreement_id}/approve")
    def approve_api(request: Request, agreement_id: str):
        return _call(pa.approve, _identity(request), agreement_id)

    @app.post("/api/admin/partner-agreements/{agreement_id}/reject")
    def reject_api(request: Request, agreement_id: str, payload: dict):
        return _call(pa.reject, _identity(request), agreement_id, payload.get("reason", ""))

    @app.post("/api/admin/partner-agreements/{agreement_id}/cancel")
    def cancel_api(request: Request, agreement_id: str, payload: dict):
        return _call(pa.cancel, _identity(request), agreement_id, payload.get("reason", ""))

    @app.post("/api/partner/agreements/{agreement_id}/collections")
    def collection_api(request: Request, agreement_id: str, payload: dict):
        return _call(pa.record_collection, _identity(request), agreement_id, service_month=payload.get("service_month", ""))

    @app.post("/api/admin/partner-agreements/{agreement_id}/activate-service")
    def activate_service_api(request: Request, agreement_id: str):
        return _call(pa.activate_service, _identity(request), agreement_id)

    @app.post("/api/admin/partner-agreements/{agreement_id}/suspend-service")
    def suspend_service_api(request: Request, agreement_id: str, payload: dict):
        return _call(pa.suspend_service, _identity(request), agreement_id, payload.get("reason", ""))

    @app.get("/api/partner/payout-account")
    def payout_account_api(request: Request):
        payee = _call(pb.payee_of, _identity(request))
        return pb.public_view(pb.account(payee["payee_key"]), payee["payee_key"])

    @app.post("/api/partner/payout-account/onboarding")
    def payout_onboarding_api(request: Request):
        from main import PUBLIC_BASE_URL
        if not PUBLIC_BASE_URL:
            raise HTTPException(status_code=503, detail="ANYAICAM_PUBLIC_URL is required for bank account setup.")
        url = _call(pb.start_onboarding, _identity(request), return_url=f"{PUBLIC_BASE_URL}/partner-payouts/banking/return",
                    refresh_url=f"{PUBLIC_BASE_URL}/partner-payouts?banking=expired")
        return {"url": url}

    @app.post("/api/partner/payout-account/refresh")
    def payout_refresh_api(request: Request, payload: dict | None = None):
        return _call(pb.refresh, _identity(request), (payload or {}).get("payee_key"))

    @app.get("/partner-payouts/banking/return")
    def payout_banking_return(request: Request):
        identity = _page_identity(request)
        if not identity:
            return RedirectResponse("/partner-login", status_code=303)
        try:
            pb.refresh(identity)
        except (pb.BankingError, pa.AgreementForbidden):
            return RedirectResponse("/partner-payouts?banking=check-later", status_code=303)
        return RedirectResponse("/partner-payouts?banking=updated", status_code=303)

    @app.get("/partner-payouts/{payout_id}/statement.csv")
    def statement_csv(request: Request, payout_id: str):
        identity = _page_identity(request)
        if not identity:
            return RedirectResponse("/partner-login", status_code=303)
        filename, text = _call(pp.statement_csv, identity, payout_id)
        return Response(text, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})

    @app.post("/api/admin/partner-agreements/{agreement_id}/remittances")
    def remittance_api(request: Request, agreement_id: str, payload: dict):
        return _call(pa.record_remittance, _identity(request), agreement_id, service_month=payload.get("service_month", ""),
                     amount_cents=payload.get("amount_cents"), reference=payload.get("reference", ""))

    @app.post("/api/admin/commission-ledger/{entry_id}/reverse")
    def reverse_api(request: Request, entry_id: str, payload: dict):
        return {"reversals": _call(pa.reverse_partner_entry, _identity(request), entry_id, reason=payload.get("reason", ""))}

    @app.get("/api/partner/payouts")
    def payouts_api(request: Request):
        identity = _identity(request)
        return {"payouts": pp.statements(identity)}

    @app.get("/api/partner/payouts/{payout_id}")
    def payout_api(request: Request, payout_id: str):
        return _call(pp.statement, _identity(request), payout_id)

    @app.post("/api/admin/partner-payouts/close")
    def close_api(request: Request, payload: dict):
        return {"issued": _call(pp.close_quarter, _identity(request), payload.get("quarter", ""))}

    @app.post("/api/admin/partner-payouts/{payout_id}/paid")
    def paid_api(request: Request, payout_id: str, payload: dict):
        return _call(pp.mark_paid, _identity(request), payout_id, reference=payload.get("reference", ""))

    # ---------------------------------------------------------------- pages
    def _page_identity(request: Request):
        from partner_portal import partner_identity
        identity = partner_identity(request)
        if not identity:
            return None
        if identity.get("role") not in PARTNER_ROLES:
            raise HTTPException(status_code=403, detail="Partner authorization required.")
        return identity

    @app.get("/partner-agreements", response_class=HTMLResponse)
    def agreements_page(request: Request):
        identity = _page_identity(request)
        if not identity:
            return RedirectResponse("/partner-login", status_code=303)
        admin = pa.is_global_admin(identity)
        agreements = pa.visible(identity)
        names = _names(a["customer_id"] for a in agreements)
        table = "".join(
            f"<tr><td><a href=\"/partner-agreements/{escape(a['id'])}\">{escape(names.get(a['customer_id']) or 'Customer')}</a></td>"
            f"<td>{escape(_plan_label(a))}</td><td>{money(a['base_price_cents'])}</td><td>{money(a['upcharge_cents'])}</td>"
            f"<td><strong>{money(a['customer_price_cents'])}</strong>/month</td><td>{money(a['activation_fee_cents'])}</td>"
            f"<td>{escape(BILLING_LABELS.get(a['billing_party'], a['billing_party']))}</td>"
            f"<td>{escape(STATUS_LABELS.get(a['status'], a['status']))} · v{int(a['version'])}</td></tr>"
            for a in agreements
        ) or '<tr><td colspan="8">No customer agreements yet.</td></tr>'
        customers = _customers_for(identity)
        customer_options = "".join(f'<option value="{escape(c["id"], quote=True)}">{escape(c["name"])}</option>' for c in customers)
        plans = pricing_catalog.base_plans()
        plan_options = "".join(
            f'<option value="{escape(p["plan_type"])}|{escape(p["tier_label"])}" data-base="{int(p["monthly_cents"])}">'
            f'{escape(p["label"])} — {money(p["monthly_cents"])}/month</option>' for p in plans)
        salesperson_field = ""
        if admin:
            sellers = rows("SELECT id,email,name,partner_id FROM partner_users WHERE role IN ('salesperson','partner_owner') ORDER BY name")
            salesperson_field = ('<label>Salesperson who earns the residual<select id="ag-seller"><option value="">Use the customer\'s salesperson</option>'
                                 + "".join(f'<option value="{escape(s["id"], quote=True)}">{escape(s["name"] or s["email"])}</option>' for s in sellers)
                                 + "</select></label>")
        form = ("" if not customers else
                f'''<form class="panel rule-form" id="ag-form"><h2>New or changed agreement</h2>
<label>Customer<select id="ag-customer" required>{customer_options}</select></label>
<label>AnyAiCam plan<select id="ag-plan" required>{plan_options}</select></label>
<p>AnyAiCam base price: <strong id="ag-base">—</strong>/month (set by AnyAiCam)</p>
<label>Your monthly upcharge ($)<input id="ag-upcharge" type="number" min="0" step="0.01" value="0" required></label>
<label>Your activation / setup fee ($, one time)<input id="ag-fee" type="number" min="0" step="0.01" value="0" required></label>
<p>Customer pays: <strong id="ag-final">—</strong>/month</p>
<fieldset><legend>Who bills the customer</legend>
<label style="display:flex;gap:.6rem;align-items:flex-start;margin:.4rem 0"><input type="radio" style="width:auto;min-height:0;height:auto;margin:.25rem 0 0;flex:0 0 auto" name="ag-billing" value="{pricing_catalog.BILLING_ANYAICAM}" checked> <span>AnyAiCam bills the customer the final price and pays you your upcharge and fee</span></label>
<label style="display:flex;gap:.6rem;align-items:flex-start;margin:.4rem 0"><input type="radio" style="width:auto;min-height:0;height:auto;margin:.25rem 0 0;flex:0 0 auto" name="ag-billing" value="{pricing_catalog.BILLING_PARTNER}"> <span>We bill the customer and pay AnyAiCam the base price</span></label></fieldset>
{salesperson_field}{_residual_terms()}
<p class="health-detail">{"Saved agreements take effect at once." if admin else "AnyAiCam approves the upcharge and fee before they apply."}</p>
<button class="action-button" id="ag-save">Save agreement</button> <span id="ag-status" role="status"></span></form>''')
        content = (f'<header class="topbar"><div><p class="eyebrow">Confidential · partner billing</p><h1>Customer agreements</h1></div>'
                   f'<a class="ghost-button" href="/partner-payouts">Quarterly payouts</a></header>{form}'
                   '<section class="panel" style="overflow:auto;margin-top:14px"><h2>Agreements</h2><table class="data-table"><thead><tr>'
                   '<th>Customer</th><th>Plan</th><th>AnyAiCam base</th><th>Upcharge</th><th>Customer price</th><th>Activation fee</th>'
                   f'<th>Billing</th><th>Status</th></tr></thead><tbody>{table}</tbody></table></section>')
        scripts = ("<script>" + CSRF_JS + """
const form=document.getElementById('ag-form');
if(form){const plan=document.getElementById('ag-plan'),up=document.getElementById('ag-upcharge'),fee=document.getElementById('ag-fee');
const fmt=c=>'$'+(c/100).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const cents=v=>Math.round(Number(v||0)*100);
function preview(){const base=Number(plan.selectedOptions[0].dataset.base);document.getElementById('ag-base').textContent=fmt(base);
document.getElementById('ag-final').textContent=fmt(base+Math.max(0,cents(up.value)));}
[plan,up].forEach(el=>el.addEventListener('input',preview));preview();
form.addEventListener('submit',e=>{e.preventDefault();const [plan_type,tier_label]=plan.value.split('|');const seller=document.getElementById('ag-seller');
const body={customer_id:document.getElementById('ag-customer').value,plan_type,tier_label,upcharge_cents:cents(up.value),
activation_fee_cents:cents(fee.value),billing_party:document.querySelector('input[name=ag-billing]:checked').value,salesperson:seller?seller.value:null};
act(document.getElementById('ag-save'),'/api/partner/agreements',body,document.getElementById('ag-status'));});}
</script>""")
        return shell("Customer agreements", "partner-agreements", content, scripts)

    @app.get("/partner-agreements/{agreement_id}", response_class=HTMLResponse)
    def agreement_page(request: Request, agreement_id: str):
        identity = _page_identity(request)
        if not identity:
            return RedirectResponse("/partner-login", status_code=303)
        agreement = _call(pa.get, identity, agreement_id)
        admin = pa.is_global_admin(identity)
        fin = pa.financials(agreement)
        name = _names([agreement["customer_id"]]).get(agreement["customer_id"]) or "Customer"
        a = agreement
        history = rows("SELECT id,version,status,base_price_cents,upcharge_cents,customer_price_cents,activation_fee_cents,billing_party,created_at "
                       "FROM partner_agreements WHERE customer_id=? ORDER BY version DESC", (a["customer_id"],))
        history_rows = "".join(
            f"<tr><td><a href=\"/partner-agreements/{escape(h['id'])}\">v{int(h['version'])}</a></td><td>{money(h['base_price_cents'])}</td>"
            f"<td>{money(h['upcharge_cents'])}</td><td>{money(h['customer_price_cents'])}</td><td>{money(h['activation_fee_cents'])}</td>"
            f"<td>{escape(BILLING_LABELS.get(h['billing_party'], h['billing_party']))}</td><td>{escape(STATUS_LABELS.get(h['status'], h['status']))}</td>"
            f"<td>{_when(h['created_at'])}</td></tr>" for h in history)
        partner_billed = a["billing_party"] == pricing_catalog.BILLING_PARTNER
        entries = "".join(
            f"<tr><td>{_when(e['earned_at'])}</td><td>{escape(KIND_LABELS.get(e['kind'], e['kind']))}{' (reversal)' if e.get('reverses_entry_id') else ''}</td>"
            f"<td>{escape(e.get('service_month') or ('month ' + str(e['paid_month_index']) if e.get('paid_month_index') else '—'))}</td>"
            f"<td>{money(e['collected_cents']) if e.get('collected_cents') is not None else '—'}</td><td>{money(e['amount_cents'])}</td>"
            f"<td>{'On a statement' if e.get('payout_id') else 'Accrued'}</td>"
            + (f"<td>{_reverse_button(e)}</td>" if admin else "")
            + "</tr>"
            for e in fin["entries"]) or f'<tr><td colspan="{7 if admin else 6}">Nothing collected yet.</td></tr>'
        by_kind = "".join(f"<li>{escape(KIND_LABELS.get(k, k))}: <strong>{money(v)}</strong></li>" for k, v in fin["by_kind_cents"].items()) or "<li>Nothing yet.</li>"
        nxt = fin["next_payout"]
        actions = []
        if admin and a["status"] == pa.PENDING:
            actions.append('<button class="action-button" id="approve">Approve</button> <button class="ghost-button" id="reject">Reject</button>')
        if admin and a["status"] in (pa.PENDING, *pa.IN_FORCE):
            actions.append('<button class="ghost-button" id="cancel">Cancel agreement</button>')
        partner_forms = ""
        if partner_billed:
            if a.get("service_activated_at") and not a.get("service_suspended_at"):
                service = f"Active since {_when(a['service_activated_at'])}"
            elif a.get("service_suspended_at"):
                service = f"Suspended {_when(a['service_suspended_at'])}"
            else:
                service = "Not active yet: AnyAiCam activates service after receiving your base payment."
            buttons = ""
            if admin and a["status"] in pa.IN_FORCE:
                buttons = ('<button class="action-button" id="activate">Activate service</button> '
                           if not a.get("service_activated_at") or a.get("service_suspended_at") else
                           '<button class="ghost-button" id="suspend">Suspend service</button>')
            partner_forms += (f'<section class="panel" style="margin-top:14px"><h2>Service</h2><p>{service}</p>{buttons}'
                              '<span id="svc-status" role="status"></span></section>')
        if partner_billed and a["status"] in pa.IN_FORCE:
            partner_forms += ('<form class="panel rule-form" id="collect"><h2>Record a month you billed</h2>'
                              f'<p class="health-detail">You owe AnyAiCam {money(a["base_price_cents"])} for each month you bill this customer.</p>'
                              '<label>Service month<input id="collect-month" type="month" required></label>'
                              '<button class="action-button">Record</button> <span id="collect-status" role="status"></span></form>')
            if admin:
                partner_forms += ('<form class="panel rule-form" id="remit"><h2>Record money received from the partner</h2>'
                                  '<label>Service month<input id="remit-month" type="month" required></label>'
                                  '<label>Amount received ($)<input id="remit-amount" type="number" min="0.01" step="0.01" required></label>'
                                  '<label>Payment reference<input id="remit-ref" maxlength="120" required></label>'
                                  '<button class="action-button">Record</button> <span id="remit-status" role="status"></span></form>')
        content = f'''<header class="topbar"><div><p class="eyebrow">Confidential · agreement v{int(a["version"])}</p><h1>{escape(name)}</h1></div>
<a class="ghost-button" href="/partner-agreements">All agreements</a></header>
<section class="health-grid"><div class="panel"><h2>Price breakdown</h2><table class="data-table"><tbody>
<tr><th>AnyAiCam plan</th><td>{escape(_plan_label(a))}</td></tr>
<tr><th>AnyAiCam base price</th><td>{money(a["base_price_cents"])}/month</td></tr>
<tr><th>Partner monthly upcharge</th><td>{money(a["upcharge_cents"])}/month</td></tr>
<tr><th>Customer price</th><td><strong>{money(a["customer_price_cents"])}/month</strong></td></tr>
<tr><th>Partner activation / setup fee</th><td>{money(a["activation_fee_cents"])} one time{" · collected " + _when(a.get("activation_fee_collected_at")) if a.get("activation_fee_collected_at") else ""}</td></tr>
<tr><th>Billing</th><td>{escape(BILLING_LABELS.get(a["billing_party"], a["billing_party"]))}</td></tr>
<tr><th>Status</th><td>{escape(STATUS_LABELS.get(a["status"], a["status"]))}{" · approved " + _when(a.get("approved_at")) if a.get("approved_at") else ""}</td></tr>
</tbody></table></div>
<div class="panel"><h2>Residual and payouts</h2>
<p>{int(a["commission_rate_bps"]) / 100:g}% residual for {int(a["commission_max_paid_months"])} paid months · {fin["residual_paid_months"]} paid so far, {fin["residual_months_remaining"]} remaining.</p>
<p>Residual started: {_when(a.get("commission_start_at"))} · ended: {_when(a.get("commission_end_at"))}</p>
<ul>{by_kind}</ul><p>Not yet on a statement: <strong>{money(fin["unpaid_cents"])}</strong></p>
<p>Next payout: {escape(nxt["quarter"])} statement, issued after the quarter ends ({escape(nxt["closes_after"])}). Payee accrued: {money(nxt["accrued_cents"])}; carried from the last statement: {money(nxt["carried_forward_cents"])}.</p>
{_residual_terms()}</div></section>
<div class="panel" style="margin-top:14px">{" ".join(actions)} <span id="act-status" role="status"></span></div>
{partner_forms}
<section class="panel" style="overflow:auto;margin-top:14px"><h2>Payments and amounts</h2><table class="data-table"><thead><tr><th>Recorded</th><th>Type</th><th>Month</th><th>Collected</th><th>Amount</th><th>Payout</th>{"<th></th>" if admin else ""}</tr></thead><tbody>{entries}</tbody></table></section>
<section class="panel" style="overflow:auto;margin-top:14px"><h2>Agreement history</h2><table class="data-table"><thead><tr><th>Version</th><th>Base</th><th>Upcharge</th><th>Customer price</th><th>Activation fee</th><th>Billing</th><th>Status</th><th>Created</th></tr></thead><tbody>{history_rows}</tbody></table></section>'''
        agreement_json = json.dumps(a["id"])
        scripts = ("<script>" + CSRF_JS + "const id=" + agreement_json + ",st=document.getElementById('act-status');"
                   """const on=(i,f)=>{const el=document.getElementById(i);if(el)el.addEventListener('click',f);};
on('approve',e=>act(e.target,`/api/admin/partner-agreements/${id}/approve`,{},st));
on('activate',e=>act(e.target,`/api/admin/partner-agreements/${id}/activate-service`,{},document.getElementById('svc-status')));
on('suspend',e=>{const reason=prompt('Reason for suspending service');if(reason)act(e.target,`/api/admin/partner-agreements/${id}/suspend-service`,{reason},document.getElementById('svc-status'));});
on('reject',e=>{const reason=prompt('Reason for rejecting');if(reason)act(e.target,`/api/admin/partner-agreements/${id}/reject`,{reason},st);});
on('cancel',e=>{const reason=prompt('Reason for cancelling');if(reason)act(e.target,`/api/admin/partner-agreements/${id}/cancel`,{reason},st);});
document.querySelectorAll('[data-reverse]').forEach(b=>b.addEventListener('click',()=>{const reason=prompt('Reason for the reversal');
if(reason)act(b,`/api/admin/commission-ledger/${b.dataset.reverse}/reverse`,{reason},st);}));
const c=document.getElementById('collect');if(c)c.addEventListener('submit',e=>{e.preventDefault();act(c.querySelector('button'),
`/api/partner/agreements/${id}/collections`,{service_month:document.getElementById('collect-month').value},document.getElementById('collect-status'));});
const r=document.getElementById('remit');if(r)r.addEventListener('submit',e=>{e.preventDefault();act(r.querySelector('button'),
`/api/admin/partner-agreements/${id}/remittances`,{service_month:document.getElementById('remit-month').value,
amount_cents:Math.round(Number(document.getElementById('remit-amount').value)*100),reference:document.getElementById('remit-ref').value},
document.getElementById('remit-status'));});
</script>""")
        return shell("Customer agreement", "partner-agreements", content, scripts)

    @app.get("/partner-payouts", response_class=HTMLResponse)
    def payouts_page(request: Request):
        identity = _page_identity(request)
        if not identity:
            return RedirectResponse("/partner-login", status_code=303)
        admin = pa.is_global_admin(identity)
        statements = pp.statements(identity)
        try:
            payee = pb.payee_of(identity)
        except pa.AgreementForbidden:
            payee = None
        sections = []
        notice = {"updated": "Your bank details were sent to the payout provider. The status is shown below.",
                  "check-later": "The payout provider has not confirmed your details yet. Check back shortly.",
                  "expired": "The setup link expired. Start the bank setup again."}.get(request.query_params.get("banking", ""))
        if notice:
            sections.append(f'<div class="panel" role="status" style="margin-bottom:14px">{escape(notice)}</div>')
        if payee:
            sections.append(_earnings_and_banking(payee))
        elif identity.get("role") == "salesperson":
            sections.append('<div class="panel">Payouts for your sales go to your partner company, which receives the quarterly statement. '
                            'Your own earnings are listed on <a href="/partner-revenue">Recurring revenue</a>.</div>')
        if admin:
            sections.append('<form class="panel rule-form" id="close" style="margin-top:14px"><h2>Issue quarterly statements</h2>'
                            '<label>Quarter (after it has ended)<input id="close-quarter" placeholder="2026-Q3" pattern="\\d{4}-Q[1-4]" required></label>'
                            '<button class="action-button">Issue statements</button> <span id="close-status" role="status"></span></form>')
        banks = {b["payee_key"]: b for b in pb.all_accounts()} if admin else {}
        selected = request.query_params.get("statement")
        detail = _statement_detail(_call(pp.statement, identity, selected)) if selected else ""
        table = "".join(_statement_row(s, admin, banks) for s in statements) \
            or f'<tr><td colspan="{10 if admin else 8}">No payout statements yet.</td></tr>'
        content = (f'<header class="topbar"><div><p class="eyebrow">Confidential · partner billing</p><h1>Payouts</h1></div>'
                   f'<a class="ghost-button" href="/partner-agreements">Customer agreements</a></header>{"".join(sections)}{_residual_terms()}'
                   '<section class="panel" style="overflow:auto;margin-top:14px"><h2>Payout history</h2><table class="data-table"><thead><tr><th>Quarter</th>'
                   '<th>Payee</th><th>Opening</th><th>This quarter</th><th>Payable</th><th>Carried forward</th><th>Status</th><th>Statement</th>'
                   f'{"<th>Bank</th><th></th>" if admin else ""}</tr></thead><tbody>{table}</tbody></table></section>{detail}')
        scripts = ("<script>" + CSRF_JS + """const c=document.getElementById('close');if(c)c.addEventListener('submit',e=>{e.preventDefault();
act(c.querySelector('button'),'/api/admin/partner-payouts/close',{quarter:document.getElementById('close-quarter').value},document.getElementById('close-status'));});
document.querySelectorAll('[data-paid]').forEach(b=>b.addEventListener('click',()=>{const reference=prompt('Payment reference');
if(reference)act(b,`/api/admin/partner-payouts/${b.dataset.paid}/paid`,{reference},b.parentElement);}));
const bs=document.getElementById('bank-setup');if(bs)bs.addEventListener('click',()=>{const st=document.getElementById('bank-status');bs.disabled=true;
st.textContent='Opening the secure bank setup…';send('/api/partner/payout-account/onboarding',{}).then(p=>{location.href=p.url;})
.catch(e=>{st.textContent=e.message;bs.disabled=false;});});
const br=document.getElementById('bank-refresh');if(br)br.addEventListener('click',()=>act(br,'/api/partner/payout-account/refresh',{},document.getElementById('bank-status')));
</script>""")
        return shell("Payouts", "partner-payouts", content, scripts)


def _earnings_and_banking(payee: dict) -> str:
    d = pp.dashboard(payee["partner_id"], payee["salesperson_user_id"])
    bank = pb.public_view(pb.account(payee["payee_key"]), payee["payee_key"])
    bank_line = (f"{escape(bank.get('bank_name') or 'Bank account')} ending {escape(bank['last4'])}"
                 if bank.get("last4") else "No bank account on file")
    started = bank["status"] != pb.NOT_STARTED
    refresh = '<button class="ghost-button" id="bank-refresh">Refresh status</button>' if started else ""
    return f'''<section class="health-grid"><div class="panel"><h2>Earnings not yet paid</h2><table class="data-table"><tbody>
<tr><th>20% residual earned</th><td>{money(d["residual_cents"])}</td></tr>
<tr><th>Partner markup owed (upcharges and activation fees, after processing fees)</th><td>{money(d["markup_cents"])}</td></tr>
<tr><th>Adjustments (refunds, chargebacks, partner-billed base)</th><td>{money(d["adjustments_cents"])}</td></tr>
<tr><th>Carried from the last statement</th><td>{money(d["carried_forward_cents"])}</td></tr>
<tr><th>Estimated next payout</th><td><strong>{money(d["estimated_next_payout_cents"])}</strong></td></tr>
</tbody></table><p>Current quarter: <strong>{escape(d["quarter"])}</strong>. Its statement is issued after the quarter ends ({escape(d["statement_after"])}).</p></div>
<div class="panel"><h2>Payouts / Banking</h2><p>Status: <strong>{escape(bank["status_label"])}</strong></p><p>{bank_line}</p>
<p class="health-detail">You enter your bank details on our payout provider's secure page (Stripe). AnyAiCam never sees or stores your routing or account number; we keep only the bank name, the last four digits and the verification status.</p>
<button class="action-button" id="bank-setup">{"Update bank account" if started else "Set up bank account"}</button> {refresh}
<span id="bank-status" role="status"></span></div></section>'''


def _statement_row(s: dict, admin: bool, banks: dict) -> str:
    sid = escape(s["id"], quote=True)
    cells = (f"<tr><td><a href=\"/partner-payouts?statement={sid}\">{escape(s['quarter'])}</a></td><td>{escape(s['payee_key'])}</td>"
             f"<td>{money(s['opening_balance_cents'])}</td><td>{money(s['period_total_cents'])}</td><td><strong>{money(s['payable_cents'])}</strong></td>"
             f"<td>{money(s['carried_forward_cents'])}</td><td>{escape(STATUS_LABELS.get(s['status'], s['status']))}"
             f"{' · ' + escape(s['payment_reference'] or '') if s.get('payment_reference') else ''}</td>"
             f"<td><a href=\"/partner-payouts/{sid}/statement.csv\">CSV</a></td>")
    if admin:
        cells += f"<td>{escape((banks.get(s['payee_key']) or {}).get('status_label') or 'Not set up')}</td><td>{_paid_button(s)}</td>"
    return cells + "</tr>"


def _statement_detail(s: dict) -> str:
    names = _names(e["customer_id"] for e in s["entries"])
    lines = "".join(
        f"<tr><td>{_when(e['earned_at'])}</td><td>{escape(names.get(e['customer_id']) or 'Customer')}</td>"
        f"<td>{escape(KIND_LABELS.get(e['kind'], e['kind']))}{' (reversal)' if e.get('reverses_entry_id') else ''}</td>"
        f"<td>{escape(e.get('service_month') or e.get('stripe_invoice_id') or '—')}</td><td>{money(e['amount_cents'])}</td></tr>"
        for e in s["entries"])
    return (f'<section class="panel" style="overflow:auto;margin-top:14px"><h2>{escape(s["quarter"])} statement · {escape(s["payee_key"])}</h2>'
            f'<p>Opening balance {money(s["opening_balance_cents"])} + this quarter {money(s["period_total_cents"])} = '
            f'payable <strong>{money(s["payable_cents"])}</strong>, carried forward {money(s["carried_forward_cents"])}. '
            f'<a href="/partner-payouts/{escape(s["id"], quote=True)}/statement.csv">Download statement (CSV)</a></p>'
            '<table class="data-table"><thead><tr><th>Recorded</th><th>Customer</th><th>Type</th><th>Invoice / month</th><th>Amount</th></tr></thead>'
            f'<tbody>{lines}</tbody></table></section>')
