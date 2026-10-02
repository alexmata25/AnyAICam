"""AACO settings (first release, 2026-09-30).

Per-customer controls over what the AACO assistant may do, enforced on the server
between parsing and execution. AACO's free-form (typed or spoken) understanding is
untouched: settings only decide whether an already-understood request may run.

- enabled: AACO on/off for the account.
- show_floating: the floating assistant button on portal pages.
- camera_scope/camera_ids: all cameras, or only the selected ones.
- allow_live / allow_playback (playback and event search) / allow_talk.
- allow_door_actions: OFF by default. Turning it on needs the account owner's
  explicit confirmation, and every unlock still requires the person's own door
  permission (vms.unlock_door() is unchanged).

Only the account owner (customer_owner) may change settings; viewers see them
read-only. Defaults keep today's behavior except door actions, which start off.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from html import escape

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from partner_db import audit, connection

DEFAULTS = {
    "enabled": True,
    "show_floating": True,
    "camera_scope": "all",
    "camera_ids": [],
    "allow_live": True,
    "allow_playback": True,
    "allow_talk": True,
    "allow_door_actions": False,
}
_BOOLEANS = ("enabled", "show_floating", "allow_live", "allow_playback", "allow_talk", "allow_door_actions")

# Which setting an understood operation needs. camera_status needs none.
OPERATION_SETTING = {
    "live_view": "allow_live",
    "playback": "allow_playback",
    "playback_navigation": "allow_playback",
    "event_search": "allow_playback",
    "event_navigation": "allow_playback",
    "talk": "allow_talk",
    "unlock_door": "allow_door_actions",
}
NOT_ALLOWED_MESSAGE = {
    "allow_live": "Live view through AACO is turned off for this account.",
    "allow_playback": "Playback and event search through AACO are turned off for this account.",
    "allow_talk": "Talking through cameras with AACO is turned off for this account.",
    "allow_door_actions": "Door actions through AACO are turned off for this account. The account owner can turn them on in Settings → AACO.",
}
DISABLED_MESSAGE = "AACO is turned off for this account. The account owner can turn it on in Settings → AACO."
CAMERA_NOT_ALLOWED_MESSAGE = "AACO isn't allowed to use that camera on this account."

DDL = ("CREATE TABLE IF NOT EXISTS aaco_customer_settings(customer_id TEXT PRIMARY KEY, settings_json TEXT NOT NULL, "
       "updated_at TEXT NOT NULL, updated_by TEXT)")


def get_settings(db, customer_id: str) -> dict:
    db.execute(DDL)
    found = db.execute("SELECT settings_json FROM aaco_customer_settings WHERE customer_id=?", (customer_id,)).fetchone()
    settings = dict(DEFAULTS)
    if found:
        try:
            stored = json.loads(found["settings_json"])
        except (TypeError, ValueError):
            stored = {}
        for key in DEFAULTS:
            if key in stored:
                settings[key] = stored[key]
    return settings


def load(customer_id: str | None) -> dict:
    if not customer_id:
        return dict(DEFAULTS)
    with connection() as db:
        return get_settings(db, customer_id)


def customer_cameras(db, customer_id: str) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT id,name,camera_number FROM cameras WHERE customer_id=? AND camera_number IS NOT NULL "
        "AND COALESCE(status,'') NOT IN ('removed','pending_installation') ORDER BY camera_number,id", (customer_id,)).fetchall()]


def camera_label(camera: dict) -> str:
    return str(camera.get("name") or "").strip() or f"Camera {camera.get('camera_number')}"


def validate(db, customer_id: str, payload: object) -> dict:
    if not isinstance(payload, dict) or set(payload) - set(DEFAULTS) - {"confirm_door_actions"}:
        raise ValueError("Unknown AACO setting.")
    current = get_settings(db, customer_id)
    updated = dict(current)
    for key in _BOOLEANS:
        if key in payload:
            if not isinstance(payload[key], bool):
                raise ValueError(f"{key} must be true or false.")
            updated[key] = payload[key]
    if "camera_scope" in payload:
        if payload["camera_scope"] not in ("all", "selected"):
            raise ValueError("camera_scope must be 'all' or 'selected'.")
        updated["camera_scope"] = payload["camera_scope"]
    if "camera_ids" in payload:
        ids = payload["camera_ids"]
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids) or len(ids) > 500:
            raise ValueError("camera_ids must be a list of camera IDs.")
        own = {c["id"] for c in customer_cameras(db, customer_id)}
        if set(ids) - own:
            raise ValueError("Some cameras are not on this account.")
        updated["camera_ids"] = sorted(set(ids))
    # Turning door actions on is an explicit, confirmed owner decision.
    if updated["allow_door_actions"] and not current["allow_door_actions"] and payload.get("confirm_door_actions") is not True:
        raise ValueError("Confirm that AACO may perform door actions.")
    return updated


def save(customer_id: str, payload: object, actor: dict) -> dict:
    with connection() as db:
        updated = validate(db, customer_id, payload)
        db.execute(DDL)
        now = datetime.now(timezone.utc).isoformat()
        existing = db.execute("SELECT 1 FROM aaco_customer_settings WHERE customer_id=?", (customer_id,)).fetchone()
        if existing:
            db.execute("UPDATE aaco_customer_settings SET settings_json=?,updated_at=?,updated_by=? WHERE customer_id=?",
                       (json.dumps(updated), now, actor.get("email"), customer_id))
        else:
            db.execute("INSERT INTO aaco_customer_settings(customer_id,settings_json,updated_at,updated_by) VALUES(?,?,?,?)",
                       (customer_id, json.dumps(updated), now, actor.get("email")))
    audit(actor, "aaco.settings_updated", "customer", customer_id, updated)
    return updated


def allowed_camera_ids(settings: dict) -> set[str] | None:
    """None means every camera the person may already see."""
    return None if settings.get("camera_scope") != "selected" else set(settings.get("camera_ids") or [])


def operation_denial(settings: dict, operation: str) -> str | None:
    if not settings.get("enabled", True):
        return DISABLED_MESSAGE
    key = OPERATION_SETTING.get(operation)
    if key and not settings.get(key, DEFAULTS[key]):
        return NOT_ALLOWED_MESSAGE[key]
    return None


class RestrictedBoundary:
    """Wraps the VMS boundary so AACO only ever reaches allowed cameras.

    execute() sends every camera-taking operation through authorized_camera()
    first; a camera outside the allowed set resolves to nothing there, which
    fails closed exactly like a camera the person cannot see. Event searches
    and camera status are filtered to allowed cameras. Everything else passes
    through unchanged."""

    def __init__(self, inner, *, allowed_ids: set[str], allowed_labels: set[str]):
        self._inner = inner
        self._allowed_ids = allowed_ids
        self._allowed_labels = {" ".join(label.lower().split()) for label in allowed_labels}

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def authorized_camera(self, identity, camera_id):
        result = self._inner.authorized_camera(identity, camera_id)
        if isinstance(result, dict) and result.get("id") not in self._allowed_ids:
            return None
        return result

    def camera_names(self, identity):
        return [name for name in self._inner.camera_names(identity) if " ".join(name.lower().split()) in self._allowed_labels]

    def search_events(self, identity, **kwargs):
        result = self._inner.search_events(identity, **kwargs)
        if isinstance(result, dict) and isinstance(result.get("events"), list):
            from urllib.parse import parse_qs, urlsplit
            kept = [e for e in result["events"]
                    if (parse_qs(urlsplit(str(e.get("href", ""))).query).get("camera") or [""])[0] in self._allowed_ids]
            if len(kept) != len(result["events"]):
                result = dict(result, events=kept, context=kept[0]["context"] if kept else None)
                result["message"] = ("Latest event." if kept else "No matching events yet.") if kwargs.get("limit") == 1 else \
                    f"{len(kept)} event{'s' if len(kept) != 1 else ''} found."
        return result

    def camera_status(self, identity):
        result = self._inner.camera_status(identity)
        if isinstance(result, dict) and isinstance(result.get("cameras"), list):
            kept = [c for c in result["cameras"] if " ".join(str(c.get("label", "")).lower().split()) in self._allowed_labels]
            result = dict(result, cameras=kept, message=f"Status requested for {len(kept)} authorized camera(s).")
        return result

    def unlock_door(self, identity, door_id):
        """Only doors on allowed cameras (2026-10-02, Codex launch blocker).

        The VMS boundary resolves every token form (camera-name:, camera-N,
        ...) to the door's camera id and checks it against allowed_ids
        there, before any authorization or relay call -- a name check here
        alone let "camera-2" through to an excluded camera. An inner
        boundary that cannot take allowed_ids fails closed: only an exact,
        allowed camera name passes."""
        import inspect
        from aaco import Clarification
        try:
            scoped = "allowed_ids" in inspect.signature(self._inner.unlock_door).parameters
        except (TypeError, ValueError):
            scoped = False
        if scoped:
            return self._inner.unlock_door(identity, door_id, allowed_ids=set(self._allowed_ids))
        token = str(door_id)
        requested = " ".join(token.removeprefix("camera-name:").lower().split())
        if not token.startswith("camera-name:") or requested not in self._allowed_labels:
            return Clarification(CAMERA_NOT_ALLOWED_MESSAGE)
        return self._inner.unlock_door(identity, door_id)


def restrict(vms, settings: dict, customer_id: str):
    allowed = allowed_camera_ids(settings)
    if allowed is None:
        return vms
    with connection() as db:
        labels = {camera_label(c) for c in customer_cameras(db, customer_id) if c["id"] in allowed}
    return RestrictedBoundary(vms, allowed_ids=allowed, allowed_labels=labels)


# ---------------------------------------------------------------- settings page

def render_page(identity: dict) -> tuple[str, str]:
    customer_id = identity["customer_id"]
    is_owner = identity.get("role") == "customer_owner"
    with connection() as db:
        settings = get_settings(db, customer_id)
        cameras = customer_cameras(db, customer_id)
    disabled = "" if is_owner else " disabled"

    def toggle(key, label, help_text):
        return (f'<label class="aaco-toggle"><input type="checkbox" data-setting="{key}"'
                f'{" checked" if settings[key] else ""}{disabled}><span><strong>{escape(label)}</strong>'
                f'<small>{escape(help_text)}</small></span></label>')

    selected = set(settings["camera_ids"])
    camera_boxes = "".join(
        f'<label class="aaco-camera"><input type="checkbox" class="aaco-camera-box" value="{escape(c["id"], quote=True)}"'
        f'{" checked" if c["id"] in selected else ""}{disabled}> {escape(camera_label(c))}</label>' for c in cameras
    ) or '<div class="health-detail">No cameras are set up on your account yet.</div>'
    owner_note = "" if is_owner else '<div class="health-detail" style="margin-top:8px">Only the account owner can change these settings.</div>'
    content = f'''
    <style>
      .aaco-settings .panel{{margin-top:14px}}
      .aaco-toggle{{display:flex;gap:12px;align-items:flex-start;margin:12px 0}}
      .aaco-toggle input,.aaco-camera input{{width:20px;height:20px;margin-top:2px;flex:none}}
      .aaco-toggle small{{display:block;color:var(--muted,#9aa7b5);font-weight:400;margin-top:2px}}
      .aaco-scope{{display:flex;gap:18px;flex-wrap:wrap;margin:8px 0}}
      .aaco-camera-list{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;margin-top:8px}}
      .aaco-camera-list[hidden]{{display:none}}
      .aaco-camera{{display:flex;gap:8px;align-items:center}}
      .aaco-status{{margin-top:10px}}
    </style>
    <header class="topbar">
      <div><p class="eyebrow">Settings</p><h1>AACO assistant</h1></div>
      <a class="ghost-button" href="/customer-app-settings">All settings</a>
    </header>
    <div class="aaco-settings">
    <section class="panel">
      <div class="health-detail">AACO understands everyday requests like "show the driveway at 3 PM yesterday" or
      "anyone at the front door today?". These settings decide what it is allowed to do on your account.</div>{owner_note}
      {toggle("enabled", "Use AACO", "Turn the assistant on or off for everyone on this account.")}
      {toggle("show_floating", "Show the AACO button on every page", "The round assistant button in the corner. The AACO page stays available either way.")}
    </section>
    <section class="panel">
      <div class="panel-head"><h2>Cameras AACO may use</h2></div>
      <div class="aaco-scope">
        <label><input type="radio" name="aaco-scope" value="all"{" checked" if settings["camera_scope"] == "all" else ""}{disabled}> All cameras each person can already see</label>
        <label><input type="radio" name="aaco-scope" value="selected"{" checked" if settings["camera_scope"] == "selected" else ""}{disabled}> Only these cameras</label>
      </div>
      <div class="aaco-camera-list" id="aaco-camera-list"{"" if settings["camera_scope"] == "selected" else " hidden"}>{camera_boxes}</div>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>What AACO may do</h2></div>
      {toggle("allow_live", "Open live view", "“Show the front door”.")}
      {toggle("allow_playback", "Find recordings and events", "“Play the driveway at 3 PM”, “any people today?”.")}
      {toggle("allow_talk", "Talk through a camera", "“Talk to the front door”. Uses each person's own Talk permission.")}
      {toggle("allow_door_actions", "Door actions (unlock)", "Off by default. Each unlock still needs the person's own door permission.")}
    </section>
    {'<button class="action-button" id="aaco-save" type="button">Save AACO settings</button>' if is_owner else ''}
    <p class="health-detail aaco-status" id="aaco-status" role="status"></p>
    </div>'''
    scripts = '''<script>
    (function(){
      const list=document.getElementById('aaco-camera-list');
      document.querySelectorAll('input[name="aaco-scope"]').forEach(r=>r.addEventListener('change',()=>{list.hidden=r.value!=='selected'||!r.checked}));
      const save=document.getElementById('aaco-save');
      if(!save)return;
      const status=document.getElementById('aaco-status');
      const doorBox=document.querySelector('[data-setting="allow_door_actions"]');
      const doorWasOn=doorBox.checked;
      save.addEventListener('click',async()=>{
        const body={};
        document.querySelectorAll('[data-setting]').forEach(box=>{body[box.dataset.setting]=box.checked});
        body.camera_scope=(document.querySelector('input[name="aaco-scope"]:checked')||{}).value||'all';
        body.camera_ids=[...document.querySelectorAll('.aaco-camera-box:checked')].map(b=>b.value);
        if(body.camera_scope==='selected'&&!body.camera_ids.length){status.textContent='Choose at least one camera, or allow all cameras.';return}
        if(body.allow_door_actions&&!doorWasOn){
          if(!window.confirm('Allow AACO to unlock doors? Each unlock will still require the person\\'s own door permission.')){status.textContent='Door actions were not turned on.';return}
          body.confirm_door_actions=true;
        }
        save.disabled=true;status.textContent='Saving…';
        try{
          const response=await fetch('/api/customer/aaco/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
          let data={};try{data=await response.json()}catch(e){}
          if(!response.ok)throw new Error(data.detail||'Could not save. Please try again.');
          status.textContent='Saved.';
        }catch(e){status.textContent=e.message}
        finally{save.disabled=false}
      });
    })();
    </script>'''
    return content, scripts


def register_routes(app, shell, identity_provider):
    def customer(request):
        identity = identity_provider(request)
        if not identity or identity.get("role") not in {"customer_owner", "customer_viewer"} or not identity.get("customer_id"):
            raise HTTPException(status_code=403, detail="Customer sign-in required.")
        return identity

    @app.get("/customer/aaco-settings", response_class=HTMLResponse)
    def aaco_settings_page(request: Request):
        content, scripts = render_page(customer(request))
        return shell("AACO settings", "customer-app-settings", content, scripts)

    @app.get("/api/customer/aaco/settings")
    def aaco_settings_get(request: Request):
        identity = customer(request)
        return {"settings": load(identity["customer_id"]), "can_change": identity["role"] == "customer_owner"}

    @app.put("/api/customer/aaco/settings")
    async def aaco_settings_put(request: Request):
        identity = customer(request)
        if identity["role"] != "customer_owner":
            raise HTTPException(status_code=403, detail="Only the account owner can change AACO settings.")
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Settings must be valid JSON.")
        try:
            return {"settings": save(identity["customer_id"], payload, identity)}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
