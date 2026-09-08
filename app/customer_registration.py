import secrets
from datetime import datetime
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from appliance_identity import create_grant
from database_backend import backend
from partner_db import connection, password_hash
from partner_portal import partner_identity


def _now() -> str:
    return datetime.now().isoformat()


def create_pending_registration(display_name: str, email: str, password: str) -> tuple[str, bool]:
    """Create an unassigned, inactive request in the authoritative database."""
    normalized = email.strip().lower()
    with connection() as db:
        existing = db.execute(
            "SELECT id,status FROM customer_registration_requests WHERE email=?", (normalized,)
        ).fetchone()
        if existing:
            return existing["status"], False
        if db.execute("SELECT 1 FROM partner_users WHERE lower(email)=?", (normalized,)).fetchone():
            raise HTTPException(status_code=409, detail="That email is already registered. Use the sign-in page.")
        if db.execute("SELECT 1 FROM customers WHERE lower(email)=?", (normalized,)).fetchone():
            raise HTTPException(status_code=409, detail="That email is already registered. Use the sign-in page.")
        request_id = secrets.token_hex(16)
        try:
            db.execute(
                "INSERT INTO customer_registration_requests(id,display_name,email,password_hash,status,requested_at) VALUES(?,?,?,?,?,?)",
                (request_id, display_name.strip(), normalized, password_hash(password), "pending", _now()),
            )
            db.execute(
                "INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (normalized, "anonymous", "customer_registration.requested", "customer_registration", request_id, "{}", _now()),
            )
        except Exception as error:
            if "unique" in str(error).lower() or "duplicate" in str(error).lower():
                raise HTTPException(status_code=409, detail="That email already has an account request.") from error
            raise
    return "pending", True


def _approval_identity(request: Request, current_user, is_master_admin) -> dict:
    identity = partner_identity(request)
    if identity:
        if identity.get("role") == "administrator":
            with connection() as db:
                global_grant = db.execute(
                    "SELECT 1 FROM identity_grants g JOIN partner_users u ON u.id=g.user_id "
                    "WHERE lower(u.email)=? AND g.role='administrator' AND g.scope_type='global' AND g.revoked_at IS NULL",
                    (str(identity.get("email") or "").lower(),),
                ).fetchone()
            if global_grant:
                return {"master": True, "email": identity["email"], "partner_id": identity.get("partner_id")}
        if identity.get("role") in {"partner_owner", "administrator"} and identity.get("partner_id"):
            return {"master": False, "email": identity["email"], "partner_id": identity["partner_id"]}
        raise HTTPException(status_code=403, detail="Customer registration approval permission required.")
    legacy = current_user(request)
    if is_master_admin(legacy):
        return {"master": True, "email": legacy.get("email", "admin@local"), "partner_id": None}
    raise HTTPException(status_code=403, detail="Customer registration approval permission required.")


def _request_for_actor(db, request_id: str, actor: dict):
    item = db.execute("SELECT * FROM customer_registration_requests WHERE id=?", (request_id,)).fetchone()
    if not item:
        raise HTTPException(status_code=404, detail="Customer registration request not found.")
    item = dict(item)
    if not actor["master"] and item.get("partner_id") != actor["partner_id"]:
        raise HTTPException(status_code=403, detail="This registration belongs to another tenant or is unassigned.")
    return item


def approve_registration(request_id: str, partner_id: str | None, actor: dict) -> dict:
    with connection() as db:
        if backend() == "sqlite":
            db.execute("BEGIN IMMEDIATE")
        else:
            db.execute("SELECT id FROM customer_registration_requests WHERE id=? FOR UPDATE", (request_id,))
        item = _request_for_actor(db, request_id, actor)
        if item["status"] == "approved":
            return {"status": "complete", "message": "Account was already approved.", "customer_id": item["customer_id"], "user_id": item["user_id"]}
        if item["status"] == "rejected":
            raise HTTPException(status_code=409, detail="A rejected registration cannot be approved.")
        selected_partner = (partner_id or item.get("partner_id") or "").strip()
        if not selected_partner:
            raise HTTPException(status_code=422, detail="Select an existing partner before approval.")
        if not actor["master"] and selected_partner != actor["partner_id"]:
            raise HTTPException(status_code=403, detail="Cross-tenant approval is not allowed.")
        partner = db.execute("SELECT id FROM partners WHERE id=? AND approval_status='approved'", (selected_partner,)).fetchone()
        if not partner:
            raise HTTPException(status_code=422, detail="Select an approved partner.")
        if db.execute("SELECT 1 FROM partner_users WHERE lower(email)=?", (item["email"],)).fetchone() or db.execute("SELECT 1 FROM customers WHERE lower(email)=?", (item["email"],)).fetchone():
            raise HTTPException(status_code=409, detail="That email is already associated with an account.")
        now = _now(); customer_id = secrets.token_hex(16); user_id = secrets.token_hex(16)
        db.execute(
            "INSERT INTO customers(id,partner_id,name,company,email,status,source,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?)",
            (customer_id, selected_partner, item["display_name"], item["display_name"], item["email"], "active", "real", now, actor["email"]),
        )
        db.execute(
            "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, selected_partner, item["email"], item["display_name"], "customer_owner", item["password_hash"], 1, customer_id, now, "active", "all"),
        )
        if not db.execute("SELECT 1 FROM identity_grants WHERE user_id=? AND role='customer_owner' AND scope_type='customer' AND scope_id=? AND revoked_at IS NULL", (user_id, customer_id)).fetchone():
            create_grant(db, user_id=user_id, role="customer_owner", scope_type="customer", scope_id=customer_id, granted_by=actor["email"], now=now)
        db.execute(
            "UPDATE customer_registration_requests SET status='approved',partner_id=?,customer_id=?,user_id=?,decided_at=?,decided_by=?,rejection_reason=NULL WHERE id=?",
            (selected_partner, customer_id, user_id, now, actor["email"], request_id),
        )
        db.execute(
            "INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (actor["email"], "administrator" if actor["master"] else "partner_owner", "customer_registration.approved", "customer_registration", request_id, '{}', now),
        )
    return {"status": "complete", "message": "Customer account approved.", "customer_id": customer_id, "user_id": user_id}


def assign_registration(request_id: str, partner_id: str, actor: dict) -> dict:
    if not actor["master"]:
        raise HTTPException(status_code=403, detail="Only a master administrator can assign an unassigned registration.")
    selected_partner = partner_id.strip()
    with connection() as db:
        item = _request_for_actor(db, request_id, actor)
        if item["status"] != "pending":
            raise HTTPException(status_code=409, detail="Only pending registrations can be assigned.")
        if not db.execute("SELECT 1 FROM partners WHERE id=? AND approval_status='approved'", (selected_partner,)).fetchone():
            raise HTTPException(status_code=422, detail="Select an approved partner.")
        now = _now()
        db.execute("UPDATE customer_registration_requests SET partner_id=? WHERE id=?", (selected_partner, request_id))
        db.execute("INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)", (actor["email"], "administrator", "customer_registration.assigned", "customer_registration", request_id, "{}", now))
    return {"status": "complete", "message": "Registration assigned to partner.", "partner_id": selected_partner}


def reject_registration(request_id: str, reason: str, actor: dict) -> dict:
    with connection() as db:
        item = _request_for_actor(db, request_id, actor)
        now = _now()
        if item.get("user_id"):
            db.execute("UPDATE partner_users SET approved=0,account_status='revoked',authorization_version=authorization_version+1 WHERE id=?", (item["user_id"],))
            db.execute("UPDATE identity_grants SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now, item["user_id"]))
        if item.get("customer_id"):
            db.execute("UPDATE customers SET status='suspended' WHERE id=?", (item["customer_id"],))
        db.execute("UPDATE customer_registration_requests SET status='rejected',decided_at=?,decided_by=?,rejection_reason=? WHERE id=?", (now, actor["email"], reason.strip()[:1000], request_id))
        db.execute("INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)", (actor["email"], "administrator" if actor["master"] else "partner_owner", "customer_registration.rejected", "customer_registration", request_id, '{}', now))
    return {"status": "complete", "message": "Customer registration rejected."}


def register_customer_registration_routes(app, page_shell, current_user, is_master_admin) -> None:
    def actor(request):
        return _approval_identity(request, current_user, is_master_admin)

    @app.get("/api/customer-registration-requests")
    def list_requests(request: Request):
        who = actor(request)
        with connection() as db:
            if who["master"]:
                items = db.execute("SELECT * FROM customer_registration_requests ORDER BY requested_at DESC").fetchall()
            else:
                items = db.execute("SELECT * FROM customer_registration_requests WHERE partner_id=? ORDER BY requested_at DESC", (who["partner_id"],)).fetchall()
        return {"requests": [{k: v for k, v in dict(row).items() if k != "password_hash"} for row in items]}

    @app.post("/api/customer-registration-requests/{request_id}/approve")
    def approve(request_id: str, request: Request, payload: dict):
        return approve_registration(request_id, str(payload.get("partner_id") or ""), actor(request))

    @app.post("/api/customer-registration-requests/{request_id}/assign")
    def assign(request_id: str, request: Request, payload: dict):
        return assign_registration(request_id, str(payload.get("partner_id") or ""), actor(request))

    @app.post("/api/customer-registration-requests/{request_id}/reject")
    def reject(request_id: str, request: Request, payload: dict):
        return reject_registration(request_id, str(payload.get("reason") or ""), actor(request))

    @app.get("/customer-registration-requests", response_class=HTMLResponse)
    def requests_page(request: Request):
        actor(request)
        content = '''<header class="topbar"><div><p class="eyebrow">Customer access</p><h1>Registration requests</h1></div></header><section class="panel"><p class="health-detail">Unassigned public requests require a master administrator to select an existing partner. Partner administrators see only requests assigned to their tenant.</p><div id="registration-requests">Loading…</div></section>'''
        scripts = '''<script>const esc=v=>String(v??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));async function loadRegistrations(){const r=await fetch('/api/customer-registration-requests'),p=await r.json(),box=document.getElementById('registration-requests');box.innerHTML=(p.requests||[]).map(x=>`<article class="feature-card"><strong>${esc(x.display_name)}</strong><p>${esc(x.email)} · ${esc(x.status)}</p><label>Existing partner ID<input id="partner-${x.id}" value="${esc(x.partner_id)}"></label><button onclick="decide('${x.id}','assign')">Assign</button> <button onclick="decide('${x.id}','approve')">Approve</button> <button onclick="decide('${x.id}','reject')">Reject</button></article>`).join('')||'<div class="empty">No registration requests.</div>'}async function decide(id,action){const partner=document.getElementById('partner-'+id)?.value||'',reason=action==='reject'?prompt('Rejection reason')||'Rejected':'';const r=await fetch(`/api/customer-registration-requests/${id}/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({partner_id:partner,reason})}),p=await r.json();showToast(p.message||p.detail);if(r.ok)loadRegistrations()}loadRegistrations();</script>'''
        return page_shell("Customer registrations", "business-users", content, current_user(request), scripts=scripts)
