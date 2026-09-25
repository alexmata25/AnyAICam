"""Access-control API and page (2026-09-25): doors, status, manual
lock/unlock, device configuration and Z-Wave enrollment, on top of
access_control.AccessControlService.

Authorization is re-checked on every request, never trusted from a link or
a notification: the account owner (customer_owner) can do everything for
their own doors; a customer_viewer may unlock/lock a camera-linked door
only with that camera's existing can_unlock grant (the same grant the live
tile's Unlock button uses). Configuration and Z-Wave inclusion/exclusion
are owner-only. Another customer's door is a 404, never a 403 (no oracle).
Every unlock/lock attempt is also written to door_access_events -- the
same authorization-level audit the Unlock button, face rules, AACO and AAC
Voice Call already write -- while the service logs the hardware command.

The service runs where the door hardware is attached (the edge appliance);
a cloud portal without configured doors simply lists none.
"""
from __future__ import annotations

import time
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import access_control
import door_access
from partner_db import audit, connection
from partner_portal import partner_identity

_enrollment = None


def _identity(request: Request) -> dict:
    identity = partner_identity(request)
    if not identity or identity.get("role") not in {"customer_owner", "customer_viewer"} or not identity.get("customer_id"):
        raise HTTPException(status_code=403, detail="Customer access is required.")
    return identity


def _require_owner(identity: dict) -> None:
    if identity.get("role") != "customer_owner":
        raise HTTPException(status_code=403, detail="Only the account owner can configure access control.")


def _service() -> access_control.AccessControlService:
    service = access_control.get_service()
    if service is None:
        raise HTTPException(status_code=503, detail="Access control is disabled on this appliance.")
    return service


def _own_door(service, door_id: str, identity: dict) -> access_control.Door:
    door = service.door(door_id)
    if door is None or door.customer_id != identity["customer_id"]:
        raise HTTPException(status_code=404, detail="Door not found.")
    return door


def _authorize_command(door: access_control.Door, identity: dict) -> str | None:
    """Returns the acting user's id; raises 403 without permission."""
    with connection() as db:
        user = db.execute("SELECT id FROM partner_users WHERE lower(email)=?", (identity["email"].lower(),)).fetchone()
        user_id = user["id"] if user else None
        if identity.get("role") == "customer_owner":
            return user_id
        if not door.camera_id or not user_id:
            raise HTTPException(status_code=403, detail="You do not have permission to operate this door.")
        grant = db.execute("SELECT can_unlock FROM customer_camera_permissions WHERE user_id=? AND camera_id=?",
                           (user_id, door.camera_id)).fetchone()
    if not grant or not grant["can_unlock"]:
        raise HTTPException(status_code=403, detail="You do not have permission to operate this door.")
    return user_id


def _audit(door, identity, user_id, *, authorization: str, relay_result: str, success: bool, error=None) -> None:
    with connection() as db:
        door_access.record_door_access_event(
            db, customer_id=identity["customer_id"], camera_id=door.camera_id or "", door_name=door.name,
            relay_channel=door.config.get("channel"), trigger_type="manual", actor_user_id=user_id,
            actor_email=identity["email"], authorization_result=authorization, relay_result=relay_result,
            success=success, error=error, now=datetime.now())


def _outcome_http(outcome: access_control.Outcome) -> dict:
    if outcome.ok:
        return {"result": outcome.result, "state": outcome.state, "relock_at": outcome.relock_at, "command_id": outcome.command_id}
    status = {"duplicate": 409, "dry_run": 409, "uncertain_state": 409, "offline": 503, "timeout": 504,
              "failed": 502, "denied": 403}.get(outcome.result, 409)
    raise HTTPException(status_code=status, detail={"result": outcome.result, "message": outcome.detail or outcome.result,
                                                    "command_id": outcome.command_id})


def _door_from_payload(identity: dict, payload: dict, existing: access_control.Door | None) -> access_control.Door:
    camera_id = payload.get("camera_id", existing.camera_id if existing else None) or None
    if camera_id:
        with connection() as db:
            if not db.execute("SELECT 1 FROM cameras WHERE id=? AND customer_id=?", (camera_id, identity["customer_id"])).fetchone():
                raise HTTPException(status_code=404, detail="Camera not found.")
    config = payload.get("config", existing.config if existing else {})
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail="config must be an object.")
    return access_control.Door(
        id=existing.id if existing else access_control.new_door_id(), customer_id=identity["customer_id"],
        name=str(payload.get("name", existing.name if existing else "")).strip(),
        kind=str(payload.get("kind", existing.kind if existing else "")),
        config=config, camera_id=camera_id,
        unlock_seconds=int(payload.get("unlock_seconds", existing.unlock_seconds if existing else access_control.DEFAULT_UNLOCK_SECONDS)),
        enabled=bool(payload.get("enabled", existing.enabled if existing else True)),
        # New doors start in dry run: a real command needs an explicit opt-in.
        dry_run=bool(payload.get("dry_run", existing.dry_run if existing else True)),
    )


def enrollment():
    global _enrollment
    if _enrollment is None:
        import access_zwave
        _enrollment = access_zwave.ZWaveEnrollment(_service().adapters.zwave_client())
    return _enrollment


def register_access_control_routes(app: FastAPI, page_shell=None) -> None:
    @app.get("/api/customer/access/doors")
    def list_doors(request: Request) -> dict:
        identity = _identity(request)
        service = access_control.get_service()
        if service is None:
            return {"doors": [], "enabled": False}
        doors = service.list_doors(identity["customer_id"])
        return {"enabled": True, "doors": [service.get_status(door.id) for door in doors]}

    @app.get("/api/customer/access/doors/{door_id}")
    def door_detail(request: Request, door_id: str) -> dict:
        identity = _identity(request)
        service = _service()
        door = _own_door(service, door_id, identity)
        status = service.get_status(door.id)
        status["health"] = service.health(door.id)
        if identity.get("role") == "customer_owner":
            status["config"] = door.config
            status["unlock_seconds"] = door.unlock_seconds
        return status

    @app.post("/api/customer/access/doors/{door_id}/unlock")
    def unlock(request: Request, door_id: str, payload: dict | None = None) -> dict:
        identity = _identity(request)
        service = _service()
        door = _own_door(service, door_id, identity)
        try:
            user_id = _authorize_command(door, identity)
        except HTTPException as error:
            _audit(door, identity, None, authorization="denied", relay_result="skipped", success=False, error=error.detail)
            raise
        duration = (payload or {}).get("duration_seconds")
        if duration is not None and (not isinstance(duration, (int, float)) or duration <= 0):
            raise HTTPException(status_code=400, detail="duration_seconds must be a positive number.")
        outcome = service.unlock(door.id, duration_seconds=duration, reason="manual_unlock", actor=identity["email"], trigger="manual")
        _audit(door, identity, user_id, authorization="authorized",
               relay_result="activated" if outcome.ok else outcome.result, success=outcome.ok, error=outcome.detail)
        return _outcome_http(outcome)

    @app.post("/api/customer/access/doors/{door_id}/lock")
    def lock(request: Request, door_id: str) -> dict:
        identity = _identity(request)
        service = _service()
        door = _own_door(service, door_id, identity)
        user_id = _authorize_command(door, identity)
        outcome = service.lock(door.id, reason="manual_lock", actor=identity["email"], trigger="manual")
        return _outcome_http(outcome)

    @app.get("/api/customer/access/doors/{door_id}/history")
    def history(request: Request, door_id: str) -> dict:
        identity = _identity(request)
        service = _service()
        door = _own_door(service, door_id, identity)
        _authorize_command(door, identity)
        with connection() as db:
            commands = [dict(row) for row in db.execute(
                "SELECT id,command,trigger_type,actor,reason,person_id,facial_event_id,result,detail,duration_seconds,created_at "
                "FROM access_door_commands WHERE door_id=? ORDER BY created_at DESC LIMIT 100", (door.id,))]
            events = [dict(row) for row in db.execute(
                "SELECT id,event_type,detail_json,created_at FROM access_door_events WHERE door_id=? ORDER BY created_at DESC LIMIT 100",
                (door.id,))]
        return {"commands": commands, "events": events}

    @app.post("/api/customer/access/doors")
    def save_door(request: Request, payload: dict) -> dict:
        identity = _identity(request)
        _require_owner(identity)
        service = _service()
        existing = _own_door(service, payload["id"], identity) if payload.get("id") else None
        door = _door_from_payload(identity, payload, existing)
        try:
            service.configure_door(door)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        audit(identity, "access_door.configured", "access_door", door.id,
              {"kind": door.kind, "camera_id": door.camera_id, "dry_run": door.dry_run, "enabled": door.enabled})
        return {"door_id": door.id, "status": service.get_status(door.id)}

    @app.delete("/api/customer/access/doors/{door_id}")
    def delete_door(request: Request, door_id: str) -> dict:
        identity = _identity(request)
        _require_owner(identity)
        service = _service()
        door = _own_door(service, door_id, identity)
        service.lock(door.id, reason="door removed", actor=identity["email"])
        service.remove_door(door.id)
        audit(identity, "access_door.removed", "access_door", door.id, {})
        return {"removed": door.id}

    @app.get("/api/customer/access/zwave")
    def zwave_status(request: Request) -> dict:
        identity = _identity(request)
        _require_owner(identity)
        import access_zwave
        client = _service().adapters.zwave_client()
        # A missing Z-Wave server must not hold a request for the connect
        # timeout on every status poll: retry at most every 30 s.
        if not client.connected and time.monotonic() - getattr(client, "_last_attempt", -1e9) >= 30:
            client._last_attempt = time.monotonic()
            try:
                client.ensure_connected()
            except Exception as error:
                client.last_error = str(error)
        nodes = [access_zwave.node_summary(node) for node in client.nodes.values()]
        return {"controller": client.health(), "sticks": access_zwave.discover_zsticks(),
                "enrollment": enrollment().status(), "nodes": sorted(nodes, key=lambda n: n["node_id"] or 0)}

    @app.post("/api/customer/access/zwave/inclusion")
    def zwave_inclusion(request: Request, payload: dict) -> dict:
        identity = _identity(request)
        _require_owner(identity)
        from access_adapters import AdapterError
        action = payload.get("action")
        try:
            if action == "start":
                result = enrollment().start_inclusion()
            elif action == "pin":
                result = enrollment().submit_pin(payload.get("pin"))
            elif action == "stop":
                result = enrollment().stop()
            else:
                raise HTTPException(status_code=400, detail="action must be start, pin or stop.")
        except AdapterError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        audit(identity, f"zwave.inclusion_{action}", "zwave", "", {})  # never the PIN
        return result

    @app.post("/api/customer/access/zwave/exclusion")
    def zwave_exclusion(request: Request, payload: dict) -> dict:
        identity = _identity(request)
        _require_owner(identity)
        from access_adapters import AdapterError
        try:
            result = enrollment().start_exclusion() if payload.get("action") == "start" else enrollment().stop()
        except AdapterError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        audit(identity, f"zwave.exclusion_{payload.get('action')}", "zwave", "", {})
        return result

    @app.get("/api/customer/access/relay/devices")
    def relay_devices(request: Request) -> dict:
        identity = _identity(request)
        _require_owner(identity)
        from access_adapters import discover_serial_devices
        return {"devices": discover_serial_devices(("Numato", "relay", "CDC"))}

    if page_shell is None:
        return

    @app.get("/customer/access-control", response_class=HTMLResponse)
    def access_control_page(request: Request):
        identity = partner_identity(request)
        if not identity or identity.get("role") not in {"customer_owner", "customer_viewer"}:
            return RedirectResponse("/customer-login.html", status_code=303)
        owner = identity.get("role") == "customer_owner"
        content = (
            '<header class="topbar"><div><p class="eyebrow">Access control</p><h1>Doors</h1></div></header>'
            '<section class="panel"><div id="access-doors" class="settings-list">Loading&hellip;</div></section>'
            + ('<section class="panel" style="margin-top:14px"><div class="panel-head"><div><h2>Z-Wave locks</h2>'
               '<div class="health-detail">Add a lock: press Start, then follow the lock\'s own pairing steps. '
               'An S2 lock asks for the 5-digit PIN printed on it.</div></div></div>'
               '<div id="zwave-status" class="health-detail">Loading&hellip;</div>'
               '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">'
               '<button class="action-button" id="zwave-include" type="button">Start adding a lock</button>'
               '<input id="zwave-pin" inputmode="numeric" maxlength="5" placeholder="S2 PIN" style="width:90px">'
               '<button class="ghost-button" id="zwave-pin-send" type="button">Send PIN</button>'
               '<button class="ghost-button" id="zwave-exclude" type="button">Remove a lock</button>'
               '<button class="ghost-button" id="zwave-stop" type="button">Stop</button></div></section>' if owner else ""))
        script = """<script>(function(){
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path,options){const r=await fetch(path,Object.assign({credentials:'same-origin',headers:{'Content-Type':'application/json'}},options||{}));
  const body=await r.json().catch(()=>({}));if(!r.ok)throw new Error((body.detail&&body.detail.message)||body.detail||'Request failed');return body;}
async function loadDoors(){const el=document.getElementById('access-doors');
  try{const data=await api('/api/customer/access/doors');
    if(!data.doors.length){el.textContent='No doors are configured yet.';return;}
    el.innerHTML=data.doors.map(d=>`<div class="settings-row" data-door="${esc(d.door_id)}"><div><strong>${esc(d.name)}</strong>
      <div class="health-detail">${esc(d.kind)} &middot; ${d.online?esc(d.state):'offline'}${d.dry_run?' &middot; dry run':''}${d.relock_at?' &middot; relocks '+esc(new Date(d.relock_at).toLocaleTimeString()):''}</div></div>
      <div style="display:flex;gap:8px"><button class="action-button" data-unlock type="button">Unlock</button><button class="ghost-button" data-lock type="button">Lock</button></div></div>`).join('');
    el.querySelectorAll('[data-door]').forEach(row=>{const id=row.dataset.door;
      row.querySelector('[data-unlock]').onclick=()=>api(`/api/customer/access/doors/${id}/unlock`,{method:'POST',body:'{}'}).then(()=>{showToast('Unlocked');loadDoors()}).catch(e=>showToast(e.message));
      row.querySelector('[data-lock]').onclick=()=>api(`/api/customer/access/doors/${id}/lock`,{method:'POST'}).then(()=>{showToast('Locked');loadDoors()}).catch(e=>showToast(e.message));});
  }catch(e){el.textContent=e.message;}}
async function loadZwave(){const el=document.getElementById('zwave-status');if(!el)return;
  try{const z=await api('/api/customer/access/zwave');
    el.innerHTML=`Controller: ${z.controller.controller_online?'connected':'not connected'} &middot; Adding/removing: ${esc(z.enrollment.state)}${z.enrollment.error?' &middot; '+esc(z.enrollment.error):''}<br>`+
      z.nodes.filter(n=>n.is_lock).map(n=>`Node ${n.node_id}: ${esc(n.label||'lock')} (${esc(n.status)}${n.is_secure?', secure':', NOT secure'})`).join('<br>');
  }catch(e){el.textContent=e.message;}}
const bind=(id,fn)=>{const b=document.getElementById(id);if(b)b.onclick=()=>fn().then(loadZwave).catch(e=>showToast(e.message));};
bind('zwave-include',()=>api('/api/customer/access/zwave/inclusion',{method:'POST',body:JSON.stringify({action:'start'})}));
bind('zwave-pin-send',()=>api('/api/customer/access/zwave/inclusion',{method:'POST',body:JSON.stringify({action:'pin',pin:document.getElementById('zwave-pin').value})}));
bind('zwave-exclude',()=>api('/api/customer/access/zwave/exclusion',{method:'POST',body:JSON.stringify({action:'start'})}));
bind('zwave-stop',()=>api('/api/customer/access/zwave/inclusion',{method:'POST',body:JSON.stringify({action:'stop'})}));
loadDoors();loadZwave();setInterval(loadDoors,10000);setInterval(loadZwave,3000);
})();</script>"""
        return page_shell("Access control", "access-control", content, script)
