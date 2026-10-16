"""Hardware-first checkout (owner decision 2026-10-15).

The customer pays for the physical AnyAiCam hardware first, in its own
Stripe Checkout, and buys the per-camera subscription separately later:

    Build Your System (anyaicam.com) -> /hardware-checkout (guest or signed in)
    -> Stripe Checkout (mode=payment: appliance + relay modules only)
    -> verified webhook records the paid order and emails the confirmation
    -> /hardware-order/complete -> create the account (or sign in)
    -> /customer/orders: Paid / Preparing / Shipped / Delivered
    -> after delivery: My subscription -> separate subscription checkout.

Rules this module keeps:

* Prices come only from the server catalog (hardware_orders.HARDWARE_CATALOG)
  and each Stripe Price is checked against it before a session is created
  (main.require_stripe_price_matches_catalog): a $0.50-$0.89 test Price or a
  missing Price ID refuses checkout. The browser sends only choices.
* Nothing recurring is set up here: no subscription line, no saved card for
  off-session use, no deferred storage plan. The camera subscription starts
  only when the customer buys it on My subscription.
* Only the signed Stripe webhook marks an order paid (payment_status=paid,
  including delayed methods via async_payment_succeeded). The success page
  reads state; it never grants anything.
* One order per (Checkout Session, SKU) -- hardware_orders.upsert_order -- so a
  redelivered or duplicated event never creates a second paid order; opening
  a new checkout for the same email expires the previous unpaid one, so two
  open tabs cannot both be paid.
* A signed-in purchase belongs to the signed-in account. A guest order is
  stored without an account and is linked only on proof that the buyer
  controls the order email: a verified direct signup with that email, or a
  sign-in to the account with that email from the order's own confirmation
  page (whose URL carries the unguessable Checkout Session id, sent only to
  the buyer and to the order email). Never by email match alone.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
from datetime import datetime
from html import escape
from urllib.parse import quote, urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from appliance_protocol import RateLimiter
from partner_db import connection, row, rows

logger = logging.getLogger("anyaicam.hardware_checkout")

CHECKOUT_PATH = "/hardware-checkout"
COMPLETE_PATH = "/hardware-order/complete"
ORDERS_PATH = "/customer/orders"
API_PATH = "/api/hardware-checkout"
HARDWARE_ONLY = "hardware_only"
# Accessory order (VMS 1.2.7, owner decision 2026-10-16): door-control relays bought from My subscription after
# activation, by the signed-in account owner only. Same Stripe flow, shipping, records and fulfilment as a hardware
# order; never an appliance, never a guest.
ACCESSORY_ONLY = "accessory_only"
RELAY_API_PATH = "/api/customer/relay-checkout"
APPLIANCE_PREFIX = "AIC-APPLIANCE-"
RELAY_SKU = "AIC-RELAY-NUMATO-3CH"
MAX_RELAY_MODULES = 16
SESSION_ID = re.compile(r"cs_(test|live)_[A-Za-z0-9]{8,200}")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
CONFIRMATION_KEY = "hardware-checkout-confirmed:"
PURCHASED_TODAY = ("Your AnyAiCam hardware is purchased today. Your camera subscription will be activated separately "
                   "after your equipment arrives and you complete setup.")
WAITING_FOR_DELIVERY = ("Your AnyAiCam system has been ordered. Your equipment is being prepared for shipment. You do not "
                        "need to activate or pay for your camera subscription yet. Return here when your equipment arrives.")
_create_limiter = RateLimiter(limit=10, window_seconds=900)

CSRF_JS = ("const csrf=()=>{const m=document.cookie.split('; ').find(x=>x.startsWith('anyaicam_csrf='));if(!m)return '';"
           "let v=decodeURIComponent(m.split('=').slice(1).join('='));return v.length>=2&&v[0]==='\"'&&v[v.length-1]==='\"'?v.slice(1,-1):v};")


def _now() -> str:
    return datetime.now().isoformat()


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _page(title: str, body: str, *, script: str = "", signed_in: bool = False) -> str:
    import order_funnel
    return order_funnel._page(title, body, script=script, signed_in=signed_in)


# ------------------------------------------------------------------ selection and quote

def _catalog() -> dict:
    import hardware_orders
    return {sku: {"name": name, "cents": cents, "env": env}
            for sku, _product, name, cents, env in hardware_orders.HARDWARE_CATALOG}


def _int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def selection_from(values) -> dict | None:
    """The hardware order from query/body values, or None when the appliance
    is not an orderable catalog appliance. Plan and camera count are kept
    only as the customer's intent for the later subscription; they are
    never charged here."""
    import per_camera_billing as billing
    get = values.get
    appliance = str(get("appliance") or "").strip()
    if not appliance.startswith(APPLIANCE_PREFIX) or appliance not in _catalog():
        return None
    relays = _int(get("relays"))
    if not 0 <= relays <= MAX_RELAY_MODULES:
        return None
    cameras = _int(get("cameras"))
    plan = str(get("plan") or "").strip()
    return {"appliance": appliance, "relays": relays,
            "cameras": cameras if billing.MIN_CAMERA_QUANTITY <= cameras <= billing.MAX_CAMERA_QUANTITY else None,
            "plan": plan if plan in billing.PLANS else None}


def selection_query(selection: dict) -> str:
    return urlencode({key: selection[key] for key in ("appliance", "relays", "cameras", "plan") if selection.get(key)})


def quote_for(selection: dict) -> dict:
    catalog = _catalog()
    lines = ([{"sku": selection["appliance"], "name": catalog[selection["appliance"]]["name"], "quantity": 1,
               "cents": catalog[selection["appliance"]]["cents"]}] if selection.get("appliance") else [])
    if selection.get("relays"):
        lines.append({"sku": RELAY_SKU, "name": catalog[RELAY_SKU]["name"], "quantity": int(selection["relays"]),
                      "cents": catalog[RELAY_SKU]["cents"]})
    return {"lines": lines, "total_cents": sum(line["cents"] * line["quantity"] for line in lines)}


# ------------------------------------------------------------------ records

def checkout_row(*, checkout_id: str | None = None, session_id: str | None = None) -> dict | None:
    if checkout_id:
        return row("SELECT * FROM hardware_checkouts WHERE id=?", (checkout_id,))
    if session_id:
        return row("SELECT * FROM hardware_checkouts WHERE stripe_checkout_session_id=?", (session_id,))
    return None


def _orders_for_session(session_id: str) -> list:
    return rows("SELECT * FROM hardware_orders WHERE stripe_checkout_session_id=? ORDER BY sku LIKE 'AIC-APPLIANCE-%' DESC, created_at",
                (session_id,))


def _identity(request: Request) -> dict | None:
    from partner_portal import partner_identity
    identity = partner_identity(request)
    if identity and identity.get("customer_id") and identity.get("role") in {"customer_owner", "customer_viewer"}:
        return identity
    return None


def _account_for_email(email: str) -> bool:
    email = (email or "").strip().lower()
    return bool(email and row("SELECT id FROM partner_users WHERE lower(email)=? AND role='customer_owner' LIMIT 1", (email,)))


def _account_email(customer_id: str) -> str:
    customer = row("SELECT email FROM customers WHERE id=?", (customer_id,)) or {}
    return str(customer.get("email") or "").strip().lower()


def link_paid_orders(customer_id: str, email: str, *, session_id: str | None = None) -> int:
    """Attach paid guest hardware orders to the account. Call only on proof
    that the account owner controls this email: right after a verified
    signup (all of the email's guest orders), or a sign-in from one order's
    confirmation page (session_id: that order only). Returns how many
    checkouts were linked."""
    email = (email or "").strip().lower()
    if not customer_id or not email:
        return 0
    linked = 0
    now = _now()
    query = ("SELECT id,stripe_checkout_session_id FROM hardware_checkouts WHERE lower(email)=? AND customer_id IS NULL "
             "AND status='paid'" + (" AND stripe_checkout_session_id=?" if session_id else ""))
    with connection() as db:
        for checkout in db.execute(query, (email, session_id) if session_id else (email,)).fetchall():
            db.execute("UPDATE hardware_orders SET customer_id=?,updated_at=? WHERE stripe_checkout_session_id=? AND customer_id IS NULL",
                       (customer_id, now, checkout["stripe_checkout_session_id"]))
            db.execute("UPDATE hardware_checkouts SET customer_id=?,linked_at=?,updated_at=? WHERE id=? AND customer_id IS NULL",
                       (customer_id, now, now, checkout["id"]))
            linked += 1
    return linked


# ------------------------------------------------------------------ checkout

def _expire_previous(email: str, keep: str) -> None:
    """An earlier unpaid checkout for the same email is closed so it cannot
    also be paid (two tabs, a back button, a repeated click)."""
    import main
    for previous in rows("SELECT id,stripe_checkout_session_id FROM hardware_checkouts WHERE lower(email)=? AND status='open' AND id<>?",
                         (email, keep)):
        session_id = previous["stripe_checkout_session_id"]
        try:
            if session_id:
                main.stripe_api_post(f"/v1/checkout/sessions/{session_id}/expire", [])
            state = "expired"
        except Exception as error:
            # Already completed or expired on Stripe: the webhook records the outcome.
            logger.info("hardware_checkout.previous_not_expired id=%s reason=%s", previous["id"], type(error).__name__)
            continue
        with connection() as db:
            db.execute("UPDATE hardware_checkouts SET status=?,updated_at=? WHERE id=? AND status='open'", (state, _now(), previous["id"]))


def create_checkout(selection: dict, *, email: str, identity: dict | None) -> dict:
    import checkout_guard
    import main
    base = main.PUBLIC_BASE_URL
    if not base:
        raise HTTPException(status_code=503, detail="Online hardware ordering is not available right now.")
    catalog = _catalog()
    quote_ = quote_for(selection)
    fields: list[tuple[str, str]] = [("mode", "payment")]
    items = []
    for index, line in enumerate(quote_["lines"]):
        entry = catalog[line["sku"]]
        price_id = os.environ.get(entry["env"], "").strip()
        if not price_id:
            raise HTTPException(status_code=503, detail=f"Online ordering for the {entry['name']} is not available yet.")
        main.require_stripe_price_matches_catalog(price_id, entry["cents"], None)
        fields += [(f"line_items[{index}][price]", price_id), (f"line_items[{index}][quantity]", str(line["quantity"]))]
        items.append(f"{line['sku']}:{line['quantity']}")
    checkout_id = secrets.token_hex(16)
    customer_id = identity["customer_id"] if identity else None
    accessory = not selection.get("appliance")
    if accessory and not customer_id:
        raise HTTPException(status_code=403, detail="Sign in to your AnyAiCam account to order relays.")
    metadata = {"anyaicam_build_order": ACCESSORY_ONLY if accessory else HARDWARE_ONLY, "anyaicam_hardware_checkout_id": checkout_id,
                "anyaicam_hardware_items": ",".join(items)}
    if customer_id:
        metadata["anyaicam_customer_id"] = customer_id
    if selection.get("plan"):
        metadata["anyaicam_intended_plan"] = selection["plan"]
    if selection.get("cameras"):
        metadata["anyaicam_intended_cameras"] = str(selection["cameras"])
    for key, value in metadata.items():
        fields += [(f"metadata[{key}]", value), (f"payment_intent_data[metadata][{key}]", value)]
    fields += [
        ("success_url", f"{base}{ORDERS_PATH}?relay_payment=success" if accessory
         else f"{base}{COMPLETE_PATH}?session_id={{CHECKOUT_SESSION_ID}}"),
        ("cancel_url", "{0}/subscription-portal?relay_payment=cancelled#v2-relays".format(base) if accessory
         else f"{base}{CHECKOUT_PATH}?{selection_query(selection)}&checkout=cancelled"),
        ("client_reference_id", customer_id or checkout_id),
        ("shipping_address_collection[allowed_countries][0]", "US"),
        ("phone_number_collection[enabled]", "true"),
        ("allow_promotion_codes", "false"),
        ("payment_intent_data[description]", "AnyAiCam relay order" if accessory else "AnyAiCam hardware order"),
    ]
    if customer_id:
        fields.append(("customer", checkout_guard.canonical_stripe_customer(customer_id, email=email)))
    else:
        fields += [("customer_email", email), ("customer_creation", "always")]
    now = _now()
    with connection() as db:
        db.execute("INSERT INTO hardware_checkouts(id,customer_id,email,items,intended_plan,intended_cameras,status,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (checkout_id, customer_id, email, ",".join(items), selection.get("plan"), selection.get("cameras"), "creating", now, now))
    _expire_previous(email, checkout_id)
    try:
        session = main.stripe_api_post("/v1/checkout/sessions", fields, idempotency_key=f"anyaicam-hardware-checkout-{checkout_id}")
    except Exception as error:
        with connection() as db:
            db.execute("UPDATE hardware_checkouts SET status='failed',updated_at=? WHERE id=?", (_now(), checkout_id))
        logger.warning("hardware_checkout.session_not_created reason=%s", type(error).__name__)
        raise HTTPException(status_code=502, detail="Secure checkout could not be opened. Please try again.")
    if not session.get("id") or not session.get("url"):
        raise HTTPException(status_code=502, detail="Stripe did not return a checkout page.")
    with connection() as db:
        db.execute("UPDATE hardware_checkouts SET stripe_checkout_session_id=?,status='open',updated_at=? WHERE id=?",
                   (session["id"], _now(), checkout_id))
    return {"checkout_url": session["url"], "session_id": session["id"]}


# ------------------------------------------------------------------ webhook step

def _shipping(session: dict) -> dict:
    details = ((session.get("collected_information") or {}).get("shipping_details") or session.get("shipping_details") or {})
    address = details.get("address") or {}
    return {"name": details.get("name"), "line1": address.get("line1"), "line2": address.get("line2"), "city": address.get("city"),
            "state": address.get("state"), "postal_code": address.get("postal_code"), "country": address.get("country"),
            "phone": (session.get("customer_details") or {}).get("phone")}


def sync_from_stripe_event(event: dict) -> dict:
    """Webhook step for hardware-only orders. Raises only to make Stripe
    redeliver (a Stripe read failed while recording a paid order)."""
    import build_orders
    import hardware_orders
    event_type = str(event.get("type") or "")
    session = (event.get("data") or {}).get("object") or {}
    metadata = session.get("metadata") or {}
    kind = str(metadata.get("anyaicam_build_order") or "")
    if kind not in (HARDWARE_ONLY, ACCESSORY_ONLY):
        return {"status": "ignored"}
    checkout_id = str(metadata.get("anyaicam_hardware_checkout_id") or "")
    record = checkout_row(checkout_id=checkout_id) if checkout_id else None
    if not record:
        return {"status": "rejected", "reason": "unknown hardware checkout"}
    if event_type in {"checkout.session.expired", "checkout.session.async_payment_failed"}:
        with connection() as db:
            db.execute("UPDATE hardware_checkouts SET status=?,updated_at=? WHERE id=? AND status IN ('open','creating','processing')",
                       ("expired" if event_type.endswith("expired") else "payment_failed", _now(), checkout_id))
        return {"status": "closed"}
    if event_type not in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        return {"status": "ignored"}
    if str(session.get("mode") or "") != "payment":
        return {"status": "rejected", "reason": "a hardware order is a one-time payment"}
    session_id = str(session.get("id") or "")
    if record.get("stripe_checkout_session_id") and record["stripe_checkout_session_id"] != session_id:
        return {"status": "rejected", "reason": "session does not belong to this checkout"}
    if session.get("payment_status") != "paid":
        with connection() as db:  # e.g. a bank debit still clearing: async_payment_succeeded records it
            db.execute("UPDATE hardware_checkouts SET status='processing',stripe_checkout_session_id=COALESCE(stripe_checkout_session_id,?),"
                       "updated_at=? WHERE id=? AND status IN ('open','creating')", (session_id, _now(), checkout_id))
        return {"status": "not_granted", "reason": "payment has not cleared"}
    catalog = _catalog()
    items = []
    for part in str(metadata.get("anyaicam_hardware_items") or "").split(","):
        sku, _, quantity = part.partition(":")
        if sku in catalog and quantity.isdigit() and int(quantity) >= 1:
            items.append((sku, int(quantity)))
    if kind == ACCESSORY_ONLY:
        # Relays only, for a signed-in account that still exists; never an appliance through this path.
        if not items or any(sku != RELAY_SKU for sku, _ in items):
            return {"status": "rejected", "reason": "an accessory order holds relays only"}
        if not row("SELECT id FROM customers WHERE id=?", (str(metadata.get("anyaicam_customer_id") or ""),)):
            return {"status": "rejected", "reason": "an accessory order needs its account"}
    elif not items or not items[0][0].startswith(APPLIANCE_PREFIX):
        return {"status": "rejected", "reason": "no appliance in this order"}
    try:
        amounts = build_orders._line_amounts(session)
    except build_orders.RetryableBuildOrderError:
        raise
    email = str((session.get("customer_details") or {}).get("email") or session.get("customer_email") or record.get("email") or "").strip().lower()
    # Signed-in purchases only; a guest's order waits for proof (link_paid_orders).
    customer_id = str(metadata.get("anyaicam_customer_id") or "") or None
    if customer_id and not row("SELECT id FROM customers WHERE id=?", (customer_id,)):
        customer_id = None
    intent = session.get("payment_intent")
    intent_id = str((intent.get("id") if isinstance(intent, dict) else intent) or "") or None
    stripe_customer = str(session.get("customer") or "") or None
    order_ids = []
    for sku, quantity in items:
        entry = catalog[sku]
        price_id = os.environ.get(entry["env"], "").strip()
        charged = amounts.get(price_id)
        order = hardware_orders.upsert_order(
            customer_id=customer_id, sku=sku, product_name=entry["name"], stripe_price_id=price_id or "unconfigured",
            quantity=quantity, amount_cents=charged if charged is not None else entry["cents"] * quantity,
            stripe_checkout_session_id=session_id, stripe_payment_intent_id=intent_id, stripe_customer_id=stripe_customer,
            status="paid")
        order_ids.append(order["id"])
    now = _now()
    with connection() as db:
        db.execute("UPDATE hardware_checkouts SET status='paid',stripe_checkout_session_id=?,email=?,customer_id=COALESCE(customer_id,?),"
                   "amount_total_cents=?,stripe_payment_intent_id=?,stripe_customer_id=?,shipping_json=?,order_ids=?,"
                   "paid_at=COALESCE(paid_at,?),updated_at=? WHERE id=?",
                   (session_id, email or record.get("email"), customer_id, session.get("amount_total"), intent_id, stripe_customer,
                    json.dumps(_shipping(session)), ",".join(order_ids), now, now, checkout_id))
    try:  # the confirmation email never fails the step; retried from the outbox and on the order pages
        import stripe_mode
        send_confirmation(checkout_id, test_mode=stripe_mode.is_test_mode(event=event))
    except Exception:
        logger.exception("hardware_checkout.confirmation_not_sent")
    return {"status": "hardware_order_recorded", "order_ids": order_ids, "customer_id": customer_id}


# ------------------------------------------------------------------ confirmation email

def _order_numbers(order_ids: list[str]) -> str:
    from hardware_fulfillment import generate_order_number
    return ", ".join(generate_order_number(order_id) for order_id in order_ids)


def send_confirmation(checkout_id: str, *, test_mode: bool | None = None) -> dict:
    import purchase_notifications as pn
    record = checkout_row(checkout_id=checkout_id)
    if not record or record["status"] != "paid":
        return {"status": "ignored"}
    orders = _orders_for_session(record["stripe_checkout_session_id"])
    if not orders:
        return {"status": "ignored"}
    lines = [f"{order['product_name']}{' x ' + str(order['quantity']) if int(order['quantity']) > 1 else ''}: "
             f"{_money(int(order['amount_cents']))}" for order in orders]
    number = _order_numbers([order["id"] for order in orders[:1]])
    base = _public_base()
    if record.get("customer_id"):
        next_step, link = "Sign in to see your order status.", f"{base}/customer-login.html?next={quote(ORDERS_PATH, safe='')}"
    else:
        next_step = (f"Create your AnyAiCam account with this email address ({record['email']}), or sign in to your existing "
                     "account, from your order page to follow your order and set up your system when it arrives.")
        link = f"{base}{COMPLETE_PATH}?session_id={record['stripe_checkout_session_id']}"
    paragraphs = [f"Thank you for your order. Your payment is confirmed (order {number}).", *lines,
                  f"Total charged: {_money(int(record.get('amount_total_cents') or sum(int(o['amount_cents']) for o in orders)))}",
                  PURCHASED_TODAY, "We'll email you when your equipment ships.", next_step]
    subject = "Your AnyAiCam hardware order is confirmed"
    text = "Hello,\n\n" + "\n\n".join(paragraphs) + f"\n\n{link}\n\n{pn._SUPPORT_FOOTER_TEXT}"
    html = ("<p>Hello,</p>" + "".join(f"<p>{escape(p)}</p>" for p in paragraphs)
            + f'<p><a href="{escape(link, quote=True)}">{"View your order" if record.get("customer_id") else "Open your order"}</a></p>'
            + pn._SUPPORT_FOOTER_HTML)
    return pn._send_once(event_id=CONFIRMATION_KEY + checkout_id, notification_type="hardware_order_confirmation",
                         customer_id=record.get("customer_id"), recipient_email=str(record.get("email") or ""), subject=subject,
                         text=text, html=html, metadata={"hardware_checkout_id": checkout_id}, test_mode=test_mode)


def retry_confirmations(limit: int = 50) -> int:
    sent = 0
    for record in rows("SELECT h.id FROM hardware_checkouts h WHERE h.status='paid' AND NOT EXISTS (SELECT 1 FROM provisioning_notifications n "
                       "WHERE n.stripe_event_id=('hardware-checkout-confirmed:' || h.id) AND n.status='sent') LIMIT ?", (limit,)):
        try:
            if send_confirmation(record["id"]).get("status") == "sent":
                sent += 1
        except Exception:
            continue
    return sent


def _public_base() -> str:
    import main
    return main.PUBLIC_BASE_URL or ""


# ------------------------------------------------------------------ pages

STATUS_LABELS = {"paid": "Paid", "preparing": "Preparing", "configuring": "Preparing", "testing": "Preparing",
                 "ready_to_ship": "Preparing", "shipped": "Shipped", "delivered": "Delivered", "cancelled": "Cancelled"}


def order_status_label(order: dict) -> str:
    if order.get("status") == "refunded":
        return "Refunded"
    if order.get("status") == "disputed":
        return "Payment disputed"
    if order.get("status") != "paid":
        return "Payment pending"
    return STATUS_LABELS.get(str(order.get("fulfillment_status") or "paid"), "Preparing")


def _lines_html(lines: list[dict]) -> str:
    return "".join(f'<div class="order-line"><span>{escape(line["name"])}{" × " + str(line["quantity"]) if line["quantity"] > 1 else ""}</span>'
                   f'<span>{_money(line["cents"] * line["quantity"])}</span></div>' for line in lines)


def _checkout_page(selection: dict, identity: dict | None, *, cancelled: bool) -> str:
    quote_ = quote_for(selection)
    email = _account_email(identity["customer_id"]) if identity else ""
    intent = ""
    if selection.get("plan") or selection.get("cameras"):
        import per_camera_billing as billing
        plan = billing.PLANS.get(selection.get("plan") or "")
        parts = [plan["display_name"] if plan else None,
                 f"{selection['cameras']} camera{'s' if selection['cameras'] != 1 else ''}" if selection.get("cameras") else None]
        intent = (f'<p class="note" id="hw-plan-later">Camera plan you chose ({escape(" · ".join(p for p in parts if p))}): '
                  'not charged today. You choose and start it on My subscription after your equipment arrives.</p>')
    notice = ('<p class="notice" id="hw-cancelled" role="status">Checkout was cancelled. No payment was taken.</p>' if cancelled else "")
    if identity:
        who = (f'<p class="note" id="hw-account">Ordering as {escape(email)}. Your order will appear in your account.</p>'
               f'<input type="hidden" id="hw-email" value="{escape(email, quote=True)}">')
    else:
        sign_in = f"/customer-login.html?next={quote(CHECKOUT_PATH + '?' + selection_query(selection), safe='')}"
        who = ('<label>Email for your order confirmation<input id="hw-email" type="email" autocomplete="email" required></label>'
               f'<p class="note">Already have an AnyAiCam account? <a href="{escape(sign_in, quote=True)}">Sign in first</a>. '
               'Otherwise you create your account after paying, with this email address.</p>')
    body = ('<h2>Your AnyAiCam hardware</h2>' + notice + _lines_html(quote_["lines"]) +
            '<div class="order-line" id="order-license-included"><span>AnyAiCam VMS Software License</span><span>FREE, included with your PC ($0.00)</span></div>'
            f'<div class="order-line order-total" id="hw-total"><span>Due today</span><span>{_money(quote_["total_cents"])}</span></div>'
            f'<p class="notice ok" id="hw-purchased-today">{escape(PURCHASED_TODAY)}</p>' + intent + who +
            '<p class="note">Stripe collects your shipping address and shows the final amount, including any tax, before you pay.</p>'
            '<button class="submit" id="hw-pay" type="button">Pay for hardware</button>'
            '<p id="hw-message" class="note" role="alert" aria-live="assertive"></p>'
            '<a class="ghost" href="https://anyaicam.com/build-your-system.html">Back to Build Your System</a>')
    payload = json.dumps({key: selection.get(key) for key in ("appliance", "relays", "cameras", "plan")})
    script = (CSRF_JS + f"const hwOrder={payload};const pay=document.getElementById('hw-pay'),msg=document.getElementById('hw-message');"
              "const reset=()=>{pay.disabled=false;pay.textContent='Pay for hardware'};window.addEventListener('pageshow',reset);"
              "const detail=b=>{const d=b&&b.detail;if(typeof d==='string')return d;if(Array.isArray(d)&&d.length)return 'Please check your email address and try again.';return ''};"
              "pay.onclick=async()=>{const email=document.getElementById('hw-email').value.trim();msg.textContent='';"
              "if(!/^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/.test(email)){msg.textContent='Enter the email address for your order confirmation.';return}"
              "if(pay.disabled)return;pay.disabled=true;pay.textContent='Opening secure checkout…';let r=null,b={};"
              "try{r=await fetch('" + API_PATH + "',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},"
              "body:JSON.stringify(Object.assign({},hwOrder,{email}))});b=await r.json().catch(()=>({}))}catch(e){r=null}"
              "if(r&&r.ok&&b.checkout_url){location.href=b.checkout_url;return}reset();"
              "msg.textContent=detail(b)||(r?'Secure checkout could not be opened (error '+r.status+'). Please try again.':'Could not reach AnyAiCam. Check your connection and try again.');"
              "msg.scrollIntoView({block:'center'})};")
    return _page("Your hardware order", body, script=script, signed_in=bool(identity))


def _complete_page(record: dict | None, session_state: str, identity: dict | None) -> tuple[str, int]:
    if record and record["status"] == "paid":
        orders = _orders_for_session(record["stripe_checkout_session_id"])
        lines = "".join(f'<div class="order-line"><span>{escape(o["product_name"])}{" × " + str(o["quantity"]) if int(o["quantity"]) > 1 else ""}</span>'
                        f'<span>{_money(int(o["amount_cents"]))}</span></div>' for o in orders)
        number = escape(_order_numbers([o["id"] for o in orders[:1]])) if orders else ""
        here = f"{COMPLETE_PATH}?session_id={record['stripe_checkout_session_id']}"
        email = escape(str(record.get("email") or ""))
        if record.get("customer_id"):
            action = f'<a class="submit" id="hw-view-order" href="{ORDERS_PATH}">View your order</a>'
        elif identity:
            action = (f'<p class="notice warn" id="hw-other-account">This order was placed with {email}. Sign in with that email '
                      'address to add it to your account.</p>')
        elif _account_for_email(str(record.get("email") or "")):
            action = (f'<p id="hw-sign-in-to-link">You already have an AnyAiCam account with <strong>{email}</strong>. '
                      'Sign in to add this order to it.</p>'
                      f'<a class="submit" id="hw-signin" href="/customer-login.html?next={quote(here, safe="")}">Sign in</a>')
        else:
            action = (f'<p id="hw-create-account">Next, create your AnyAiCam account with <strong>{email}</strong>. '
                      'After you confirm your email, your order is added to your account.</p>'
                      f'<a class="submit" id="hw-signup" href="/customer-signup?next={quote(ORDERS_PATH, safe="")}">Create your account</a>'
                      f'<a class="ghost" href="/customer-login.html?next={quote(here, safe="")}">I already have an account — sign in</a>')
        body = ('<p class="notice ok" role="status">Payment complete · Order confirmed</p>'
                f'<h2 id="hw-confirmed">Thank you — order {number} is confirmed.</h2>' + lines +
                f'<p class="note" id="hw-confirmation-email">A confirmation has been emailed to {escape(str(record.get("email") or ""))}.</p>'
                f'<p id="hw-next">{escape(PURCHASED_TODAY)}</p>' + action)
        return _page("Order confirmed", body, signed_in=bool(identity)), 200
    if session_state == "not_paid":
        return _page("Payment not completed", '<h2>Payment not completed</h2><p>Stripe has not confirmed this payment, so no order was '
                     'placed. You can try again.</p><a class="submit" href="https://anyaicam.com/build-your-system.html">Back to Build Your System</a>',
                     signed_in=bool(identity)), 200
    if session_state == "foreign":
        return _page("Order not found", "<h2>We couldn't find this order</h2>", signed_in=bool(identity)), 404
    script = ("let n=0;try{n=Number(sessionStorage.getItem('hwConfirm')||0)}catch(e){}"
              "if(n<20){try{sessionStorage.setItem('hwConfirm',String(n+1))}catch(e){}setTimeout(()=>location.reload(),3000)}"
              "else{const s=document.getElementById('hw-confirming');if(s)s.textContent='Confirmation is taking longer than usual. "
              "Your payment is safe; check your email for the order confirmation or refresh this page in a minute.'}")
    return _page("Confirming your payment", '<h2>Payment received</h2><p id="hw-confirming" role="status" aria-live="polite">'
                 'Confirming your order… This page updates automatically.</p>', script=script, signed_in=bool(identity)), 200


def _session_state(session_id: str, record: dict | None) -> str:
    """Read-only Stripe look: 'paid' (webhook still confirming), 'not_paid',
    'unknown', or 'foreign'. Grants nothing."""
    if not SESSION_ID.fullmatch(session_id or ""):
        return "foreign"
    if not record:
        return "foreign"
    try:
        import main
        session = main.stripe_api_get(f"/v1/checkout/sessions/{session_id}")
    except Exception:
        return "unknown"
    if str((session.get("metadata") or {}).get("anyaicam_hardware_checkout_id") or "") != record["id"]:
        return "foreign"
    if session.get("status") == "complete":
        return "paid"
    return "not_paid"


def _orders_page(customer_id: str) -> str:
    orders = rows("SELECT * FROM hardware_orders WHERE customer_id=? ORDER BY created_at DESC", (customer_id,))
    if not orders:
        return ('<h2>My orders</h2><p id="orders-none">You have no hardware orders.</p>'
                '<a class="submit" href="https://anyaicam.com/build-your-system.html">Build your system</a>'
                '<a class="ghost" href="/customer-account">Open AnyAiCam</a>')
    import order_funnel
    from hardware_fulfillment import generate_order_number
    cards, delivered, waiting = "", False, False
    for order in orders:
        label = order_status_label(order)
        if order["sku"].startswith(APPLIANCE_PREFIX) and order.get("status") == "paid":
            delivered = delivered or label == "Delivered"
            waiting = waiting or label in ("Paid", "Preparing", "Shipped")
        tracking = order_funnel._shipment_lines(order) if label in ("Shipped", "Delivered") else ""
        quantity = f" × {order['quantity']}" if int(order["quantity"]) > 1 else ""
        cards += (f'<div class="order-line hw-order" data-status="{escape(label, quote=True)}"><span>{escape(order["product_name"])}{quantity}'
                  f'<br><small class="note">Order {escape(generate_order_number(order["id"]))} · {escape(str(order["created_at"])[:10])}</small></span>'
                  f'<span class="hw-status">{escape(label)}</span></div>{tracking}')
    if waiting and not delivered:
        guide = (f'<p class="notice ok" id="orders-waiting" role="status">{escape(WAITING_FOR_DELIVERY)}</p>'
                 '<p class="note">Order statuses: Paid → Preparing → Shipped → Delivered. We email you when your equipment ships.</p>')
    elif delivered:
        guide = ('<p class="notice ok" id="orders-delivered" role="status">Your equipment has arrived.</p>'
                 '<p>Choose your camera plan and number of cameras on My subscription. That is a separate checkout, and billing '
                 'starts only when you complete it. Then set up your appliance and add your cameras.</p>'
                 '<a class="submit" id="orders-choose-plan" href="/subscription-portal#v2-checkout">Choose my camera plan</a>'
                 '<a class="ghost" id="orders-setup" href="/customer/setup">Set up my system</a>')
    else:
        guide = ""
    return '<h2>My orders</h2>' + guide + cards + '<a class="ghost" href="/customer-account">Open AnyAiCam</a>'


def register_hardware_checkout_routes(app: FastAPI) -> None:
    @app.get(CHECKOUT_PATH, response_class=HTMLResponse)
    def hardware_checkout_page(request: Request):
        selection = selection_from(request.query_params)
        identity = _identity(request)
        if not selection:
            return HTMLResponse(_page("Choose your hardware", '<h2>Choose your AnyAiCam system</h2><p>Pick an AnyAiCam appliance in '
                                      'Build Your System to order it here.</p><a class="submit" href="https://anyaicam.com/build-your-system.html">'
                                      'Build Your System</a>', signed_in=bool(identity)), status_code=400, headers=NO_STORE)
        if identity and identity.get("role") != "customer_owner":
            return HTMLResponse(_page("Account owner required", '<h2>Only the account owner can order hardware</h2>', signed_in=True),
                                status_code=403, headers=NO_STORE)
        cancelled = request.query_params.get("checkout") == "cancelled"
        return HTMLResponse(_checkout_page(selection, identity, cancelled=cancelled), headers=NO_STORE)

    @app.post(API_PATH)
    async def hardware_checkout_api(request: Request) -> dict:
        client_ip = request.client.host if request.client else "unknown"
        if not _create_limiter.allow(client_ip):
            raise HTTPException(status_code=429, detail="Too many checkout attempts. Please wait a few minutes and try again.")
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid request.")
        if not isinstance(payload, dict) or set(payload) - {"appliance", "relays", "cameras", "plan", "email"}:
            raise HTTPException(status_code=422, detail="Invalid request.")
        selection = selection_from(payload)
        if not selection:
            raise HTTPException(status_code=422, detail="Choose an AnyAiCam appliance in Build Your System first.")
        identity = _identity(request)
        if identity and identity.get("role") != "customer_owner":
            raise HTTPException(status_code=403, detail="Only the account owner can order hardware.")
        email = _account_email(identity["customer_id"]) if identity else str(payload.get("email") or "").strip().lower()
        if not EMAIL.fullmatch(email) or len(email) > 254:
            raise HTTPException(status_code=422, detail="Enter a valid email address for your order confirmation.")
        return create_checkout(selection, email=email, identity=identity)

    @app.post(RELAY_API_PATH)
    async def relay_checkout_api(request: Request) -> dict:
        """Door-control relays from My subscription: account owner only, after activation, quantity 1-16, priced from
        the server catalog; one open checkout per account email (_expire_previous) and one paid order per session."""
        client_ip = request.client.host if request.client else "unknown"
        if not _create_limiter.allow(client_ip):
            raise HTTPException(status_code=429, detail="Too many checkout attempts. Please wait a few minutes and try again.")
        identity = _identity(request)
        if not identity or identity.get("role") != "customer_owner" or not identity.get("customer_id"):
            raise HTTPException(status_code=403, detail="Only the account owner can order relays.")
        import per_camera_billing
        per_camera_billing.require_activated(identity["customer_id"])
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid request.")
        if not isinstance(payload, dict) or set(payload) - {"quantity"}:
            raise HTTPException(status_code=422, detail="Invalid request.")
        quantity = payload.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_RELAY_MODULES:
            raise HTTPException(status_code=422, detail=f"Choose from 1 to {MAX_RELAY_MODULES} relays.")
        return create_checkout({"relays": quantity}, email=_account_email(identity["customer_id"]), identity=identity)

    @app.get(COMPLETE_PATH, response_class=HTMLResponse)
    def hardware_order_complete(request: Request, session_id: str = ""):
        identity = _identity(request)
        record = checkout_row(session_id=session_id) if SESSION_ID.fullmatch(session_id or "") else None
        if record and record["status"] == "paid":
            # Proof for a guest order: signed in to the account with the order
            # email, holding this order's own session id.
            if (identity and identity.get("role") == "customer_owner" and not record.get("customer_id")
                    and str(identity.get("email") or "").strip().lower() == str(record.get("email") or "").lower()):
                link_paid_orders(identity["customer_id"], record["email"], session_id=session_id)
                record = checkout_row(session_id=session_id)
            try:
                send_confirmation(record["id"])  # no-op once sent; a retry if the webhook's send failed
            except Exception:
                logger.exception("hardware_checkout.confirmation_retry_failed")
            state = "paid"
        else:
            state = _session_state(session_id, record)
        page, status = _complete_page(record, state, identity)
        return HTMLResponse(page, status_code=status, headers=NO_STORE)

    @app.get(ORDERS_PATH, response_class=HTMLResponse)
    def customer_orders(request: Request):
        identity = _identity(request)
        if not identity:
            return RedirectResponse(f"/customer-login.html?next={quote(ORDERS_PATH, safe='')}", status_code=303)
        notice = ('<p class="notice ok" id="orders-relay-paid" role="status">Payment received. Your relay order appears below as soon '
                  'as Stripe confirms it; refresh in a moment if it is not there yet.</p>'
                  if request.query_params.get("relay_payment") == "success" else "")
        return HTMLResponse(_page("My orders", notice + _orders_page(identity["customer_id"]), signed_in=True), headers=NO_STORE)
