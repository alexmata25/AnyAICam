"""Website-first Build Your System purchase pages (owner decision 2026-10-05).

A new customer stays in the sales funnel until payment succeeds:

    Build Your System (anyaicam.com) -> create an account / sign in
    -> /order-summary -> Proceed to Secure Checkout -> Stripe Checkout
    -> /order-complete -> Set Up My System -> /customer/setup -> normal VMS.

These pages are deliberately outside the VMS shell (no VMS navigation). They
reuse billing v2 end to end: POST /api/v2/customer/subscription/checkout
creates the Checkout Session (server-side Price allowlist and catalog price
check, canonical Stripe customer, checkout_guard duplicate/lost-response
protection), and only the verified Stripe webhook grants the plan. Nothing
here grants anything, reads a price from the browser, or creates a Checkout
Session before the customer presses Proceed to Secure Checkout. Existing
plan holders keep managing their plan on My subscription.
"""
from __future__ import annotations

import json
import re
from html import escape
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import per_camera_billing as billing

WEBSITE_BUILD_URL = "https://anyaicam.com/build-your-system.html"
SALES_PHONE = "(346) 554-4699"
SESSION_ID = re.compile(r"cs_(test|live)_[A-Za-z0-9]{8,200}")
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

STYLE = (".card{max-width:560px}.order-line{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid #e2e8f2}"
         ".order-line span:last-child{text-align:right;font-weight:700}.order-total{font-size:20px;font-weight:900}"
         ".note{color:#4b5873;font-size:14px}.notice{padding:10px 12px;border-radius:9px;background:#eef3ff;color:#17233e}"
         ".notice.warn{background:#fff4dc;color:#5c3d00}.notice.ok{background:#e7f6ec;color:#14532d}"
         ".card select{padding:12px;border:1px solid #aab7ca;border-radius:9px;font:inherit}.row{display:grid;grid-template-columns:2fr 1fr;gap:10px}"
         ".submit{display:block;width:100%;text-align:center;text-decoration:none;font-size:16px}.submit[disabled]{opacity:.6;cursor:wait}"
         ".ghost{display:block;text-align:center;margin-top:10px;font-weight:700;color:#5360df;text-decoration:none;background:none;border:0;"
         "font:inherit;cursor:pointer;width:100%}.head{justify-content:space-between}.head form{margin:0}"
         "@media(max-width:480px){.row{grid-template-columns:1fr}.card{padding:20px}}")


def _page(title: str, body: str, *, script: str = "", signed_in: bool = True) -> str:
    try:
        from cloud_features import CUSTOMER_AUTH_STYLE as base
    except Exception:
        base = ""
    sign_out = ('<form method="post" action="/partner-logout"><button class="ghost" type="submit" style="width:auto">Sign out</button></form>'
                if signed_in else "")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="referrer" content="no-referrer"><title>{escape(title)} | ANY AI CAM</title><style>{base}{STYLE}</style></head><body>'
            '<header class="head"><a class="brand" href="https://anyaicam.com/"><img src="/static/brand-icon.png" alt="AnyAiCam">ANY AI CAM</a>'
            f'{sign_out}</header><main class="auth-wrap"><section class="card">{body}</section></main><script>{script}</script></body></html>')


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _cameras(count: int) -> str:
    return f"{count} licensed camera{'' if count == 1 else 's'}"


def _hardware() -> dict:
    try:
        import hardware_orders
        return {sku: {"name": name, "cents": cents} for sku, _product, name, cents, _env in hardware_orders.HARDWARE_CATALOG}
    except Exception:
        return {}


def _identity(request: Request, next_path: str):
    """(identity, None) for a customer owner, else (None, response)."""
    from partner_portal import partner_identity
    identity = partner_identity(request)
    if not identity or not identity.get("customer_id") or identity.get("role") not in {"customer_owner", "customer_viewer"}:
        return None, RedirectResponse(f"/customer-login.html?next={quote(next_path, safe='')}", status_code=303)
    if identity.get("role") != "customer_owner":
        return None, HTMLResponse(_page("Account owner required", '<h2>Only the account owner can buy a plan</h2>'
                                        '<p>Ask the owner of this AnyAiCam account to complete the purchase.</p>'
                                        '<a class="submit" href="/customer-account">Open AnyAiCam</a>'), status_code=403, headers=NO_STORE)
    return identity, None


def _order_lines(selection: dict) -> tuple[str, str]:
    """Summary rows and the hardware notes, all priced from the server catalog."""
    plan = billing.PLANS[selection["plan"]]
    quantity = int(selection.get("cameras") or billing.MIN_CAMERA_QUANTITY)
    unit = int(plan["monthly_cents_per_camera"])
    retention = f"{plan['local_retention_days']}-day local recording retention"
    if plan.get("cloud_event_storage"):
        retention += f" · {plan['cloud_event_retention_days']}-day cloud event retention"
    lines = (f'<div class="order-line" id="order-plan"><span>AnyAiCam {escape(plan["display_name"])}</span><span>{_cameras(quantity)}</span></div>'
             f'<div class="order-line"><span>{_money(unit)} per camera/month</span><span>{retention}</span></div>'
             f'<div class="order-line"><span>AnyAiCam VMS software license</span><span>Included · {quantity} camera{"" if quantity == 1 else "s"}</span></div>'
             f'<div class="order-line order-total" id="order-monthly"><span>Monthly subscription</span><span>{_money(unit * quantity)}/month</span></div>')
    notes = []
    catalog = _hardware()
    if selection.get("appliance") in catalog:
        item = catalog[selection["appliance"]]
        notes.append(f'<li id="order-appliance">{escape(item["name"])} appliance · {_money(item["cents"])} one-time</li>')
    if selection.get("relays"):
        relay = catalog.get("AIC-RELAY-NUMATO-3CH")
        if relay:
            notes.append(f'<li id="order-relays">{escape(relay["name"])} × {int(selection["relays"])} · {_money(relay["cents"])} each, one-time</li>')
    hardware = ""
    if notes:
        hardware = (f'<div class="notice warn" id="order-hardware"><strong>Hardware you selected</strong><ul style="margin:6px 0">{"".join(notes)}</ul>'
                    f'Hardware is arranged with AnyAiCam by phone, not in this online checkout: call {SALES_PHONE}. '
                    'Your subscription can start now; setup continues when your appliance arrives.</div>')
    elif selection.get("own_pc"):
        hardware = ('<p class="note" id="order-own-pc">Runs on your own Ubuntu 24.04 PC. The AnyAiCam VMS software license is '
                    'included with this subscription; there is no separate license charge.</p>')
    return lines, hardware


def _chooser(selection: dict) -> str:
    """Plan/quantity form; submits to this page, which re-prices server-side."""
    options = "".join(f'<option value="{key}"{" selected" if key == selection.get("plan") else ""}>{escape(plan["display_name"])} · '
                      f'{_money(plan["monthly_cents_per_camera"])} per camera/month</option>' for key, plan in billing.PLANS.items())
    keep = "".join(f'<input type="hidden" name="{key}" value="{escape(str(value), quote=True)}">'
                   for key, value in (("appliance", selection.get("appliance")), ("relays", selection.get("relays")),
                                      ("vms_licence", selection.get("cameras") if selection.get("own_pc") else None)) if value)
    return ('<form method="get" action="/order-summary" id="order-change"><div class="row">'
            f'<label>Plan<select name="plan" id="order-plan-key">{options}</select></label>'
            f'<label>Cameras<input name="cameras" id="order-cameras" type="number" min="1" max="64" step="1" '
            f'value="{int(selection.get("cameras") or 1)}" required></label></div>{keep}'
            '<button class="ghost" type="submit">Update order</button></form>')


CHECKOUT_SCRIPT = ("const csrf=()=>{const m=document.cookie.split('; ').find(x=>x.startsWith('anyaicam_csrf='));if(!m)return '';"
                   "let v=decodeURIComponent(m.split('=').slice(1).join('='));return v.length>=2&&v[0]==='\"'&&v[v.length-1]==='\"'?v.slice(1,-1):v};"
                   "try{sessionStorage.removeItem('orderActivation')}catch(e){}const pay=document.getElementById('order-checkout');if(pay)pay.onclick=async()=>{const msg=document.getElementById('order-message');"
                   "if(pay.disabled)return;pay.disabled=true;pay.textContent='Opening secure checkout…';msg.textContent='';let r,b={};"
                   "try{r=await fetch('/api/v2/customer/subscription/checkout',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},"
                   "body:JSON.stringify({plan_key:pay.dataset.plan,camera_quantity:Number(pay.dataset.cameras),flow:'order'})});b=await r.json().catch(()=>({}))}"
                   "catch(e){r=null}if(r&&r.ok&&b.checkout_url){location.href=b.checkout_url;return}"
                   "pay.disabled=false;pay.textContent='Proceed to Secure Checkout';"
                   "msg.textContent=(b&&b.detail&&typeof b.detail==='string')?b.detail:'Secure checkout could not be opened. Please try again.'};")


def _session_state(customer_id: str, session_id: str) -> str:
    """Read-only look at this customer's own Checkout Session: 'paid',
    'not_paid', 'unknown' (Stripe unreachable) or 'foreign'. Grants nothing."""
    if not SESSION_ID.fullmatch(session_id or ""):
        return "foreign"
    try:
        import main
        session = main.stripe_api_get(f"/v1/checkout/sessions/{session_id}")
    except Exception:
        return "unknown"
    if str((session.get("metadata") or {}).get("anyaicam_customer_id") or "") != customer_id:
        return "foreign"
    if session.get("status") == "complete" and session.get("payment_status") in ("paid", "no_payment_required"):
        return "paid"
    return "not_paid"


def register_order_funnel_routes(app: FastAPI) -> None:
    @app.get(billing.ORDER_SUMMARY_PATH, response_class=HTMLResponse)
    def order_summary(request: Request):
        query_selection = billing.selection_from_query(request.query_params)
        next_path = billing.purchase_intent_next(f"{billing.ORDER_SUMMARY_PATH}?{billing.selection_query(query_selection)}") \
            or billing.ORDER_SUMMARY_PATH
        identity, refusal = _identity(request, next_path)
        if refusal:
            return refusal
        customer_id = identity["customer_id"]
        if billing.has_camera_plan(customer_id):
            progress = billing.build_progress(customer_id)
            if progress and progress["state"] == billing.BUILD_PAID:
                return RedirectResponse(billing.ORDER_COMPLETE_PATH, status_code=303)
            manage = "/subscription-portal"
            if query_selection.get("plan"):
                manage += "?" + urlencode({key: query_selection[key] for key in ("plan", "cameras") if key in query_selection})
            return HTMLResponse(_page("Your AnyAiCam plan", '<h2>You already have an AnyAiCam plan</h2>'
                                      '<p>Plan changes, camera count and billing for an existing account are managed in My subscription.</p>'
                                      f'<a class="submit" id="order-manage" href="{escape(manage, quote=True)}">Open My subscription</a>'),
                                headers=NO_STORE)
        if query_selection.get("plan"):
            billing.save_build_selection(customer_id, query_selection)
        progress = billing.build_progress(customer_id) or {}
        selection = progress if progress.get("plan") else {}
        cancelled = request.query_params.get("checkout") == "cancelled"
        notice = ('<p class="notice" id="order-cancelled" role="status">Checkout was cancelled. No payment was taken and nothing was activated.</p>'
                  if cancelled else "")
        if not selection:
            body = ('<h2>Choose your AnyAiCam plan</h2>' + notice +
                    '<p>Pick a plan and how many cameras to license, then review your order.</p>' + _chooser({}) +
                    f'<a class="ghost" href="{WEBSITE_BUILD_URL}">Back to Build Your System</a>')
            return HTMLResponse(_page("Your order", body), headers=NO_STORE)
        lines, hardware = _order_lines(selection)
        quantity = int(selection.get("cameras") or billing.MIN_CAMERA_QUANTITY)
        body = ('<h2>Order summary</h2>' + notice + lines + hardware +
                '<p class="note">Billed monthly. Stripe shows the final amount, including any discount or tax, before you pay. '
                'Cancel any time from My subscription.</p>'
                f'<button class="submit" id="order-checkout" type="button" data-plan="{escape(selection["plan"], quote=True)}" '
                f'data-cameras="{quantity}">Proceed to Secure Checkout</button>'
                '<p id="order-message" class="note" role="status" aria-live="polite"></p>'
                '<details style="margin-top:14px"><summary class="note" style="cursor:pointer">Change plan or camera count</summary>'
                + _chooser(selection) + '</details>'
                f'<a class="ghost" href="{WEBSITE_BUILD_URL}">Back to Build Your System</a>')
        return HTMLResponse(_page("Order summary", body, script=CHECKOUT_SCRIPT), headers=NO_STORE)

    @app.get(billing.ORDER_COMPLETE_PATH, response_class=HTMLResponse)
    def order_complete(request: Request, session_id: str = ""):
        next_path = billing.ORDER_COMPLETE_PATH + (f"?session_id={session_id}" if SESSION_ID.fullmatch(session_id or "") else "")
        identity, refusal = _identity(request, next_path)
        if refusal:
            return refusal
        customer_id = identity["customer_id"]
        progress = billing.build_progress(customer_id) or {}
        if billing.has_camera_plan(customer_id):
            entitlement = billing.entitlement_payload(billing.entitlement_for_customer(customer_id))
            summary = (f'<div class="order-line"><span>AnyAiCam {escape(entitlement["display_name"])}</span>'
                       f'<span>{_cameras(int(entitlement["camera_quantity"]))}</span></div>') if entitlement else ""
            hardware = ""
            if progress.get("appliance") or progress.get("relays"):
                hardware = (f'<p class="note" id="order-hardware-reminder">Your hardware is arranged with AnyAiCam by phone at {SALES_PHONE}. '
                            'Start setup when your appliance arrives.</p>')
            if billing._setup_needed(customer_id):
                body = ('<p class="notice ok" role="status">Payment complete</p><h2 id="order-ready">Your AnyAiCam system is ready to set up.</h2>'
                        + summary + hardware +
                        '<p>Next, connect your AnyAiCam appliance and add your cameras.</p>'
                        '<a class="submit" id="order-setup" href="/customer/setup">Set Up My System</a>')
            else:
                body = ('<h2>Your AnyAiCam plan is active</h2>' + summary +
                        '<a class="submit" href="/customer-account">Open AnyAiCam</a>')
            return HTMLResponse(_page("Order complete", body), headers=NO_STORE)
        if not session_id:
            return RedirectResponse(billing.ORDER_SUMMARY_PATH, status_code=303)
        state = _session_state(customer_id, session_id)
        if state == "foreign":
            return HTMLResponse(_page("Order not found", "<h2>We couldn't find this checkout</h2>"
                                      f'<a class="submit" href="{billing.ORDER_SUMMARY_PATH}">Return to your order</a>'),
                                status_code=404, headers=NO_STORE)
        if state == "not_paid":
            return HTMLResponse(_page("Payment not completed", '<h2>Payment not completed</h2>'
                                      '<p>Stripe has not confirmed this payment, so nothing was activated. You can try again.</p>'
                                      f'<a class="submit" href="{billing.ORDER_SUMMARY_PATH}">Return to your order</a>'), headers=NO_STORE)
        # Paid (or Stripe briefly unreachable): the plan appears as soon as
        # the verified webhook grants it. Refresh a bounded number of times.
        script = ("let n=0;try{n=Number(sessionStorage.getItem('orderActivation')||0)}catch(e){}"
                  "if(n<20){try{sessionStorage.setItem('orderActivation',String(n+1))}catch(e){}setTimeout(()=>location.reload(),3000)}"
                  "else{const s=document.getElementById('order-activating');if(s)s.textContent="
                  + json.dumps("Activation is taking longer than usual. Refresh this page in a minute, or contact AnyAiCam support "
                               f"at {SALES_PHONE} if your plan does not appear.") + "}")
        return HTMLResponse(_page("Activating your plan", '<h2>Payment received</h2>'
                                  '<p id="order-activating" role="status" aria-live="polite">Activating your AnyAiCam plan… '
                                  'This page updates automatically.</p>', script=script), headers=NO_STORE)
