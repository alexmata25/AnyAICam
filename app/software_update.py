"""Settings -> System -> Software Update (2026-10-03).

Customer-facing half of Software Update. The account OWNER (and only the
owner -- checked here, server-side) sees the installed release of each of
their appliances, the published offline-signed release, and can install it
after an explicit confirmation. Household members (customer_viewer) see the
status read-only. Partners and the Admin Portal bridge cannot install
(appliance_cloud.queue_command refuses install_update).

An install request queues one install_update command naming one exact
release (update_id, version, sha256). The appliance agent verifies and
stages it; its root applier re-verifies everything, swaps the application
directory, validates the running version/build and rolls back on failure.
Progress and the outcome come back through the existing update ledger
(appliance_update_results): one row per update and appliance holding the
CURRENT state, so a browser refresh or a VMS restart never loses it. A final
outcome is never overwritten.

No schema change: the ledger, appliance_commands and appliances.software_version
(now '1.2.0+<build12>', reported by the agent from the root-written release
marker) already exist.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta
from html import escape
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from partner_db import audit, connection

TARGET = "anyaicam-appliance"
CHANNEL = "stable"
COMMAND_TTL_HOURS = 6
FINAL_STATES = {"healthy", "rolled_back", "rollback_failed", "rejected", "install_failed", "download_failed",
                "verify_failed", "activation_failed"}
IN_PROGRESS_LABELS = {
    "requested": "Waiting for the appliance",
    "validating_manifest": "Checking the release",
    "downloading": "Downloading",
    "downloaded": "Downloaded",
    "verifying": "Verifying",
    "verified": "Verified",
    "staged": "Ready to install",
    "activation_requested": "Starting installation",
    "installing": "Installing",
    "health_checking": "Checking the new version",
    "rolling_back": "Restoring the previous version",
}
FINAL_LABELS = {
    "healthy": "Updated",
    "rolled_back": "Update failed -- previous version restored",
    "rollback_failed": "Update failed -- the previous version could not be restored automatically",
    "rejected": "Update refused",
    "install_failed": "Update failed -- nothing was changed",
    "download_failed": "Download failed -- nothing was changed",
    "verify_failed": "Verification failed -- nothing was changed",
    "activation_failed": "Update failed",
}
_VERSION = re.compile(r"^\d{1,4}(\.\d{1,4}){1,3}$")


# ------------------------------------------------------------------ helpers

def _version_tuple(version: str) -> tuple | None:
    if not isinstance(version, str) or not _VERSION.match(version):
        return None
    return tuple(int(part) for part in version.split("."))


def is_newer(candidate: str, current: str) -> bool:
    new = _version_tuple(candidate)
    if new is None:
        return False
    old = _version_tuple(current)
    if old is None:
        return not current  # unknown legacy label: let the appliance decide (it refuses downgrades)
    width = max(len(new), len(old))
    return new + (0,) * (width - len(new)) > old + (0,) * (width - len(old))


def split_release_label(label: str) -> tuple[str, str]:
    """'1.2.0+3f2a9c1b0d4e' -> ('1.2.0', '3f2a9c1b0d4e'); a legacy label -> ('', '')."""
    if not isinstance(label, str) or "+" not in label:
        return "", ""
    version, _, build = label.partition("+")
    if not _VERSION.match(version) or not re.fullmatch(r"[0-9a-f]{7,40}", build):
        return "", ""
    return version, build


def record_update_progress(db, *, update_id: str, appliance_id: str, payload: dict, state: str, now: str) -> bool:
    """Moves an existing, not-yet-final ledger row to `state`. False when
    there is no row (the caller inserts one) or the row is already final
    (the caller's insert then conflicts: a final outcome is never
    overwritten)."""
    row = db.execute("SELECT state FROM appliance_update_results WHERE update_id=? AND appliance_id=?",
                     (update_id, appliance_id)).fetchone()
    if row is None or row["state"] in FINAL_STATES:
        return False
    db.execute(
        "UPDATE appliance_update_results SET state=?,from_version=COALESCE(?,from_version),to_version=COALESCE(?,to_version),"
        "error=?,rollback_from=?,duration_seconds=?,reported_at=? WHERE update_id=? AND appliance_id=?",
        (state, payload.get("from_version") or None, payload.get("to_version") or None, str(payload.get("error", ""))[:500],
         payload.get("rollback_from"), payload.get("duration_seconds"), now, update_id, appliance_id),
    )
    return True


def published_release() -> dict | None:
    """The offline-signed release currently published for appliances, or
    None. An unsigned catalog record is never offered."""
    from updates_storage import get_latest_release
    record = get_latest_release(TARGET, CHANNEL)
    if not record or not isinstance(record.get("signature"), str) or not record.get("signature"):
        return None
    manifest = record["manifest"]
    if not _version_tuple(str(manifest.get("version", ""))):
        return None
    return {"update_id": str(manifest.get("update_id", "")), "version": str(manifest.get("version", "")),
            "build_id": str(manifest.get("build_id", "")), "sha256": str(manifest.get("sha256", "")),
            "issued_at": str(manifest.get("issued_at", "")), "package_size_bytes": manifest.get("package_size_bytes")}


def _identity(request: Request) -> dict | None:
    from partner_portal import partner_identity
    identity = partner_identity(request)
    if identity and identity.get("role") in ("customer_owner", "customer_viewer") and identity.get("customer_id"):
        return identity
    return None


def _owner(request: Request) -> dict:
    identity = _identity(request)
    if not identity or identity.get("role") != "customer_owner":
        raise HTTPException(status_code=403, detail="Only the account owner can install software updates.")
    return identity


def _error_code(error: str) -> str:
    code = (error or "").split(":", 1)[0].strip()
    return code if re.fullmatch(r"[a-z_]{1,40}", code) else ("error" if error else "")


def _in_flight(db, appliance_id: str) -> dict | None:
    row = db.execute(
        "SELECT update_id,state,to_version,reported_at FROM appliance_update_results WHERE appliance_id=? "
        "ORDER BY reported_at DESC LIMIT 20", (appliance_id,)).fetchall()
    for item in row:
        if item["state"] not in FINAL_STATES:
            return dict(item)
    for command in db.execute(
            "SELECT id,payload_json,created_at FROM appliance_commands WHERE appliance_id=? AND command='install_update' "
            "AND status IN ('pending','delivered') AND expires_at>=?", (appliance_id, datetime.now().isoformat())).fetchall():
        payload = json.loads(command["payload_json"] or "{}")
        # A command whose update already reached a final outcome is history,
        # even if the appliance never acknowledged the command itself.
        final = db.execute("SELECT state FROM appliance_update_results WHERE update_id=? AND appliance_id=?",
                           (payload.get("update_id"), appliance_id)).fetchone()
        if final and final["state"] in FINAL_STATES:
            continue
        return {"update_id": payload.get("update_id"), "state": "requested", "to_version": payload.get("version"),
                "reported_at": command["created_at"]}
    return None


def software_status(db, customer_id: str) -> dict:
    release = published_release()
    appliances = []
    for row in db.execute("SELECT id,cloud_id,software_version,last_check_in,online_status FROM appliances "
                          "WHERE customer_id=? ORDER BY created_at", (customer_id,)).fetchall():
        version, build = split_release_label(row["software_version"] or "")
        history = [
            {"update_id": item["update_id"], "from_version": item["from_version"], "to_version": item["to_version"],
             "state": item["state"], "label": FINAL_LABELS.get(item["state"]) or IN_PROGRESS_LABELS.get(item["state"], item["state"]),
             "reason": _error_code(item["error"] or ""), "reported_at": item["reported_at"]}
            for item in db.execute("SELECT * FROM appliance_update_results WHERE appliance_id=? ORDER BY reported_at DESC LIMIT 10",
                                   (row["id"],)).fetchall()
        ]
        progress = _in_flight(db, row["id"])
        if progress:
            progress["label"] = IN_PROGRESS_LABELS.get(progress["state"], "Updating")
        appliances.append({
            "id": row["id"], "name": row["cloud_id"], "current_version": version, "current_build": build,
            "last_check_in": row["last_check_in"], "online": (row["online_status"] or "") == "online",
            "update_available": bool(release and is_newer(release["version"], version)),
            "in_progress": progress, "history": history,
        })
    return {"release": release, "appliances": appliances}


def request_install(db, *, identity: dict, appliance_id: str, update_id: str, version: str, confirmed: bool) -> dict:
    """Owner-confirmed install of the currently published release on one of
    the owner's appliances. The appliance row is write-locked first (an
    UPDATE: SQLite's single writer, a Postgres row lock), so a second
    concurrent request sees the first one and gets 'already in progress'."""
    if confirmed is not True:
        raise HTTPException(status_code=400, detail="Confirm the installation first.")
    locked = db.execute("UPDATE appliances SET state=state WHERE id=? AND customer_id=?",
                        (appliance_id, identity["customer_id"])).rowcount
    if locked != 1:
        raise HTTPException(status_code=404, detail="Appliance not found.")
    release = published_release()
    if not release or release["update_id"] != update_id or release["version"] != version:
        raise HTTPException(status_code=409, detail="That release is no longer the published one. Refresh and try again.")
    row = db.execute("SELECT software_version FROM appliances WHERE id=?", (appliance_id,)).fetchone()
    current_version, _build = split_release_label(row["software_version"] or "")
    if not is_newer(release["version"], current_version):
        raise HTTPException(status_code=409, detail="This appliance already runs this release or a newer one.")
    if _in_flight(db, appliance_id):
        raise HTTPException(status_code=409, detail="An update is already in progress on this appliance.")
    existing = db.execute("SELECT state FROM appliance_update_results WHERE update_id=? AND appliance_id=?",
                          (update_id, appliance_id)).fetchone()
    if existing:
        raise HTTPException(status_code=409, detail="This release was already attempted on this appliance; "
                                                    "a new release is needed before trying again.")
    now = datetime.now()
    command_id = secrets.token_hex(16)
    payload = {"update_id": update_id, "version": version, "sha256": release["sha256"], "confirmed": True,
               "requested_by": identity.get("email", "")}
    db.execute("INSERT INTO appliance_commands(id,appliance_id,command,payload_json,status,created_at,expires_at,created_by) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (command_id, appliance_id, "install_update", json.dumps(payload), "pending", now.isoformat(),
                (now + timedelta(hours=COMMAND_TTL_HOURS)).isoformat(), identity.get("email", "")))
    db.execute("INSERT INTO appliance_update_results(update_id,appliance_id,from_version,to_version,state,error,"
               "rollback_from,duration_seconds,reported_at) VALUES(?,?,?,?,?,?,?,?,?)",
               (update_id, appliance_id, current_version or None, version, "requested", "", None, None, now.isoformat()))
    return {"command_id": command_id, "state": "requested"}


# ------------------------------------------------------------------ page

_SYSTEM_SCRIPT = r'''<script>
(()=>{
const $=id=>document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const when=v=>v?new Date(v).toLocaleString([], {month:'short',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit'}):'';
const OWNER=document.body.dataset.owner==='1';
let data=null,timer=null,pending=null;
const say=(t,ok=true)=>{const m=$('su-message');m.textContent=t;m.style.color=ok?'':'#ffb4c0'};
async function call(url,method='GET',body){const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const b=await r.json().catch(()=>({}));if(!r.ok)throw new Error(b.detail||'Something went wrong.');return b}
function render(){
 const rel=data.release;
 $('su-release').innerHTML=rel?`<strong>AnyAiCam ${esc(rel.version)}</strong> <span class="health-detail">published ${esc(when(rel.issued_at))}</span>`:'<span class="health-detail">No update is published right now.</span>';
 $('su-appliances').innerHTML=data.appliances.map(a=>{
  const cur=a.current_version?`AnyAiCam ${esc(a.current_version)}`:'Version not reported yet';
  let action='';
  if(a.in_progress){action=`<span class="pill">${esc(a.in_progress.label)}</span>`}
  else if(a.update_available&&OWNER){action=`<button class="action-button" data-install="${esc(a.id)}">Install ${esc(rel.version)}</button>`}
  else if(a.update_available){action='<span class="health-detail">Update available -- only the account owner can install it.</span>'}
  else if(rel){action='<span class="pill">Up to date</span>'}
  const hist=a.history.length?'<details style="margin-top:8px"><summary>Update history</summary>'+a.history.map(h=>`<div class="health-detail">${esc(when(h.reported_at))} · ${esc(h.from_version||'?')} → ${esc(h.to_version||'?')} · ${esc(h.label)}${h.reason?` (${esc(h.reason)})`:''}</div>`).join('')+'</details>':'';
  return `<div class="health-row" style="gap:12px;flex-wrap:wrap;align-items:flex-start"><span><strong>${esc(a.name)}</strong><br><span class="health-detail">${cur}${a.online?'':' · offline'}</span>${hist}</span><span>${action}</span></div>`}).join('')||'No appliances on this account.';
 const busy=data.appliances.some(a=>a.in_progress);clearTimeout(timer);if(busy)timer=setTimeout(load,5000);
}
async function load(){try{data=await call('/api/customer/software-update');render()}catch(e){say(e.message,false)}}
document.addEventListener('click',e=>{const b=e.target.closest('button[data-install]');if(!b||!data)return;pending=data.appliances.find(a=>a.id===b.dataset.install);$('su-confirm-version').textContent=data.release.version;$('su-confirm-name').textContent=pending.name;$('su-confirm').showModal()});
$('su-cancel').onclick=()=>$('su-confirm').close();
$('su-go').onclick=async()=>{const a=pending;$('su-confirm').close();if(!a)return;try{await call('/api/customer/software-update/install','POST',{appliance_id:a.id,update_id:data.release.update_id,version:data.release.version,confirm:true});say('Installation requested. The appliance will download, verify and install it, and restore the previous version automatically if the new one does not start correctly.');load()}catch(err){say(err.message,false);load()}};
load();
})();
</script>'''


def _page(identity: dict) -> str:
    owner = identity.get("role") == "customer_owner"
    note = ("" if owner else '<p class="health-detail">Only the account owner can install updates.</p>')
    return f'''<header class="topbar"><div><p class="eyebrow">Settings</p><h1>System</h1></div>
<a class="ghost-button" href="/settings">All settings</a></header>
<p id="su-message" class="health-detail" role="status" aria-live="polite" style="font-weight:600"></p>
<section class="panel"><div class="panel-head"><h2>Software update</h2></div>
<div id="su-release" class="health-detail">Loading…</div>{note}
<div id="su-appliances" class="health-detail" style="margin-top:12px">Loading…</div></section>
<dialog id="su-confirm" style="width:min(520px,calc(100vw - 32px));border:0;border-radius:14px;padding:20px;background:#18213a;color:#eef2f6">
<h2 style="margin-top:0">Install AnyAiCam <span id="su-confirm-version"></span>?</h2>
<p>On <strong id="su-confirm-name"></strong>, recording, live view and the other VMS services may pause while the new version is prepared and started.</p>
<p class="health-detail">Your recordings, settings and accounts are kept. If the new version does not start correctly, the previous version is restored automatically.</p>
<div style="display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap"><button class="ghost-button" type="button" id="su-cancel">Cancel</button>
<button class="action-button" type="button" id="su-go">Install now</button></div></dialog>'''


def register_software_update_routes(app: FastAPI, shell: Callable) -> None:
    @app.get("/settings/system", response_class=HTMLResponse)
    def system_settings_page(request: Request) -> str:
        identity = _identity(request)
        if not identity:
            content = ('<header class="topbar"><div><p class="eyebrow">Settings</p><h1>System</h1></div>'
                       '<a class="ghost-button" href="/settings">All settings</a></header>'
                       '<section class="panel"><div class="panel-head"><h2>Customer Portal sign-in required</h2></div>'
                       '<div class="empty">Software updates belong to one customer account. Sign in to the Customer Portal '
                       'to see them.</div><a class="action-button" href="/customer-login.html" '
                       'style="margin-top:12px;display:inline-block">Sign in to Customer Portal</a></section>')
            return shell("System", "settings", content)
        marker = '1' if identity.get("role") == "customer_owner" else '0'
        script = _SYSTEM_SCRIPT.replace("document.body.dataset.owner==='1'", "'" + marker + "'==='1'")
        return shell("System", "settings", _page(identity), script)

    @app.get("/api/customer/software-update")
    def software_update_status(request: Request) -> dict:
        identity = _identity(request)
        if not identity:
            raise HTTPException(status_code=403, detail="Sign in to the Customer Portal.")
        with connection() as db:
            return software_status(db, identity["customer_id"])

    @app.post("/api/customer/software-update/install")
    def software_update_install(request: Request, payload: dict) -> dict:
        identity = _owner(request)
        appliance_id = str(payload.get("appliance_id", ""))[:80]
        update_id = str(payload.get("update_id", ""))[:80]
        version = str(payload.get("version", ""))[:20]
        with connection() as db:
            result = request_install(db, identity=identity, appliance_id=appliance_id, update_id=update_id,
                                     version=version, confirmed=payload.get("confirm") is True)
        audit(identity, "software_update.install_requested", "appliance", appliance_id,
              {"update_id": update_id, "version": version})
        return {"message": "Installation requested.", **result}
