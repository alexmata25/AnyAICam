"""Friends & Family: an administrator-approved discount, never a promo code.

Flow (2026-09-30):
1. A signed-in customer owner presses "Request Friends & Family discount"
   while buying (POST /api/customer/friends-family/request).
2. A pending request is stored and the customer sees "Pending
   administrator approval". Plan and analytics checkout is held while the
   request is pending, so the price is settled BEFORE any Stripe Checkout
   Session exists (main.py's checkout routes call checkout_discount()).
3. The platform administrator is emailed a Review Request link. Opening it
   (GET) only shows the request; approving or declining is a separate,
   CSRF-protected POST that requires a live global administrator grant.
4. The customer's page polls the status and shows Approved/Declined.
5. Approved: base plans 50% off, analytics packages/Talk Down 25% off,
   hardware 0% (pricing_catalog.FRIENDS_FAMILY_PERCENT_OFF). Applied as
   server-side Stripe coupons on that customer's own Checkout Sessions --
   there is no code a customer could share -- and customer promotion
   codes are switched off on those sessions, so discounts never stack.
   Salesperson commission on the customer's sales is $0
   (sales_commissions.py).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from html import escape
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

import pricing_catalog
from partner_db import connection, row, rows

PENDING, APPROVED, DECLINED = "pending", "approved", "declined"
ADMIN_EMAIL_ENV = "ANYAICAM_FRIENDS_FAMILY_ADMIN_EMAIL"


def _now() -> str:
    return datetime.now().isoformat()


def latest_request(customer_id: str) -> Optional[dict]:
    return row(
        "SELECT * FROM friends_family_requests WHERE customer_id=? ORDER BY requested_at DESC LIMIT 1",
        (customer_id,),
    )


def status_for(customer_id: str) -> str:
    request = latest_request(customer_id)
    return request["status"] if request else "none"


def is_approved(customer_id: str) -> bool:
    return status_for(customer_id) == APPROVED


_DISCOUNTED_WHAT = {"base": "your camera plan", "analytics": "analytics packages and Talk Down"}
_UNDISCOUNTED_WHAT = {"hardware": "Hardware", "vms_license": "VMS software licenses", "face_access": "Face Access"}


def approved_message() -> str:
    """What an approved customer is told, built from the same percentages
    checkout applies, so the two can never disagree."""
    percent = pricing_catalog.FRIENDS_FAMILY_PERCENT_OFF
    off = [f"{percent[c]}% off {what}" for c, what in _DISCOUNTED_WHAT.items() if percent.get(c)]
    full = [what for c, what in _UNDISCOUNTED_WHAT.items() if not percent.get(c)]
    message = "Approved: " + " and ".join(off) + "." if off else "Approved."
    if full:
        listed = full[0] if len(full) == 1 else ", ".join(full[:-1]) + " and " + full[-1]
        message += f" {listed} {'is' if len(full) == 1 else 'are'} not discounted."
    return message


def customer_status(customer_id: str) -> dict:
    request = latest_request(customer_id)
    if not request:
        return {"status": "none"}
    return {
        "status": request["status"], "request_id": request["id"],
        "requested_at": request["requested_at"], "decided_at": request["decided_at"],
        "percent_off": dict(pricing_catalog.FRIENDS_FAMILY_PERCENT_OFF),
        "approved_message": approved_message(),
    }


def create_request(*, customer_id: str, email: str, note: str = "") -> tuple[dict, bool]:
    """(request, created). A pending or approved request is returned as is,
    never duplicated; after a decline the customer may ask again."""
    current = latest_request(customer_id)
    if current and current["status"] in (PENDING, APPROVED):
        return current, False
    request_id = uuid.uuid4().hex
    with connection() as db:
        db.execute(
            "INSERT INTO friends_family_requests(id,customer_id,requested_by_email,status,customer_note,requested_at) "
            "VALUES(?,?,?,?,?,?)",
            (request_id, customer_id, (email or "").strip().lower(), PENDING, (note or "").strip()[:500], _now()),
        )
    return row("SELECT * FROM friends_family_requests WHERE id=?", (request_id,)), True


def decide(request_id: str, *, approve: bool, decided_by: str, note: str = "") -> dict:
    request = row("SELECT * FROM friends_family_requests WHERE id=?", (request_id,))
    if not request:
        raise LookupError("Request not found.")
    if request["status"] != PENDING:
        raise ValueError(f"This request was already {request['status']}.")
    with connection() as db:
        db.execute(
            "UPDATE friends_family_requests SET status=?,decided_at=?,decided_by=?,decision_note=? WHERE id=? AND status=?",
            (APPROVED if approve else DECLINED, _now(), decided_by, (note or "").strip()[:500], request_id, PENDING),
        )
    return row("SELECT * FROM friends_family_requests WHERE id=?", (request_id,))


class CheckoutHeld(Exception):
    """Raised while a Friends & Family request is pending: the price is not
    settled yet, so no Checkout Session may be created."""


def checkout_discount(customer_id: str, discount_class: str) -> dict:
    """What a plan/analytics Checkout Session for this customer must carry.
    {"coupon": <Stripe coupon id or None>, "allow_promotion_codes": bool,
     "friends_family": bool}. Raises CheckoutHeld while pending, and a
    RuntimeError if approved but the coupon is not configured (fail closed:
    never silently charge full price to an approved customer)."""
    state = status_for(customer_id)
    if state == PENDING:
        raise CheckoutHeld("Your Friends & Family request is waiting for administrator approval. "
                           "You can check out as soon as it is decided.")
    percent = pricing_catalog.friends_family_percent(discount_class)
    if state != APPROVED or percent == 0:
        return {"coupon": None, "allow_promotion_codes": True, "friends_family": state == APPROVED}
    env_var = pricing_catalog.FRIENDS_FAMILY_COUPON_ENV.get(discount_class, "")
    coupon = os.environ.get(env_var, "").strip()
    if not coupon:
        raise RuntimeError(f"COUPON_REQUIRED: {env_var} is not configured.")
    return {"coupon": coupon, "allow_promotion_codes": False, "friends_family": True}


def apply_to_checkout_fields(fields: list, customer_id: str, discount_class: str) -> dict:
    """Adds the Friends & Family coupon (or the normal promotion-code
    switch) to a Stripe Checkout field list in place."""
    discount = checkout_discount(customer_id, discount_class)
    fields[:] = [f for f in fields if f[0] != "allow_promotion_codes"]
    if discount["coupon"]:
        fields.append(("discounts[0][coupon]", discount["coupon"]))
    else:
        fields.append(("allow_promotion_codes", "true"))
    fields.append(("metadata[anyaicam_friends_family]", "approved" if discount["friends_family"] else "none"))
    return discount


# ------------------------------------------------------------------ email

def admin_recipient() -> str:
    configured = os.environ.get(ADMIN_EMAIL_ENV, "").strip()
    if configured:
        return configured
    from purchase_notifications import SUPPORT_EMAIL
    return SUPPORT_EMAIL


def review_url(request_id: str) -> str:
    from purchase_notifications import _portal_url
    return _portal_url(f"/admin/friends-family/{request_id}")


def notify_administrator(request: dict, customer: Optional[dict]) -> dict:
    name = (customer or {}).get("name") or "A customer"
    email = request.get("requested_by_email") or (customer or {}).get("email") or ""
    link = review_url(request["id"])
    subject = f"Friends & Family request: {name}"
    note = request.get("customer_note") or ""
    text = (f"{name} ({email}) asked for the Friends & Family discount.\n"
            f"Customer account: {request['customer_id']}\nRequest: {request['id']}\n"
            + (f"Note from the customer: {note}\n" if note else "")
            + f"\nReview Request (sign-in required): {link}\n\n"
              "Opening the link does not approve anything; approve or decline on the review page.")
    html = (f"<p><strong>{escape(name)}</strong> ({escape(email)}) asked for the Friends &amp; Family discount.</p>"
            f"<p>Customer account: {escape(request['customer_id'])}<br>Request: {escape(request['id'])}</p>"
            + (f"<p>Note from the customer: {escape(note)}</p>" if note else "")
            + f'<p><a href="{escape(link, quote=True)}">Review Request</a> (sign-in required)</p>'
              "<p>Opening the link does not approve anything; approve or decline on the review page.</p>")
    from purchase_notifications import _send_once
    return _send_once(event_id=f"friends-family:{request['id']}", notification_type="friends_family_request",
                      customer_id=request["customer_id"], recipient_email=admin_recipient(),
                      subject=subject, text=text, html=html, metadata={"friends_family_request_id": request["id"]})


# ------------------------------------------------------------------ pages

CUSTOMER_PANEL_ID = "friends-family-panel"


def customer_panel_html() -> str:
    """Drop-in panel for purchase pages (setup review, My subscription)."""
    return f'''<div class="panel" id="{CUSTOMER_PANEL_ID}" style="margin-top:14px">
  <h3 style="margin-top:0">Friends &amp; Family</h3>
  <p class="health-detail" id="ff-status-text">Checking…</p>
  <button class="ghost-button" id="ff-request" type="button" hidden>Request Friends &amp; Family discount</button>
</div>
<script>
(function(){{
  const text=document.getElementById('ff-status-text'),button=document.getElementById('ff-request');let timer=null;
  function show(state){{
    button.hidden=true;
    if(state.status==='pending'){{text.textContent='Pending administrator approval. You can check out once it is decided.';timer=timer||setInterval(load,20000);return;}}
    if(timer){{clearInterval(timer);timer=null;}}
    if(state.status==='approved'){{text.textContent=state.approved_message||{json.dumps(approved_message())};return;}}
    if(state.status==='declined'){{text.textContent='Your last request was declined. You can ask again if something has changed.';button.hidden=false;return;}}
    text.textContent='Family or friend of the AnyAiCam team? Ask for the Friends & Family discount before you check out.';button.hidden=false;
  }}
  async function load(){{
    try{{const r=await fetch('/api/customer/friends-family');if(r.ok){{show(await r.json());return;}}}}catch(e){{}}
    if(text.textContent==='Checking…')text.textContent='Friends & Family status is unavailable right now. Refresh the page to try again.';
  }}
  button.addEventListener('click',async()=>{{
    button.disabled=true;
    try{{const r=await fetch('/api/customer/friends-family/request',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:'{{}}'}});
      const body=await r.json();if(r.ok)show(body);else text.textContent=body.detail||'The request could not be sent. Try again.';}}
    catch(e){{text.textContent='The request could not be sent. Try again.';}}
    button.disabled=false;
  }});
  load();
}})();
</script>'''


def _review_page(request: dict, customer: Optional[dict]) -> str:
    decided = request["status"] != PENDING
    when = escape((request.get("requested_at") or "")[:16].replace("T", " "))
    customer_name = escape((customer or {}).get("name") or "Unknown customer")
    rows_html = "".join(
        f"<tr><th style='text-align:left;padding-right:14px'>{label}</th><td>{value}</td></tr>"
        for label, value in (
            ("Customer", customer_name),
            ("Email", escape(request.get("requested_by_email") or (customer or {}).get("email") or "")),
            ("Account", escape(request["customer_id"])),
            ("Requested", f'<span data-local-time="{escape(request.get("requested_at") or "", quote=True)}">{when}</span>'),
            ("Customer note", escape(request.get("customer_note") or "None")),
            ("Status", escape(request["status"].title())),
        )
    )
    percent = pricing_catalog.FRIENDS_FAMILY_PERCENT_OFF
    actions = "" if decided else f'''
  <label style="display:block;margin-top:14px">Note (optional)<br><input id="ff-note" maxlength="500" style="width:100%"></label>
  <div style="margin-top:14px;display:flex;gap:10px">
    <button class="action-button" id="ff-approve" type="button">Approve</button>
    <button class="ghost-button" id="ff-decline" type="button">Decline</button>
  </div>
  <p class="health-detail" id="ff-result"></p>
  <script>
  document.querySelectorAll('[data-local-time]').forEach(el=>{{const d=new Date(el.dataset.localTime);if(!isNaN(d))el.textContent=d.toLocaleString();}});
  async function decide(decision){{
    const out=document.getElementById('ff-result');
    document.getElementById('ff-approve').disabled=document.getElementById('ff-decline').disabled=true;
    const r=await fetch('/api/admin/friends-family/{escape(request["id"], quote=True)}/decision',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{decision,note:document.getElementById('ff-note').value}})}});
    const body=await r.json().catch(()=>({{}}));
    out.textContent=r.ok?('Saved: '+body.status):(body.detail||'Could not save the decision.');
    if(r.ok)setTimeout(()=>location.reload(),900);else document.getElementById('ff-approve').disabled=document.getElementById('ff-decline').disabled=false;
  }}
  document.getElementById('ff-approve').addEventListener('click',()=>decide('approve'));
  document.getElementById('ff-decline').addEventListener('click',()=>decide('decline'));
  </script>'''
    return f'''<header class="topbar"><div><p class="eyebrow">Administrator review</p><h1>Friends &amp; Family request</h1></div></header>
<section class="panel"><table>{rows_html}</table>
<p class="health-detail">Approving gives {percent["base"]}% off camera plans and {percent["analytics"]}% off analytics and Talk Down on this customer's new checkouts. Hardware is never discounted, promotion codes can't be combined with it, and no salesperson commission is paid on this customer's sales.</p>
{actions}</section>'''


def register_routes(app, shell) -> None:
    from partner_portal import partner_identity

    def customer_owner(request: Request) -> dict:
        identity = partner_identity(request)
        if not identity or identity.get("role") != "customer_owner" or not identity.get("customer_id"):
            raise HTTPException(status_code=403, detail="Customer owner permission required.")
        return identity

    def administrator(request: Request) -> dict:
        from website_partner import _require_global_admin
        return _require_global_admin(request)

    @app.get("/api/customer/friends-family")
    def friends_family_status(request: Request) -> dict:
        return customer_status(customer_owner(request)["customer_id"])

    @app.post("/api/customer/friends-family/request")
    async def friends_family_request(request: Request) -> dict:
        identity = customer_owner(request)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        note = str((payload or {}).get("note") or "") if isinstance(payload, dict) else ""
        record, created = create_request(customer_id=identity["customer_id"], email=identity.get("email", ""), note=note)
        if created:
            try:
                notify_administrator(record, row("SELECT * FROM customers WHERE id=?", (identity["customer_id"],)))
            except Exception:
                pass  # the request is stored either way; the admin list shows it
        return customer_status(identity["customer_id"])

    @app.get("/admin/friends-family", response_class=HTMLResponse)
    def friends_family_admin_list(request: Request):
        administrator(request)
        items = rows("SELECT r.*, c.name AS customer_name FROM friends_family_requests r "
                     "LEFT JOIN customers c ON c.id=r.customer_id ORDER BY r.requested_at DESC LIMIT 200")
        body = "".join(
            f'<tr><td><a href="/admin/friends-family/{escape(i["id"], quote=True)}">{escape(i.get("customer_name") or i["customer_id"])}</a></td>'
            f'<td>{escape(i.get("requested_by_email") or "")}</td><td>{escape(i["status"].title())}</td></tr>'
            for i in items
        ) or '<tr><td colspan="3">No requests yet.</td></tr>'
        content = ('<header class="topbar"><div><p class="eyebrow">Administrator</p><h1>Friends &amp; Family requests</h1></div></header>'
                   f'<section class="panel"><table><tr><th>Customer</th><th>Email</th><th>Status</th></tr>{body}</table></section>')
        return shell("Friends & Family requests", "admin-portal", content)

    @app.get("/admin/friends-family/{request_id}", response_class=HTMLResponse)
    def friends_family_review(request_id: str, request: Request):
        administrator(request)  # viewing never changes anything
        record = row("SELECT * FROM friends_family_requests WHERE id=?", (request_id,))
        if not record:
            raise HTTPException(status_code=404, detail="Request not found.")
        customer = row("SELECT * FROM customers WHERE id=?", (record["customer_id"],))
        return shell("Friends & Family request", "admin-portal", _review_page(record, customer))

    @app.post("/api/admin/friends-family/{request_id}/decision")
    async def friends_family_decision(request_id: str, request: Request) -> dict:
        identity = administrator(request)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        decision = str((payload or {}).get("decision") or "").lower()
        if decision not in ("approve", "decline"):
            raise HTTPException(status_code=400, detail="decision must be approve or decline.")
        try:
            record = decide(request_id, approve=decision == "approve", decided_by=identity.get("email", ""),
                            note=str((payload or {}).get("note") or ""))
        except LookupError:
            raise HTTPException(status_code=404, detail="Request not found.")
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error))
        return {"status": record["status"], "request_id": record["id"], "decided_at": record["decided_at"]}
