"""Per-camera operational controls and fail-closed Test Lab authorization.

Commercial assignments remain in ``camera_analytics_entitlements`` and
``analytics_subscriptions``. This module stores only operational ON/OFF
preferences and signed lab grant provenance; it never writes Stripe or
commercial subscription records.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from partner_db import connection
from partner_portal import partner_identity
from customer_analytics_panel import analytics_license_summary, assign_entitlement, remove_entitlement, LicenseLimitExceeded

ANALYTICS: dict[str, dict[str, str]] = {
    "smart_motion": {"label": "Smart Motion", "dependency": "Object detection and motion classification"},
    "people_counting": {"label": "People Counting", "dependency": "People detection and a configured counting line"},
    "lpr": {"label": "License Plates", "dependency": "Vehicle detection and the LPR engine"},
    "ppe": {"label": "PPE", "dependency": "Person detection and the PPE model"},
    "facial_recognition": {"label": "Facial Recognition", "dependency": "Person detection and Face Access assignment"},
    "line_crossing": {"label": "Line Crossing", "dependency": "Smart Motion and a configured line rule. Also turns security-line alarms on or off"},
    "intrusion": {"label": "Intrusion", "dependency": "Smart Motion and a configured zone rule"},
    "loitering": {"label": "Loitering", "dependency": "Smart Motion and a configured loitering zone"},
}
# Owner decision 2026-10-16 (Fix 7): the Line Crossing switch governs security lines too. Turning it off stops them
# (rules and history are kept); turning it back on restores them, each subject to its own enabled setting.
RULE_TYPES = {"line_crossing": ("line_crossing", "security_line"),
              "intrusion": ("intrusion",), "loitering": ("loitering",)}
RULE_ANALYTICS = {"line_crossing": "line_crossing",
                  "intrusion": "intrusion", "loitering": "loitering",
                  "people_counting": "people_counting"}
_GRANT_FIELDS = {"schema_version", "grant_id", "key_id", "environment_id", "appliance_id",
                 "customer_id", "site_id", "camera_ids", "analytics", "issued_at", "not_before", "expires_at"}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _people_counting_line_ready(db, camera_id: str) -> bool:
    """Return true only for an enabled, usable customer counting line.

    A placeholder rule row (the old UI sometimes created one with empty
    geometry) must not make People Counting appear activatable.
    """
    rows = db.execute(
        "SELECT geometry_json FROM customer_analytics_rules "
        "WHERE camera_id=? AND rule_type='people_counting' AND enabled=1",
        (camera_id,),
    ).fetchall()
    for row in rows:
        try:
            geometry = json.loads(row["geometry_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(geometry, list) or len(geometry) < 2:
            continue
        points = geometry[:2]
        try:
            coordinates = [(float(point["x"]), float(point["y"])) for point in points]
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        if all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in coordinates) and coordinates[0] != coordinates[1]:
            return True
    return False


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _utc(value: str) -> float:
    if not isinstance(value, str) or not value:
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc).timestamp()


def test_lab_environment() -> tuple[bool, str]:
    """Lab access is opt-in, environment-bound, and never available in prod."""
    env = os.getenv("ANYAICAM_ENV", "").strip().lower()
    role = os.getenv("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower()
    if env not in {"development", "test", "staging", "lab"} or role not in {"edge", "combined", "cloud"}:
        return False, "Test Lab is restricted to explicitly configured non-production appliance environments."
    if os.getenv("ANYAICAM_TEST_LAB_ENABLED", "").strip().lower() != "true":
        return False, "Test Lab is not enabled for this environment."
    if not os.getenv("ANYAICAM_TEST_LAB_ENVIRONMENT_ID", "").strip():
        return False, "Test Lab environment identity is not configured."
    if not _configured_appliance_ids():
        return False, "No approved Test Lab appliance identities are configured."
    if not _public_keys():
        return False, "No Test Lab verification keys are configured."
    return True, ""


def _configured_appliance_ids() -> set[str]:
    return {part.strip() for part in os.getenv("ANYAICAM_TEST_LAB_APPLIANCE_IDS", "").split(",") if part.strip()}


def _public_keys() -> dict[str, bytes]:
    try:
        value = json.loads(os.getenv("ANYAICAM_TEST_LAB_PUBLIC_KEYS_JSON", "{}"))
        if not isinstance(value, dict):
            return {}
        keys = {}
        for key_id, encoded in value.items():
            raw = base64.b64decode(encoded, validate=True)
            if isinstance(key_id, str) and _ID.fullmatch(key_id) and len(raw) == 32:
                keys[key_id] = raw
        return keys
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}


def verify_grant(envelope: dict, *, now: float | None = None) -> tuple[dict | None, str]:
    """Verify a strict Ed25519 grant and local non-production allowlists."""
    enabled, reason = test_lab_environment()
    if not enabled:
        return None, reason
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature"}:
        return None, "Malformed Test Lab grant envelope."
    payload, signature = envelope.get("payload"), envelope.get("signature")
    if not isinstance(payload, dict) or set(payload) != _GRANT_FIELDS:
        return None, "Malformed Test Lab grant payload."
    if payload.get("schema_version") != 1 or isinstance(payload.get("schema_version"), bool):
        return None, "Unsupported Test Lab grant version."
    for field in ("grant_id", "key_id", "environment_id", "appliance_id", "customer_id", "site_id"):
        if not isinstance(payload.get(field), str) or not _ID.fullmatch(payload[field]):
            return None, "Invalid Test Lab grant identity."
    if payload["environment_id"] != os.getenv("ANYAICAM_TEST_LAB_ENVIRONMENT_ID"):
        return None, "Test Lab grant targets a different environment."
    if payload["appliance_id"] not in _configured_appliance_ids():
        return None, "Test Lab grant targets an unapproved appliance."
    cameras, analytics = payload.get("camera_ids"), payload.get("analytics")
    if (not isinstance(cameras, list) or not cameras or any(not isinstance(c, str) or not _ID.fullmatch(c) for c in cameras)
            or len(set(cameras)) != len(cameras)):
        return None, "Invalid Test Lab camera scope."
    if not isinstance(analytics, dict) or not analytics or any(
            key not in ANALYTICS or isinstance(quantity, bool) or not isinstance(quantity, int)
            or quantity < 1 or quantity > len(cameras) for key, quantity in analytics.items()):
        return None, "Invalid Test Lab analytic quantities."
    try:
        issued, not_before, expires = (_utc(payload[name]) for name in ("issued_at", "not_before", "expires_at"))
    except (ValueError, TypeError, OverflowError):
        return None, "Invalid Test Lab grant times."
    current = time.time() if now is None else now
    max_hours_raw = os.getenv("ANYAICAM_TEST_LAB_MAX_GRANT_HOURS", "720")
    try:
        max_hours = min(720, max(1, int(max_hours_raw)))
    except ValueError:
        return None, "Invalid Test Lab maximum duration configuration."
    if (issued > current + 60 or not_before > current + 60 or not_before < issued - 60
            or expires <= current or expires <= not_before or expires - issued > max_hours * 3600):
        return None, "Test Lab grant is not currently valid."
    key = _public_keys().get(payload["key_id"])
    if key is None or not isinstance(signature, str):
        return None, "Test Lab signing key is not trusted."
    try:
        sig = base64.b64decode(signature, validate=True)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(key).verify(sig, _canonical(payload))
    except Exception:
        return None, "Test Lab grant signature is invalid."
    return payload, ""


def operational_enabled(db, camera_id: str, analytic_key: str) -> bool:
    row = db.execute("SELECT enabled,system_paused FROM camera_analytics_operations WHERE camera_id=? AND analytic_key=?",
                     (camera_id, analytic_key)).fetchone()
    return True if row is None else bool(row["enabled"]) and not bool(row["system_paused"])


def operational_status(db, camera_id: str, analytic_key: str) -> dict:
    """Customer preference and temporary system pause are independent state."""
    row = db.execute("SELECT enabled,system_paused,system_pause_reason FROM camera_analytics_operations "
                     "WHERE camera_id=? AND analytic_key=?", (camera_id, analytic_key)).fetchone()
    customer_enabled = True if row is None else bool(row["enabled"])
    paused = bool(row and row["system_paused"])
    return {"customer_enabled": customer_enabled, "system_paused": paused,
            "system_pause_reason": row["system_pause_reason"] if paused else None,
            "effective_enabled": customer_enabled and not paused}


def set_system_pause(db, camera_id: str, analytic_key: str, *, paused: bool, reason: str = "") -> None:
    """Set/clear a system pause without changing the customer preference or license.

    This is an internal integration point for resource/health governors. It is
    deliberately not exposed as a customer API. A pause must have an auditable
    reason; clearing it leaves the customer's ON/OFF choice untouched.
    """
    if analytic_key not in ANALYTICS or not isinstance(paused, bool):
        raise ValueError("Invalid analytic system-pause request")
    reason = str(reason or "").strip()
    if paused and not reason:
        raise ValueError("A system pause reason is required")
    if len(reason) > 240:
        raise ValueError("System pause reason is too long")
    now = datetime.now(timezone.utc).isoformat()
    db.execute("INSERT INTO camera_analytics_operations(camera_id,analytic_key,enabled,system_paused,system_pause_reason,"
               "system_paused_at,updated_at,updated_by) VALUES(?,?,1,?,?,?,?,'system') "
               "ON CONFLICT(camera_id,analytic_key) DO UPDATE SET system_paused=excluded.system_paused,"
               "system_pause_reason=excluded.system_pause_reason,system_paused_at=excluded.system_paused_at,"
               "updated_at=excluded.updated_at,updated_by='system'",
               (camera_id, analytic_key, int(paused), reason if paused else None,
                now if paused else None, now))
    _audit(db, {"role": "system", "email": "system"},
           "analytics.system_paused" if paused else "analytics.system_pause_cleared",
           camera_id, {"analytic_key": analytic_key, "reason": reason if paused else ""})


def active_lab_grant(db, camera: dict, analytic_key: str, *, now: float | None = None) -> dict | None:
    """Find an imported signed grant with a free assigned slot for this camera."""
    enabled, _ = test_lab_environment()
    if not enabled or analytic_key not in ANALYTICS:
        return None
    rows = db.execute("SELECT grant_id,payload_json,signature FROM test_lab_grants WHERE status='active'").fetchall()
    current = time.time() if now is None else now
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        payload, _reason = verify_grant({"payload": payload, "signature": row["signature"]}, now=current)
        if not payload or payload["grant_id"] != row["grant_id"]:
            continue
        if (payload["customer_id"] != str(camera.get("customer_id") or "")
                or payload["site_id"] != str(camera.get("site_id") or "")
                or payload["appliance_id"] != str(camera.get("appliance_id") or "")
                or camera.get("id") not in payload["camera_ids"]):
            continue
        quantity = payload["analytics"].get(analytic_key, 0)
        if not quantity:
            continue
        used = db.execute("SELECT COUNT(*) AS n FROM test_lab_analytics_assignments WHERE grant_id=? AND analytic_key=? AND enabled=1",
                          (payload["grant_id"], analytic_key)).fetchone()["n"]
        already = db.execute("SELECT 1 FROM test_lab_analytics_assignments WHERE grant_id=? AND analytic_key=? AND camera_id=? AND enabled=1",
                             (payload["grant_id"], analytic_key, camera["id"])).fetchone()
        if already or used < quantity:
            return payload
    return None


def lab_assignment_enabled(db, camera_id: str, analytic_key: str) -> bool:
    row = db.execute("SELECT 1 FROM test_lab_analytics_assignments a JOIN test_lab_grants g ON g.grant_id=a.grant_id "
                     "WHERE a.camera_id=? AND a.analytic_key=? AND a.enabled=1 AND g.status='active' LIMIT 1",
                     (camera_id, analytic_key)).fetchone()
    return row is not None


def filter_rule_items(db, rules: list[dict], cameras: list[dict]) -> list[dict]:
    """Apply per-camera analytic switches to the existing rule delivery.

    Exclusion zones are rule configuration, not an independently billed
    analytic, so retain them for an active Smart Motion camera even when
    the Intrusion toggle is off. Unknown rule types fail closed.
    """
    camera_by_id = {str(item.get("id")): item for item in cameras}
    filtered = []
    for rule in rules:
        camera = camera_by_id.get(str(rule.get("camera_id")))
        if not camera:
            continue
        rule_type = rule.get("rule_type")
        # Secure Edge security lines are armed intrusion alarms with their own
        # evaluator, but (owner decision 2026-10-16, Fix 7) the customer's Line
        # Crossing switch also governs them: OFF stops them processing and
        # alerting, ON restores them. Smart Motion still gates them too.
        if rule_type == "security_line":
            if (camera.get("smart_motion_enabled") and operational_enabled(db, rule["camera_id"], "line_crossing")
                    and operational_enabled(db, rule["camera_id"], "smart_motion")):
                filtered.append(rule)
            continue
        if rule_type == "exclusion":
            if camera.get("smart_motion_enabled"):
                filtered.append(rule)
            continue
        analytic_key = RULE_ANALYTICS.get(rule_type)
        if not analytic_key:
            continue
        if not operational_enabled(db, rule["camera_id"], analytic_key):
            continue
        if rule_type in RULE_TYPES and not operational_enabled(db, rule["camera_id"], "smart_motion"):
            continue
        camera_flag = "people_counting_enabled" if rule_type == "people_counting" else "smart_motion_enabled"
        if camera.get(camera_flag):
            filtered.append(rule)
    return filtered


def register_routes(app: FastAPI, page_shell: Callable) -> None:
    def refresh_appliance_runtime(camera_id: str) -> None:
        """Apply local operational changes to the edge's cached worker map.

        Cloud routes are authoritative for Hybrid customers and their next
        authenticated configuration poll updates the edge. On an appliance,
        the locally persisted switch must take effect immediately, including
        while offline; it cannot grant entitlement because the refresh only
        overlays OFF/pause on the last locally stored authorization snapshot.
        """
        try:
            import recording_uploader
            if recording_uploader.RUNTIME_ROLE in {"edge", "combined"}:
                recording_uploader._refresh_camera_map(prefer_local=True)
        except Exception as error:
            # A refresh failure must not roll back a durable customer choice;
            # the periodic map worker retries and emits its own diagnostic.
            import logging
            logging.getLogger("anyaicam.analytics_control_center").warning(
                "local_runtime_refresh_failed camera_id=%s error_type=%s", camera_id, type(error).__name__)

    def identity(request: Request) -> dict:
        who = partner_identity(request)
        if not who or who.get("role") not in {"customer_owner", "customer_viewer"} or not who.get("customer_id"):
            raise HTTPException(status_code=403, detail="Customer access is required.")
        return who

    def lab_administrator_identity(request: Request) -> dict:
        who = partner_identity(request)
        if not who or who.get("role") != "administrator":
            raise HTTPException(status_code=403, detail="Administrator access is required.")
        return who

    def camera_access(db, camera_id: str, who: dict, *, edit: bool) -> dict:
        row = db.execute("SELECT id,name,camera_number,customer_id,site_id,appliance_id,status,smart_motion_enabled FROM cameras WHERE id=? AND customer_id=?",
                         (camera_id, who["customer_id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Camera not found.")
        camera = dict(row)
        can_edit = who["role"] == "customer_owner"
        if not can_edit:
            user = db.execute("SELECT id FROM partner_users WHERE lower(email)=lower(?) AND customer_id=?",
                              (who.get("email", ""), who["customer_id"])).fetchone()
            perm = db.execute("SELECT can_live,can_playback,can_settings FROM customer_camera_permissions WHERE user_id=? AND camera_id=?",
                              (user["id"], camera_id)).fetchone() if user else None
            if not perm or not (perm["can_live"] or perm["can_playback"]):
                raise HTTPException(status_code=403, detail="Not authorized for this camera.")
            can_edit = bool(perm["can_settings"])
        if edit and not can_edit:
            raise HTTPException(status_code=403, detail="Camera Settings permission is required.")
        return camera

    @app.get("/analytics-control-center", response_class=HTMLResponse)
    def control_center_page(request: Request):
        who = identity(request)
        with connection() as db:
            cameras = [dict(r) for r in db.execute("SELECT id,name,camera_number FROM cameras WHERE customer_id=? AND status!='removed' ORDER BY camera_number",
                                                   (who["customer_id"],)).fetchall()]
        options = "".join('<option value="{}">{}</option>'.format(_escape(c["id"]), _escape(c.get("name") or "Camera {}".format(c.get("camera_number")))) for c in cameras)
        content = '''<header class="topbar"><div><p class="eyebrow">Settings</p><h1>Analytics Control Center</h1>
        <p class="health-detail">Choose which purchased analytics run on each camera. Turning analytics off never changes recording or video.</p></div>
        <a class="ghost-button" href="/customer-app-settings">Back to Settings</a></header>
        <section class="panel"><label style="display:grid;gap:7px;max-width:360px">Camera<select id="acc-camera">__CAMERA_OPTIONS__</select></label>
        <button class="action-button" id="acc-off" type="button" style="margin-top:14px">Turn all off</button>
        <p class="health-detail" style="margin-top:10px"><a href="/analytics">View your analytics</a></p></section>
        <section class="panel" style="margin-top:16px"><h2>Your analytics</h2><p class="health-detail">See how many licenses you purchased, assigned and have available at this camera's site. Turning an analytic off keeps its license on this camera; Unassign frees that license for another camera.</p><div id="acc-list" class="health-list">Loading…</div></section>
        <section class="panel" style="margin-top:16px"><h2>AAC Guardian</h2><p class="health-detail">Not available yet. AAC Guardian is a separate product in development and is not part of this system.</p><button class="ghost-button" type="button" disabled aria-disabled="true">Not available</button></section>
        <script>const sel=document.getElementById('acc-camera'),list=document.getElementById('acc-list');
        function note(m){if(typeof showToast==='function')showToast(m);else alert(m)}
        async function body(r){try{return await r.json()}catch(e){return {}}}
        function msg(d,fallback){const t=d&&d.detail;return typeof t==='string'&&t&&!/test lab|grant/i.test(t)?t:fallback}
        function esc(v){return String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
        async function load(){if(!sel.value){list.textContent='No cameras are assigned to this account.';return}let r,d;try{r=await fetch('/api/customer/analytics-control/cameras/'+encodeURIComponent(sel.value));d=await body(r)}catch(e){list.textContent='Analytics could not be loaded. Check your connection and try again.';return}if(!r.ok||!Array.isArray(d.analytics)){list.textContent=msg(d,'Analytics could not be loaded. Try again.');return}list.innerHTML=d.analytics.map(a=>{const q=a.license;const qty=a.assignment_required===false?'Included with Smart Motion':q.purchase_kind==='included'?'Included for eligible cameras':`Purchased ${q.purchased_quantity} / assigned ${q.assigned_quantity} / available ${q.available_quantity}`;const state=a.system_paused?'Temporarily paused by system'+(a.system_pause_reason?': '+esc(a.system_pause_reason):''):a.assigned?(a.customer_enabled?'On':'Off'):'Unassigned';return `<div class="health-row"><span><b>${esc(a.label)}</b><br><span class="health-detail">${qty} · ${state}${a.dependency_ready?'':' · Configure prerequisites'}</span></span><span><button class="ghost-button" type="button" data-assign="${esc(a.key)}" ${a.assignment_required&&a.can_assign?'':'disabled'} ${a.assignment_required?'':'hidden'}>${a.assigned?'Unassign':'Assign'}</button> <button class="ghost-button" type="button" data-key="${esc(a.key)}" ${a.assigned&&(a.customer_enabled||a.can_enable)?'':'disabled'}>${a.customer_enabled?'Turn off':'Turn on'}</button></span></div>`}).join('');list.querySelectorAll('button[data-key]').forEach(b=>b.onclick=()=>{const a=d.analytics.find(item=>item.key===b.dataset.key);const next=!a.customer_enabled;if(!next&&!confirm('Turn off '+a.label+' on this camera? Its license assignment stays in place. Recording, live view, and playback continue.'))return;set(a.key,next)});list.querySelectorAll('button[data-assign]').forEach(b=>b.onclick=()=>{const a=d.analytics.find(item=>item.key===b.dataset.assign);const next=!a.assigned;if(!next&&!confirm('Release this camera’s '+a.label+' license assignment? Its purchased subscription remains active.'))return;assign(a.key,next)})}
        async function set(key,enabled){const r=await fetch('/api/customer/analytics-control/cameras/'+encodeURIComponent(sel.value)+'/'+encodeURIComponent(key),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})}).catch(()=>null);if(!r){note('Could not update analytics. Check your connection and try again.');return}if(!r.ok)note(msg(await body(r),'Could not update analytics.'));await load()}
        async function assign(key,assigned){const r=await fetch('/api/customer/analytics-control/cameras/'+encodeURIComponent(sel.value)+'/'+encodeURIComponent(key)+'/assignment',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({assigned})}).catch(()=>null);if(!r){note('Could not update the license assignment. Check your connection and try again.');return}if(!r.ok)note(msg(await body(r),'Could not update the license assignment.'));await load()}
        document.getElementById('acc-off').onclick=async()=>{if(!confirm('Turn off all your analytics for this camera? License assignments and subscriptions stay in place. Recording, live view, and playback continue.'))return;const r=await fetch('/api/customer/analytics-control/cameras/'+encodeURIComponent(sel.value)+'/turn-all-off',{method:'POST'}).catch(()=>null);if(!r){note('Could not turn analytics off. Check your connection and try again.');return}if(!r.ok)note(msg(await body(r),'Could not turn analytics off.'));await load()};sel.onchange=load;load();</script>'''.replace('__CAMERA_OPTIONS__', options)
        return page_shell("Analytics Control Center", "customer-app-settings", content)

    @app.get("/api/customer/analytics-control/cameras/{camera_id}")
    def get_controls(camera_id: str, request: Request):
        who = identity(request)
        data = []
        with connection() as db:
            camera = camera_access(db, camera_id, who, edit=False)
            from analytics_entitlements import commercial_camera_analytic_authorized
            for key, spec in ANALYTICS.items():
                rule_analytic = key in RULE_TYPES
                license_key = "smart_motion" if rule_analytic else key
                license_counts = analytics_license_summary(db, camera["customer_id"], camera["site_id"], license_key)
                row = db.execute("SELECT status FROM camera_analytics_entitlements WHERE camera_id=? AND analytic_key=?",
                                 (camera_id, key)).fetchone()
                commercial_assigned = bool(row and row["status"] == "active")
                if rule_analytic:
                    parent_row = db.execute("SELECT status FROM camera_analytics_entitlements WHERE camera_id=? AND analytic_key='smart_motion'",
                                            (camera_id,)).fetchone()
                    commercial_assigned = bool(parent_row and parent_row["status"] == "active")
                paid = commercial_camera_analytic_authorized(
                    db, customer_id=camera["customer_id"], site_id=camera["site_id"],
                    camera_id=camera_id, analytic_key=key)
                lab = active_lab_grant(db, camera, key)
                if rule_analytic:
                    lab = active_lab_grant(db, camera, "smart_motion")
                    paid = paid or commercial_camera_analytic_authorized(
                        db, customer_id=camera["customer_id"], site_id=camera["site_id"],
                        camera_id=camera_id, analytic_key="smart_motion")
                rule_ready = True
                if key == "people_counting":
                    rule_ready = _people_counting_line_ready(db, camera_id)
                elif rule_analytic:
                    rule_ready = bool(db.execute("SELECT 1 FROM customer_analytics_rules WHERE camera_id=? AND rule_type IN (" + ",".join("?" for _ in RULE_TYPES[key]) + ") LIMIT 1",
                                                  (camera_id, *RULE_TYPES[key])).fetchone())
                parent_ready = not rule_analytic or commercial_camera_analytic_authorized(
                    db, customer_id=camera["customer_id"], site_id=camera["site_id"],
                    camera_id=camera_id, analytic_key="smart_motion") or lab_assignment_enabled(db, camera_id, "smart_motion")
                if rule_analytic:
                    parent_ready = bool(parent_ready and operational_enabled(db, camera_id, "smart_motion"))
                lab_assignment = db.execute(
                    "SELECT a.grant_id FROM test_lab_analytics_assignments a "
                    "JOIN test_lab_grants g ON g.grant_id=a.grant_id "
                    "WHERE a.camera_id=? AND a.analytic_key=? AND a.enabled=1 AND g.status='active' LIMIT 1",
                    (camera_id, key),
                ).fetchone()
                lab_assigned = bool(lab_assignment)
                if lab_assigned:
                    grant = db.execute("SELECT payload_json FROM test_lab_grants WHERE grant_id=? AND status='active'",
                                       (lab_assignment["grant_id"],)).fetchone()
                    try:
                        lab_quantity = int(json.loads(grant["payload_json"])["analytics"][key])
                    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                        lab_quantity = 0
                    lab_used = db.execute("SELECT COUNT(*) AS n FROM test_lab_analytics_assignments "
                                          "WHERE grant_id=? AND analytic_key=? AND enabled=1",
                                          (lab_assignment["grant_id"], key)).fetchone()["n"]
                    license_counts = {"purchase_kind": "test_lab", "purchased_quantity": lab_quantity,
                                      "assigned_quantity": int(lab_used),
                                      "available_quantity": max(0, lab_quantity - int(lab_used))}
                assigned = commercial_assigned or lab_assigned
                if rule_analytic:
                    # Rule analytics are capabilities of an assigned Smart
                    # Motion seat, not separately purchasable/assignable seats.
                    assigned = commercial_assigned or lab_assignment_enabled(db, camera_id, "smart_motion")
                state = operational_status(db, camera_id, key)
                license_paused = commercial_assigned and not paid
                system_paused = state["system_paused"] or license_paused
                system_pause_reason = state["system_pause_reason"] or (
                    "Purchased quantity is below the current camera assignments; release a seat or restore the license quantity."
                    if license_paused else None)
                enabled = state["effective_enabled"] and assigned and bool(paid or lab) and rule_ready and parent_ready
                can_enable = assigned and bool(paid or lab) and rule_ready and parent_ready
                can_assign = (False if rule_analytic else assigned or license_counts["purchase_kind"] == "included"
                              or bool(license_counts["available_quantity"]) or bool(lab))
                data.append({"key": key, "label": spec["label"], "dependency": spec["dependency"], "enabled": enabled,
                             "assigned": assigned, "customer_enabled": state["customer_enabled"],
                             "system_paused": system_paused, "system_pause_reason": system_pause_reason,
                             "operational_status": "system_paused" if system_paused else "customer_off" if not state["customer_enabled"] else "on" if enabled else "off",
                             "license": license_counts, "can_assign": can_assign,
                             "assignment_required": not rule_analytic,
                             "license_basis": "smart_motion" if rule_analytic else key,
                             "dependency_ready": rule_ready and parent_ready,
                             "entitled": bool(paid or lab), "authorization": "test_lab" if lab and not paid else "commercial" if paid else "none",
                             "can_enable": can_enable or enabled})
        return {"camera_id": camera_id, "analytics": data}

    @app.put("/api/customer/analytics-control/cameras/{camera_id}/{analytic_key}")
    def set_control(camera_id: str, analytic_key: str, request: Request, payload: dict):
        who = identity(request)
        if analytic_key not in ANALYTICS or set(payload) != {"enabled"} or not isinstance(payload["enabled"], bool):
            raise HTTPException(status_code=400, detail="Invalid analytic control request.")
        enabled = payload["enabled"]
        now = datetime.now(timezone.utc).isoformat()
        with connection() as db:
            from database_backend import lock_customer_camera_capacity
            if not lock_customer_camera_capacity(db, who["customer_id"]):
                raise HTTPException(status_code=404, detail="Customer account not found.")
            camera = camera_access(db, camera_id, who, edit=True)
            if enabled:
                rule_analytic = analytic_key in RULE_TYPES
                license_key = "smart_motion" if rule_analytic else analytic_key
                row = db.execute("SELECT status FROM camera_analytics_entitlements WHERE camera_id=? AND analytic_key=?",
                                 (camera_id, analytic_key)).fetchone()
                commercial_assigned = bool(row and row["status"] == "active")
                if rule_analytic:
                    row = db.execute("SELECT status FROM camera_analytics_entitlements WHERE camera_id=? AND analytic_key='smart_motion'",
                                     (camera_id,)).fetchone()
                    commercial_assigned = bool(row and row["status"] == "active")
                from analytics_entitlements import commercial_camera_analytic_authorized
                paid = commercial_camera_analytic_authorized(
                    db, customer_id=camera["customer_id"], site_id=camera["site_id"],
                    camera_id=camera_id, analytic_key=license_key)
                assigned = commercial_assigned or lab_assignment_enabled(db, camera_id, analytic_key)
                lab = active_lab_grant(db, camera, analytic_key)
                parent_paid = False
                parent_lab = None
                if rule_analytic:
                    parent_paid = commercial_camera_analytic_authorized(
                        db, customer_id=camera["customer_id"], site_id=camera["site_id"],
                        camera_id=camera_id, analytic_key="smart_motion")
                    parent_lab = active_lab_grant(db, camera, "smart_motion")
                    assigned = commercial_assigned or lab_assignment_enabled(db, camera_id, "smart_motion")
                    lab = parent_lab
                if commercial_assigned and not (paid or (rule_analytic and parent_paid)):
                    raise HTTPException(status_code=409, detail="Assigned cameras exceed the current purchased quantity or the subscription is inactive. Release an assignment or restore licenses before enabling processing.")
                if not (paid or lab or parent_paid or parent_lab):
                    raise HTTPException(status_code=403, detail="A purchased entitlement or valid scoped Test Lab grant is required.")
                if not rule_analytic and not (paid or lab):
                    raise HTTPException(status_code=403, detail="A purchased entitlement or valid scoped Test Lab grant is required.")
                if not assigned:
                    message = ("Assign Smart Motion to this camera before enabling this rule." if rule_analytic
                               else "Assign a purchased analytic license to this camera before turning it on.")
                    raise HTTPException(status_code=409, detail=message)
                if rule_analytic:
                    args = (camera_id, *RULE_TYPES[analytic_key])
                    marks = ",".join("?" for _ in RULE_TYPES[analytic_key])
                    if not db.execute(f"SELECT 1 FROM customer_analytics_rules WHERE camera_id=? AND rule_type IN ({marks}) LIMIT 1", args).fetchone():
                        raise HTTPException(status_code=409, detail="Configure this rule on the camera before enabling it.")
                    parent_lab_assigned = bool(parent_lab and lab_assignment_enabled(db, camera_id, "smart_motion"))
                    if (not parent_paid and not parent_lab_assigned) or not operational_enabled(db, camera_id, "smart_motion"):
                        raise HTTPException(status_code=409, detail="Smart Motion must be enabled for rule analytics.")
                if analytic_key == "people_counting" and not _people_counting_line_ready(db, camera_id):
                    raise HTTPException(status_code=409, detail="Configure a counting line on the camera before enabling People Counting.")
                if lab and not paid and not rule_analytic:
                    grant_id = lab["grant_id"]
                    used = db.execute("SELECT COUNT(*) AS n FROM test_lab_analytics_assignments WHERE grant_id=? AND analytic_key=? AND enabled=1",
                                      (grant_id, analytic_key)).fetchone()["n"]
                    existing = db.execute("SELECT 1 FROM test_lab_analytics_assignments WHERE grant_id=? AND analytic_key=? AND camera_id=?",
                                          (grant_id, analytic_key, camera_id)).fetchone()
                    if not existing and used >= lab["analytics"][analytic_key]:
                        raise HTTPException(status_code=409, detail="The Test Lab grant quantity is already assigned.")
                    db.execute("INSERT INTO test_lab_analytics_assignments(grant_id,camera_id,analytic_key,enabled,updated_at) VALUES(?,?,?,?,?) "
                               "ON CONFLICT(grant_id,camera_id,analytic_key) DO UPDATE SET enabled=1,updated_at=excluded.updated_at",
                               (grant_id, camera_id, analytic_key, 1, now))
            db.execute("INSERT INTO camera_analytics_operations(camera_id,analytic_key,enabled,updated_at,updated_by) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(camera_id,analytic_key) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                       (camera_id, analytic_key, int(enabled), now, who.get("email")))
            _audit(db, who, "analytics.operational_toggle_changed", camera_id,
                   {"analytic_key": analytic_key, "enabled": enabled})
        refresh_appliance_runtime(camera_id)
        return {"camera_id": camera_id, "analytic_key": analytic_key, "enabled": enabled}

    @app.put("/api/customer/analytics-control/cameras/{camera_id}/{analytic_key}/assignment")
    def set_assignment(camera_id: str, analytic_key: str, request: Request, payload: dict):
        who = identity(request)
        if analytic_key not in ANALYTICS or set(payload) != {"assigned"} or not isinstance(payload["assigned"], bool):
            raise HTTPException(status_code=400, detail="Invalid analytic assignment request.")
        now = datetime.now(timezone.utc).isoformat()
        with connection() as db:
            camera = camera_access(db, camera_id, who, edit=True)
            from database_backend import lock_customer_camera_capacity
            if not lock_customer_camera_capacity(db, camera["customer_id"]):
                raise HTTPException(status_code=404, detail="Customer account not found.")
            if payload["assigned"]:
                existing = db.execute("SELECT 1 FROM camera_analytics_entitlements WHERE camera_id=? AND analytic_key=? AND status='active'",
                                      (camera_id, analytic_key)).fetchone()
                lab = active_lab_grant(db, camera, analytic_key) if not existing else None
                if lab:
                    grant_id = lab["grant_id"]
                    used = db.execute("SELECT COUNT(*) AS n FROM test_lab_analytics_assignments WHERE grant_id=? AND analytic_key=? AND enabled=1",
                                      (grant_id, analytic_key)).fetchone()["n"]
                    if not lab_assignment_enabled(db, camera_id, analytic_key) and used >= lab["analytics"][analytic_key]:
                        raise HTTPException(status_code=409, detail="The Test Lab grant quantity is already assigned.")
                    db.execute("INSERT INTO test_lab_analytics_assignments(grant_id,camera_id,analytic_key,enabled,updated_at) VALUES(?,?,?,?,?) "
                               "ON CONFLICT(grant_id,camera_id,analytic_key) DO UPDATE SET enabled=1,updated_at=excluded.updated_at",
                               (grant_id, camera_id, analytic_key, 1, now))
                else:
                    try:
                        assign_entitlement(db, camera_id, analytic_key, now=now, customer_lock_held=True)
                    except LicenseLimitExceeded as exc:
                        raise HTTPException(status_code=409, detail=str(exc)) from exc
                    except LookupError as exc:
                        raise HTTPException(status_code=404, detail=str(exc)) from exc
            else:
                if db.execute("SELECT 1 FROM camera_analytics_entitlements WHERE camera_id=? AND analytic_key=? AND status='active'",
                              (camera_id, analytic_key)).fetchone():
                    try:
                        remove_entitlement(db, camera_id, analytic_key, now=now, customer_lock_held=True)
                    except LookupError as exc:
                        raise HTTPException(status_code=404, detail=str(exc)) from exc
                db.execute("UPDATE test_lab_analytics_assignments SET enabled=0,updated_at=? WHERE camera_id=? AND analytic_key=?",
                           (now, camera_id, analytic_key))
                # Releasing a seat also stops the edge analytic on its next
                # authenticated config sync; the purchase itself is untouched.
                db.execute("INSERT INTO camera_analytics_operations(camera_id,analytic_key,enabled,updated_at,updated_by) "
                           "VALUES(?,?,0,?,?) ON CONFLICT(camera_id,analytic_key) DO UPDATE SET enabled=0,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                           (camera_id, analytic_key, now, who.get("email")))
            _audit(db, who, "analytics.assignment_changed", camera_id,
                   {"analytic_key": analytic_key, "assigned": payload["assigned"]})
        refresh_appliance_runtime(camera_id)
        return {"camera_id": camera_id, "analytic_key": analytic_key, "assigned": payload["assigned"]}

    @app.post("/api/customer/analytics-control/cameras/{camera_id}/turn-all-off")
    def turn_all_off(camera_id: str, request: Request):
        who = identity(request)
        now = datetime.now(timezone.utc).isoformat()
        with connection() as db:
            from database_backend import lock_customer_camera_capacity
            if not lock_customer_camera_capacity(db, who["customer_id"]):
                raise HTTPException(status_code=404, detail="Customer account not found.")
            camera_access(db, camera_id, who, edit=True)
            for key in ANALYTICS:
                db.execute("INSERT INTO camera_analytics_operations(camera_id,analytic_key,enabled,updated_at,updated_by) VALUES(?,?,0,?,?) "
                           "ON CONFLICT(camera_id,analytic_key) DO UPDATE SET enabled=0,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                           (camera_id, key, now, who.get("email")))
            _audit(db, who, "analytics.turn_all_off", camera_id, {"analytics": list(ANALYTICS)})
        refresh_appliance_runtime(camera_id)
        return {"camera_id": camera_id, "enabled": False, "analytics": list(ANALYTICS)}

    @app.post("/api/admin/test-lab/grants")
    def import_test_lab_grant(request: Request, payload: dict):
        actor = lab_administrator_identity(request)
        with connection() as db:
            try:
                return import_signed_grant(db, payload, actor)
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/admin/test-lab/grants/{grant_id}/revoke")
    def revoke_test_lab_grant_route(grant_id: str, request: Request):
        actor = lab_administrator_identity(request)
        if not _ID.fullmatch(grant_id):
            raise HTTPException(status_code=400, detail="Invalid Test Lab grant ID.")
        with connection() as db:
            if not revoke_test_lab_grant(db, grant_id, actor):
                raise HTTPException(status_code=404, detail="Active Test Lab grant not found.")
        return {"grant_id": grant_id, "status": "revoked"}


def import_signed_grant(db, envelope: dict, actor: dict) -> dict:
    """Privileged provisioning helper behind an administrator-only route.

    A controlled administrator workflow may call this with an already signed
    grant. The signing private key never enters the VMS process.
    """
    if not isinstance(actor, dict) or actor.get("role") != "administrator":
        raise PermissionError("Only an authorized administrator may import a Test Lab grant.")
    payload, reason = verify_grant(envelope)
    if not payload:
        raise ValueError(reason)
    now = datetime.now(timezone.utc).isoformat()
    db.execute("INSERT INTO test_lab_grants(grant_id,payload_json,signature,status,imported_at,imported_by) VALUES(?,?,?,'active',?,?) "
               "ON CONFLICT(grant_id) DO NOTHING",
               (payload["grant_id"], _canonical(payload).decode(), envelope["signature"], now, actor.get("email")))
    row = db.execute("SELECT payload_json,signature FROM test_lab_grants WHERE grant_id=?", (payload["grant_id"],)).fetchone()
    if not row or row["payload_json"] != _canonical(payload).decode() or row["signature"] != envelope["signature"]:
        raise ValueError("Test Lab grant ID already exists with different signed content.")
    _audit(db, actor, "test_lab.grant_imported", payload["grant_id"],
           {"environment_id": payload["environment_id"], "appliance_id": payload["appliance_id"],
            "site_id": payload["site_id"], "camera_count": len(payload["camera_ids"]), "analytics": payload["analytics"],
            "expires_at": payload["expires_at"]})
    return {"grant_id": payload["grant_id"], "expires_at": payload["expires_at"]}


def revoke_test_lab_grant(db, grant_id: str, actor: dict) -> bool:
    """Revoke a signed grant and its active assignments atomically."""
    if not isinstance(actor, dict) or actor.get("role") != "administrator":
        raise PermissionError("Only an authorized administrator may revoke a Test Lab grant.")
    now = datetime.now(timezone.utc).isoformat()
    result = db.execute("UPDATE test_lab_grants SET status='revoked',revoked_at=?,revoked_by=? WHERE grant_id=? AND status='active'",
                        (now, actor.get("email"), grant_id))
    if result.rowcount:
        db.execute("UPDATE test_lab_analytics_assignments SET enabled=0,updated_at=? WHERE grant_id=?", (now, grant_id))
        _audit(db, actor, "test_lab.grant_revoked", grant_id, {})
        return True
    return False


def _audit(db, actor: dict, action: str, entity_id: str, details: dict) -> None:
    db.execute("INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
               (actor.get("email"), actor.get("role"), action, "camera_analytics", entity_id,
                json.dumps(details, sort_keys=True), datetime.now(timezone.utc).isoformat()))


def _escape(value) -> str:
    from html import escape
    return escape(str(value or ""), quote=True)
