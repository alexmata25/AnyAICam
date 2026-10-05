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
# Technical support only -- hardware is ordered online (owner decision 2026-10-05).
SUPPORT_PHONE = "(346) 554-4699"
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
    """Summary rows and notes, all priced from the server catalog
    (build_orders.order_quote): what is charged today, the monthly plan,
    and when the plan starts (owner decisions 2026-10-05)."""
    import build_orders
    quote = build_orders.order_quote(selection)
    plan = billing.PLANS[selection["plan"]]
    quantity = quote["cameras"]
    unit = int(plan["monthly_cents_per_camera"])
    retention = f"{plan['local_retention_days']}-day local recording retention"
    if plan.get("cloud_event_storage"):
        retention += f" · {plan['cloud_event_retention_days']}-day cloud event retention"
    line_ids = {build_orders.RELAY_SKU: "order-relays"}
    lines = (f'<div class="order-line" id="order-plan"><span>AnyAiCam {escape(plan["display_name"])}</span><span>{_cameras(quantity)}</span></div>'
             f'<div class="order-line"><span>{_money(unit)} per camera/month</span><span>{retention}</span></div>')
    for line in quote["one_time"]:
        line_id = line_ids.get(line["sku"]) or ("order-appliance" if line["sku"].startswith(APPLIANCE_PREFIX) else "order-license")
        count = f" × {line['quantity']}" if line["quantity"] > 1 else ""
        lines += (f'<div class="order-line" id="{line_id}"><span>{escape(line["name"])}{count}</span>'
                  f'<span>{_money(line["cents"] * line["quantity"])} one-time</span></div>')
    if quote["kind"] == build_orders.APPLIANCE_ORDER:
        lines += ('<div class="order-line"><span>AnyAiCam VMS software license</span><span>Included with your appliance</span></div>')
    elif quote["kind"] == build_orders.PLAN_ORDER:
        lines += (f'<div class="order-line"><span>AnyAiCam VMS software license</span>'
                  f'<span>Included · {quantity} camera{"" if quantity == 1 else "s"}</span></div>')
    lines += (f'<div class="order-line" id="order-monthly"><span>Monthly subscription</span><span>{_money(unit * quantity)}/month</span></div>'
              f'<div class="order-line order-total" id="order-today"><span>Due today</span><span>{_money(quote["due_today_cents"])}</span></div>')
    if quote["kind"] == build_orders.APPLIANCE_ORDER:
        from hardware_fulfillment import PREPARATION_TIMEFRAME_TEXT
        notes = ('<p class="note" id="order-plan-start">Your hardware is charged today. Your monthly subscription starts when you '
                 'activate your appliance, not before. Stripe saves your card securely at checkout so the subscription can '
                 'start then without asking for it again.</p>'
                 f'<p class="note" id="order-shipping">{escape(PREPARATION_TIMEFRAME_TEXT)} We prepare and test your appliance before it ships.</p>')
    elif quote["kind"] == build_orders.OWN_PC_ORDER:
        notes = ('<p class="note" id="order-plan-start">Your one-time VMS software license and your first month are charged today; '
                 'your monthly subscription starts today.</p>'
                 '<p class="note" id="order-own-pc">Runs on your own Ubuntu 24.04 PC. The license is yours to keep; the '
                 'monthly subscription is separate.</p>')
    else:
        notes = '<p class="note" id="order-plan-start">Your monthly subscription starts today.</p>'
    return lines, notes


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
                   # The order is the account's saved selection: the request carries no product or price.
                   "try{r=await fetch('/api/v2/customer/build-order/checkout',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},"
                   "body:'{}'});b=await r.json().catch(()=>({}))}"
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


# Hardware delivery (owner requirement 2026-10-05). After payment, a
# customer who bought an AnyAiCam appliance cannot set up yet: the
# appliance has to be prepared and delivered first. Their state is read
# from durable records -- the Build Your System selection, the paid plan,
# hardware_orders.fulfillment_status (hardware_fulfillment.py) -- so it
# survives closing the browser, signing out and coming back days later
# from another device. A customer on their own PC (or with no hardware)
# can download the installer and set up straight away.
APPLIANCE_PREFIX = "AIC-APPLIANCE-"
HARDWARE_ARRANGING = "arranging"    # appliance chosen, its paid order not recorded yet (webhook still confirming)
HARDWARE_PREPARING = "preparing"    # recorded hardware order, not shipped yet
HARDWARE_SHIPPED = "shipped"
HARDWARE_READY = "ready"            # delivered


def hardware_delivery(customer_id: str, progress: dict | None = None) -> dict | None:
    """Where the customer's appliance is, or None when there is nothing to
    wait for (own PC, software only). Refunded, disputed or cancelled
    hardware orders do not count."""
    from partner_db import rows
    orders = rows("SELECT * FROM hardware_orders WHERE customer_id=? AND sku LIKE ? AND status='paid' "
                  "AND COALESCE(fulfillment_status,'') NOT IN ('cancelled') ORDER BY created_at DESC",
                  (customer_id, APPLIANCE_PREFIX + "%"))
    if not (progress or {}).get("appliance") and not orders:
        return None
    if not orders:
        return {"state": HARDWARE_ARRANGING, "order": None}
    order = orders[0]
    status = str(order.get("fulfillment_status") or "")
    state = HARDWARE_READY if status == "delivered" else HARDWARE_SHIPPED if status == "shipped" else HARDWARE_PREPARING
    return {"state": state, "order": order}


def _appliance_name(progress: dict, delivery: dict) -> str:
    order = delivery.get("order") or {}
    if order.get("product_name"):
        return str(order["product_name"])
    item = _hardware().get(progress.get("appliance") or "")
    return item["name"] if item else "AnyAiCam appliance"


def _shipment_lines(order: dict | None) -> str:
    """Carrier and tracking only when AnyAiCam recorded them; never invented."""
    if not order:
        return ""
    parts = []
    if order.get("carrier"):
        parts.append(f"Carrier: {escape(str(order['carrier']))}")
    if order.get("tracking_number"):
        parts.append(f"Tracking number: {escape(str(order['tracking_number']))}")
    link = str(order.get("tracking_link") or "")
    if link.startswith("https://"):
        parts.append(f'<a href="{escape(link, quote=True)}" rel="noopener noreferrer" target="_blank">Track your shipment</a>')
    return f'<p class="note" id="order-tracking">{"<br>".join(parts)}</p>' if parts else ""


STORAGE_STARTS_AT_ACTIVATION = ('<p class="note" id="order-storage-start">Your monthly subscription starts when you activate '
                                'your appliance, using the card you paid with. Nothing is charged for it before then.</p>')


def _hardware_status_body(progress: dict, delivery: dict, summary: str) -> str:
    state, order = delivery["state"], delivery.get("order")
    name = escape(_appliance_name(progress, delivery))
    if state == HARDWARE_READY:
        return ('<p class="notice ok" role="status">Your appliance has been delivered</p>'
                '<h2 id="order-ready">Your AnyAiCam system is ready to set up.</h2>' + summary +
                f"<p>Plug in your {name}, then we'll walk you through activating it and adding your cameras.</p>"
                + STORAGE_STARTS_AT_ACTIVATION +
                '<a class="submit" id="order-setup" href="/customer/setup">Start Setup</a>')
    if state == HARDWARE_SHIPPED:
        return ('<p class="notice ok" role="status">Order confirmed · Shipped</p>'
                '<h2 id="order-preparing">Your AnyAiCam system is on its way.</h2>' + summary + _shipment_lines(order) +
                f"<p>When your {name} arrives, sign in to your AnyAiCam account and we'll walk you through setting up "
                'your appliance and cameras.</p>' + STORAGE_STARTS_AT_ACTIVATION +
                '<a class="submit" id="order-setup" href="/customer/setup">My appliance has arrived — Start Setup</a>')
    if state == HARDWARE_PREPARING:
        from hardware_fulfillment import PREPARATION_TIMEFRAME_TEXT, generate_order_number
        status = (f'<p class="note" id="order-status">Order {escape(generate_order_number(order["id"]))}: {name} · being prepared. '
                  f'{escape(PREPARATION_TIMEFRAME_TEXT)}</p>')
    else:
        status = f'<p class="note" id="order-status">Your {name} order is being confirmed.</p>'
    return ('<p class="notice ok" role="status">Payment complete · Order confirmed</p>'
            '<h2 id="order-preparing">Your AnyAiCam system is being prepared.</h2>' + summary + status +
            "<p>We'll email you when your system ships. When your package arrives, sign in to your AnyAiCam account "
            "and we'll walk you through setting up your appliance and cameras.</p>" + STORAGE_STARTS_AT_ACTIVATION +
            "<p class=\"note\">You don't need to set up any cameras yet. You can close this page; your order is saved to your account.</p>")


def _software_body(progress: dict, summary: str) -> str:
    try:
        import customer_downloads
        installer = customer_downloads.latest_vms_installer()
    except Exception:
        installer = None
    if installer:
        download = ('<a class="submit" id="order-download" href="/api/customer/downloads/vms-installer" download>Download AnyAiCam</a>'
                    '<p class="note">For Ubuntu 24.04 (64-bit PC, 4+ CPU cores, 8 GB+ memory, 100 GB free disk).</p>')
    else:
        download = ('<p class="note" id="order-download-pending">The AnyAiCam installer download will appear here and on '
                    'My subscription as soon as it is published.</p>')
    own_pc = bool(progress.get("own_pc"))
    steps = ('<ol class="note" id="order-steps"><li>Download AnyAiCam and install it on your PC.</li>'
             '<li>Sign in to your AnyAiCam account.</li><li>Start setup: link your PC, then discover and add your cameras.</li></ol>'
             if own_pc else
             '<ol class="note" id="order-steps"><li>Connect your AnyAiCam system.</li><li>Start setup: discover and add your cameras.</li></ol>')
    return ('<p class="notice ok" role="status">Payment complete</p><h2 id="order-ready">Your AnyAiCam system is ready to set up.</h2>'
            + summary + steps + download +
            '<a class="submit" id="order-setup" href="/customer/setup" style="margin-top:10px">Set Up My System</a>')


def _order_email(first_name: str, progress: dict, delivery: dict | None, plan_line: str) -> tuple[str, str, str, str]:
    """(notification type, subject, text, html) for the Build Your System
    order confirmation."""
    import purchase_notifications as pn
    sign_in = pn._sign_in_link()
    if delivery:
        name = _appliance_name(progress, delivery)
        paragraphs = [
            f"Your payment for your {name} is confirmed, and your AnyAiCam system is being prepared.",
            "You don't need to set up any cameras yet.",
            "We'll email you when your system ships. When your package arrives, sign in to your AnyAiCam account "
            "and we'll walk you through setting up your appliance and cameras.",
            f"Your {plan_line} starts when you activate your appliance, using the card you paid with. "
            "Nothing is charged for it before then.",
        ]
        kind, subject = "hardware_order_confirmation", "Your AnyAiCam order is confirmed"
    else:
        paragraphs = [
            f"Your payment is confirmed and your {plan_line} is active.",
            "Sign in to your AnyAiCam account to download AnyAiCam (for your own PC) and start setup: "
            "link your system, then discover and add your cameras.",
        ]
        if progress.get("own_pc"):
            paragraphs.insert(1, "Your one-time AnyAiCam VMS software license is on your account.")
        kind, subject = "account_ready", "Your AnyAiCam subscription is active"
    text = f"Hi {first_name},\n\n" + "\n\n".join(paragraphs) + f"\n\nSign in: {sign_in}\n\n{pn._SUPPORT_FOOTER_TEXT}"
    html = (f"<p>Hi {escape(first_name)},</p>" + "".join(f"<p>{escape(p)}</p>" for p in paragraphs)
            + f'<p><a href="{escape(sign_in, quote=True)}">Sign in to AnyAiCam</a></p>{pn._SUPPORT_FOOTER_HTML}')
    return kind, subject, text, html


def _ordered_plan(customer_id: str) -> tuple[str | None, int]:
    """(plan name, cameras) of the paid plan, or of the storage plan an
    appliance order starts at activation."""
    payload = billing.entitlement_payload(billing.entitlement_for_customer(customer_id))
    if payload:
        return payload["display_name"], int(payload["camera_quantity"])
    deferred = billing.deferred_storage_plan(customer_id)
    if deferred and deferred["plan_key"] in billing.PLANS:
        return billing.PLANS[deferred["plan_key"]]["display_name"], int(deferred["camera_quantity"])
    return None, 0


# Outbox key for the Build Your System order confirmation: one per account,
# in purchase_notifications' provisioning_notifications table.
ORDER_CONFIRMATION_KEY = "build-order-confirmed:"
# An attempt still 'pending' this long after its last update was
# interrupted (crash or restart mid-send) and may be retried.
ORDER_CONFIRMATION_STALE_SECONDS = 300


def _order_confirmation_attempts(customer_id: str) -> list:
    from partner_db import rows
    return rows("SELECT * FROM provisioning_notifications WHERE stripe_event_id=? ORDER BY created_at",
                (ORDER_CONFIRMATION_KEY + customer_id,))


def send_order_confirmation(customer_id: str, *, test_mode: bool | None = None) -> dict:
    """The Build Your System order-confirmation email for this account: at
    most one successful send ever, whatever its wording (hardware or
    software). A failed or interrupted send stays in the outbox as
    'failed'/'pending' and is retried by retry_order_confirmation_
    notifications() -- a transient email-provider failure never suppresses
    it. Once setup is finished the confirmation is out of date, so an
    unsent attempt is closed ('superseded') instead of retried."""
    import purchase_notifications as pn
    attempts = _order_confirmation_attempts(customer_id)
    if any(attempt["status"] == "sent" for attempt in attempts):
        return {"status": "skipped", "reason": "already sent"}
    if not billing.order_paid(customer_id):
        return {"status": "ignored", "reason": "no paid order yet"}
    progress = billing.build_progress(customer_id)
    if not progress or progress["state"] != billing.BUILD_PAID:
        if attempts:
            from partner_db import connection
            with connection() as db:
                db.execute("UPDATE provisioning_notifications SET status='superseded',updated_at=? "
                           "WHERE stripe_event_id=? AND status IN ('pending','failed')",
                           (pn._now(), ORDER_CONFIRMATION_KEY + customer_id))
        return {"status": "ignored", "reason": "no Build Your System order awaiting setup"}
    customer = pn._customer_row(customer_id) or {}
    plan_name, plan_cameras = _ordered_plan(customer_id)
    plan_line = (f"AnyAiCam {plan_name} subscription for {_cameras(plan_cameras)}" if plan_name else "AnyAiCam subscription")
    kind, subject, text, html = _order_email(pn._first_name(customer.get("name")), progress,
                                             hardware_delivery(customer_id, progress), plan_line)
    if attempts:
        # Retry the same outbox row, never a second one under another type.
        kind = attempts[0]["notification_type"]
    return pn._send_once(event_id=ORDER_CONFIRMATION_KEY + customer_id, notification_type=kind, customer_id=customer_id,
                         recipient_email=str(customer.get("email") or ""), subject=subject, text=text, html=html,
                         metadata={"build_system_order": True}, test_mode=test_mode)


def notify_order_confirmed(event: dict) -> dict:
    """Webhook hook: after Stripe's verified webhook granted the plan for a
    Build Your System order, send the order confirmation. Never raises --
    an email problem must not fail the webhook step or touch the payment
    or entitlement; a failed send is retried from the outbox by the
    billing worker. Purchases made on My subscription (no Build Your
    System order) are unchanged and get no new email."""
    try:
        obj = ((event or {}).get("data") or {}).get("object") or {}
        metadata = obj.get("metadata") or {}
        customer_id = str(metadata.get("anyaicam_customer_id") or "")
        if str(metadata.get("anyaicam_billing_version") or "") != "2" or not customer_id:
            return {"status": "ignored", "reason": "not a billing v2 event for an account"}
        import stripe_mode
        return send_order_confirmation(customer_id, test_mode=stripe_mode.is_test_mode(event=event))
    except Exception as error:
        return {"status": "error", "reason": type(error).__name__}


def retry_order_confirmation_notifications(limit: int = 50) -> int:
    """Billing-worker pass: resend order confirmations whose send failed or
    was interrupted. Returns how many were delivered now."""
    from datetime import datetime, timedelta
    from partner_db import rows
    stale_before = (datetime.now() - timedelta(seconds=ORDER_CONFIRMATION_STALE_SECONDS)).isoformat()
    sent = 0
    for pending in rows("SELECT DISTINCT stripe_event_id FROM provisioning_notifications WHERE stripe_event_id LIKE ? "
                        "AND (status='failed' OR (status='pending' AND updated_at<?)) LIMIT ?",
                        (ORDER_CONFIRMATION_KEY + "%", stale_before, limit)):
        customer_id = str(pending["stripe_event_id"])[len(ORDER_CONFIRMATION_KEY):]
        try:
            if send_order_confirmation(customer_id).get("status") == "sent":
                sent += 1
        except Exception:
            continue
    return sent


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
        if billing.order_paid(customer_id):
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
                '<p class="note">Stripe shows the final amount, including any discount or tax, before you pay. '
                'Cancel the monthly subscription any time from My subscription.</p>'
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
        if billing.order_paid(customer_id):
            plan_name, plan_cameras = _ordered_plan(customer_id)
            summary = (f'<div class="order-line"><span>AnyAiCam {escape(plan_name)}</span>'
                       f'<span>{_cameras(plan_cameras)}</span></div>') if plan_name else ""
            deferred = billing.deferred_storage_plan(customer_id)
            script = ""
            if billing._setup_needed(customer_id):
                delivery = hardware_delivery(customer_id, progress)
                body = _hardware_status_body(progress, delivery, summary) if delivery else _software_body(progress, summary)
            elif deferred and deferred["state"] == "payment_failed":
                # Activated, but the storage plan's first payment was refused.
                body = ('<p class="notice warn" role="status">Storage plan not started</p>'
                        '<h2 id="order-storage-failed">Your payment method was declined.</h2>' + summary +
                        '<p>Your appliance is activated. To start your monthly subscription, update your payment method in '
                        '<a href="/subscription-portal">My subscription</a> (Manage billing), then try again.</p>'
                        '<button class="submit" id="order-storage-retry" type="button">Try again</button>'
                        '<p id="order-message" class="note" role="status" aria-live="polite"></p>')
                script = ("const csrf=()=>{const m=document.cookie.split('; ').find(x=>x.startsWith('anyaicam_csrf='));if(!m)return '';"
                          "let v=decodeURIComponent(m.split('=').slice(1).join('='));return v.length>=2&&v[0]==='\"'&&v[v.length-1]==='\"'?v.slice(1,-1):v};"
                          "const b=document.getElementById('order-storage-retry');b.onclick=async()=>{b.disabled=true;"
                          "const r=await fetch('/api/v2/customer/storage/start',{method:'POST',headers:{'X-CSRF-Token':csrf()}});"
                          "const d=await r.json().catch(()=>({}));document.getElementById('order-message').textContent=d.message||d.detail||'';"
                          "if(r.ok)setTimeout(()=>location.reload(),1500);else b.disabled=false};")
            elif deferred and deferred["state"] not in ("started", "superseded") and not billing.has_camera_plan(customer_id):
                body = ('<h2 id="order-storage-starting">Your storage plan is starting.</h2>' + summary +
                        '<p>Your appliance is activated. Your monthly subscription is being set up with the card you paid with; '
                        'this usually takes a moment.</p><a class="submit" href="/customer-account">Open AnyAiCam</a>')
            else:
                body = ('<h2>Your AnyAiCam plan is active</h2>' + summary +
                        '<a class="submit" href="/customer-account">Open AnyAiCam</a>')
            return HTMLResponse(_page("Your order", body, script=script), headers=NO_STORE)
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
                               f"at {SUPPORT_PHONE} if your plan does not appear.") + "}")
        return HTMLResponse(_page("Activating your plan", '<h2>Payment received</h2>'
                                  '<p id="order-activating" role="status" aria-live="polite">Activating your AnyAiCam plan… '
                                  'This page updates automatically.</p>', script=script), headers=NO_STORE)
