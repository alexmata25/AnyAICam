"""Operator control for hardware order fulfillment (2026-10-05, after Codex
review of 453bd1c: the lifecycle existed in hardware_fulfillment.py but
nothing let an operator move an online order through it).

    paid / order confirmed -> preparing -> shipped -> delivered (ready for setup)
    -> activated (not an operator step: the customer's appliance activation)

* Platform administrators only (website_partner._require_global_admin: the
  administrator role AND a live global administrator grant -- the same gate
  as the Friends & Family review). Customers, partners and partner-scoped
  administrators are refused.
* The order is looked up server-side from the path; nothing about the order
  or its customer is taken from the browser.
* Each step is one conditional UPDATE from the allowed previous states, so a
  repeated or concurrent request cannot apply it twice; repeating a step
  already applied returns the order unchanged. Out-of-order steps, and any
  step on a refunded, disputed or cancelled order, are refused.
* Carrier and tracking number are length- and character-checked; the
  tracking link is only ever generated here for a known carrier, never
  taken from input. The shipped email goes out from the recorded state
  through purchase_notifications' send-once outbox (one per order).
* Every applied step is written to audit_logs.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from html import escape
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from partner_db import connection, row, rows

OPERATOR_STEPS = {
    # target: states it may be applied from ('unfulfilled' is the table's
    # pre-lifecycle default, treated as paid)
    "preparing": ("unfulfilled", "paid"),
    "shipped": ("preparing", "configuring", "testing", "ready_to_ship"),
    "delivered": ("shipped",),
}
CARRIER_TRACKING_URLS = {
    "UPS": "https://www.ups.com/track?tracknum={}",
    "USPS": "https://tools.usps.com/go/TrackConfirmAction?tLabels={}",
    "FedEx": "https://www.fedex.com/fedextrack/?trknbr={}",
    "DHL": "https://www.dhl.com/us-en/home/tracking/tracking-express.html?tracking-id={}",
}
_CARRIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .&'-]{0,59}")
_TRACKING = re.compile(r"[A-Za-z0-9][A-Za-z0-9 -]{3,63}")


class FulfillmentRequest(BaseModel):
    status: str
    carrier: str | None = None
    tracking_number: str | None = None

    class Config:
        extra = "forbid"


def tracking_link(carrier: str | None, tracking_number: str | None) -> str | None:
    template = CARRIER_TRACKING_URLS.get(str(carrier or "").strip())
    return template.format(quote(str(tracking_number).strip(), safe="")) if template and tracking_number else None


def _order(order_id: str) -> dict:
    order = row("SELECT * FROM hardware_orders WHERE id=?", (order_id,))
    if not order:
        raise HTTPException(status_code=404, detail="Hardware order not found.")
    return order


def apply_step(order_id: str, payload: FulfillmentRequest, actor: dict) -> dict:
    """Apply one operator step; returns {'status': 'applied'|'unchanged', 'order': ...}."""
    import hardware_fulfillment
    target = payload.status.strip().lower()
    if target not in OPERATOR_STEPS:
        raise HTTPException(status_code=400, detail="Status must be preparing, shipped or delivered.")
    order = _order(order_id)
    try:
        hardware_fulfillment._refuse_test_order_on_live_server(order)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    carrier = tracking_number = link = None
    if target == "shipped":
        carrier = str(payload.carrier or "").strip()
        tracking_number = str(payload.tracking_number or "").strip()
        if not _CARRIER.fullmatch(carrier) or not _TRACKING.fullmatch(tracking_number):
            raise HTTPException(status_code=400, detail="Enter the carrier and a tracking number (letters, numbers, spaces and dashes).")
        link = tracking_link(carrier, tracking_number)
    current = str(order.get("fulfillment_status") or "")
    # Checked before anything else, the repeat path included: a refunded,
    # disputed or cancelled order is never moved and never emailed.
    if order.get("status") != "paid" or current == "cancelled":
        raise HTTPException(status_code=409, detail=f"This order is {order.get('status') if order.get('status') != 'paid' else 'cancelled'}; "
                                                    "it cannot be fulfilled.")
    if current == target:
        if target == "shipped":
            if (order.get("carrier"), order.get("tracking_number")) != (carrier, tracking_number):
                raise HTTPException(status_code=409, detail="This order is already shipped with different tracking details.")
            # Repeating the step retries a shipping email that failed; the
            # send-once outbox never sends a second one (and notify_hardware_
            # shipped re-checks the order is still paid and shipped).
            from purchase_notifications import notify_hardware_shipped
            notify_hardware_shipped(order_id)
        return {"status": "unchanged", "order": order}
    allowed = OPERATOR_STEPS[target]
    placeholders = ",".join("?" * len(allowed))
    now = datetime.now().isoformat()
    details = {"from": current or "paid", "to": target, **({"carrier": carrier} if carrier else {})}
    # The transition and its audit record are one transaction: both are
    # written, or neither is.
    with connection() as db:
        if target == "shipped":
            changed = db.execute(
                f"UPDATE hardware_orders SET fulfillment_status='shipped',carrier=?,tracking_number=?,tracking_link=?,shipped_at=?,"
                f"updated_at=? WHERE id=? AND status='paid' AND fulfillment_status IN ({placeholders})",
                (carrier, tracking_number, link, now, now, order_id, *allowed)).rowcount
        else:
            stamp = ",delivered_at=?" if target == "delivered" else ""
            changed = db.execute(
                f"UPDATE hardware_orders SET fulfillment_status=?,updated_at=?{stamp} WHERE id=? AND status='paid' "
                f"AND fulfillment_status IN ({placeholders})",
                (target, now, *((now,) if stamp else ()), order_id, *allowed)).rowcount
        if changed == 1:
            db.execute("INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) "
                       "VALUES(?,?,?,?,?,?,?)",
                       (actor.get("email", ""), actor.get("role", ""), f"hardware_order.{target}", "hardware_order", order_id,
                        json.dumps(details), now))
    if changed != 1:
        latest = _order(order_id)
        if latest.get("fulfillment_status") == target:  # a concurrent request applied it first
            return {"status": "unchanged", "order": latest}
        raise HTTPException(status_code=409, detail=f"Cannot move this order from '{current or 'paid'}' to '{target}'.")
    if target == "shipped":
        from purchase_notifications import notify_hardware_shipped
        notify_hardware_shipped(order_id)  # send-once per order
    return {"status": "applied", "order": _order(order_id)}


def _orders_page_rows() -> str:
    import hardware_fulfillment
    found = rows("SELECT o.*, c.name AS customer_name, c.email AS customer_email, "
                 "(SELECT 1 FROM appliances a WHERE a.customer_id=o.customer_id AND a.activation_status='activated' LIMIT 1) AS activated "
                 "FROM hardware_orders o LEFT JOIN customers c ON c.id=o.customer_id "
                 "WHERE o.status='paid' ORDER BY o.created_at DESC LIMIT 200")
    out = []
    for order in found:
        state = str(order.get("fulfillment_status") or "paid")
        shown = "activated" if order.get("activated") and state == "delivered" else ("paid" if state == "unfulfilled" else state)
        action = ""
        oid = escape(order["id"], quote=True)
        if state in OPERATOR_STEPS["preparing"]:
            action = f'<button class="ghost-button fulfil" data-order="{oid}" data-status="preparing">Mark preparing</button>'
        elif state in OPERATOR_STEPS["shipped"]:
            options = "".join(f'<option value="{escape(name, quote=True)}">{escape(name)}</option>' for name in CARRIER_TRACKING_URLS)
            action = (f'<span class="ship-form"><select data-carrier="{oid}">{options}</select> '
                      f'<input data-tracking="{oid}" placeholder="Tracking number" maxlength="64"> '
                      f'<button class="ghost-button fulfil" data-order="{oid}" data-status="shipped">Mark shipped</button></span>')
        elif state == "shipped":
            action = f'<button class="ghost-button fulfil" data-order="{oid}" data-status="delivered">Mark delivered</button>'
        tracking = " · ".join(escape(str(order[key])) for key in ("carrier", "tracking_number") if order.get(key))
        out.append(f'<tr data-order-row="{oid}"><td>{escape(hardware_fulfillment.generate_order_number(order["id"]))}</td>'
                   f'<td>{escape(str(order.get("customer_name") or ""))}<br><span class="health-detail">{escape(str(order.get("customer_email") or ""))}</span></td>'
                   f'<td>{escape(str(order.get("product_name") or ""))} × {int(order.get("quantity") or 1)}</td>'
                   f'<td><span class="pill">{escape(shown)}</span><br><span class="health-detail">{tracking}</span></td><td>{action}</td></tr>')
    return "".join(out) or '<tr><td colspan="5">No paid hardware orders.</td></tr>'


SCRIPT = ("const csrf=()=>{const m=document.cookie.split('; ').find(x=>x.startsWith('anyaicam_csrf='));if(!m)return '';"
          "let v=decodeURIComponent(m.split('=').slice(1).join('='));return v.length>=2&&v[0]==='\"'&&v[v.length-1]==='\"'?v.slice(1,-1):v};"
          "document.querySelectorAll('button.fulfil').forEach(b=>b.onclick=async()=>{const id=b.dataset.order,s=b.dataset.status,body={status:s};"
          "if(s==='shipped'){body.carrier=document.querySelector(`[data-carrier=\"${id}\"]`).value;body.tracking_number=document.querySelector(`[data-tracking=\"${id}\"]`).value}"
          "if(!confirm('Mark this order '+s+'?'))return;b.disabled=true;const r=await fetch(`/api/admin/hardware-orders/${encodeURIComponent(id)}/fulfillment`,"
          "{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},body:JSON.stringify(body)});const d=await r.json().catch(()=>({}));"
          "document.getElementById('fulfil-message').textContent=r.ok?'Saved.':(d.detail||'Could not save.');if(r.ok)setTimeout(()=>location.reload(),600);else b.disabled=false});")


def register_fulfillment_admin_routes(app: FastAPI, shell) -> None:
    def administrator(request: Request) -> dict:
        from website_partner import _require_global_admin
        return _require_global_admin(request)

    @app.get("/admin/hardware-orders", response_class=HTMLResponse)
    def hardware_orders_page(request: Request):
        administrator(request)
        content = ('<header class="topbar"><div><p class="eyebrow">Platform administrator</p><h1>Hardware orders</h1></div></header>'
                   '<section class="panel"><p class="health-detail">Move each paid order forward: preparing, shipped (sends the '
                   'customer one shipping email), delivered (the customer can start setup). Activation happens when the '
                   'customer activates the appliance.</p><p id="fulfil-message" class="health-detail" role="status"></p>'
                   '<div style="overflow-x:auto"><table style="width:100%"><thead><tr><th>Order</th><th>Customer</th><th>Item</th>'
                   f'<th>Status</th><th></th></tr></thead><tbody>{_orders_page_rows()}</tbody></table></div></section>')
        return HTMLResponse(shell("Hardware orders", "admin-portal", content, f"<script>{SCRIPT}</script>"),
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/admin/hardware-orders/{order_id}/fulfillment")
    def hardware_order_fulfillment(order_id: str, payload: FulfillmentRequest, request: Request) -> dict:
        actor = administrator(request)
        result = apply_step(order_id, payload, actor)
        order = result["order"]
        return {"status": result["status"], "order_id": order["id"], "fulfillment_status": order["fulfillment_status"],
                "carrier": order.get("carrier"), "tracking_number": order.get("tracking_number")}
