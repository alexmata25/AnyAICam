"""On-demand AACO customer workspace; no storage, relay, or media implementation."""
from __future__ import annotations

from datetime import datetime
import logging
import re
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from aaco import Clarification, DeterministicLanguageAdapter, execute

CUSTOMER_ROLES = {"customer_owner", "customer_viewer"}
MAX_COMMAND_LENGTH = 500
_CONTEXT_CAMERA = re.compile(r"(?:camera-\d+|camera-name:[a-z0-9 &'_-]{1,81})$")


def _require_customer(identity: dict | None) -> dict:
    if not identity or identity.get("role") not in CUSTOMER_ROLES:
        raise HTTPException(status_code=403, detail="Customer authentication required.")
    return identity


def _context(payload: object) -> dict:
    """Parse only AACO's narrow convenience state; it is never authority."""
    if not isinstance(payload, dict):
        return {}
    camera_id = payload.get("camera_id")
    if not isinstance(camera_id, str) or not _CONTEXT_CAMERA.fullmatch(camera_id):
        return {}
    parsed = {"camera_id": camera_id}
    for field in ("playback_at", "event_at"):
        value = payload.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            return {}
        try:
            parsed[field] = datetime.fromisoformat(value)
        except ValueError:
            return {}
    return parsed


def _result_payload(result: object) -> dict[str, Any]:
    if not isinstance(result, dict) or result.get("kind") not in {"live", "playback", "events", "status", "door_unlock"}:
        raise HTTPException(status_code=503, detail="AACO capability is unavailable.")
    return result


def _workspace() -> str:
    # Page open deliberately makes zero VMS/media requests. The first fetch is
    # inside submit(), so the request-driven loading rule is observable.
    return """
<header class="topbar"><div><p class="eyebrow">AnyAiCam Operator</p><h1>AACO</h1></div><a class="ghost-button" href="/customer-account">Classic workspace</a></header>
<style>
.aaco-layout{display:grid;grid-template-columns:minmax(240px,.72fr) minmax(0,2fr);gap:16px}.aaco-panel{border:1px solid rgba(170,196,207,.18);border-radius:14px;background:rgba(24,33,50,.92);padding:18px}.aaco-panel h2{margin:0 0 6px}.aaco-muted{color:var(--muted);font-size:12px;line-height:1.5}.aaco-examples{display:grid;gap:8px;margin-top:14px}.aaco-example{width:100%;text-align:left;padding:10px;border:1px solid var(--line);border-radius:9px;background:#111827;color:#dce7ee;font:inherit;font-size:12px;cursor:pointer}.aaco-example:hover{border-color:var(--brand)}.aaco-command-row{display:flex;gap:8px}.aaco-command-row input{min-width:0;flex:1}.aaco-conversation{display:grid;gap:12px;min-height:350px}.aaco-turn{max-width:min(92%,700px);padding:12px 14px;border-radius:12px;line-height:1.45}.aaco-turn.customer{justify-self:end;background:#285d5a}.aaco-turn.operator{background:#111827;border:1px solid rgba(170,196,207,.15)}.aaco-turn .eyebrow{margin:0 0 5px;font-size:10px}.aaco-result-list{display:grid;gap:8px;margin-top:10px}.aaco-result-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px;border:1px solid rgba(170,196,207,.15);border-radius:9px}.aaco-result-row span{color:var(--muted);font-size:11px}.aaco-context{margin-top:12px;padding:9px 11px;border-left:3px solid var(--brand);color:#b9cfda;font-size:11px}.aaco-loading{opacity:.7}@media(max-width:760px){.aaco-layout{grid-template-columns:1fr}.aaco-command-row{flex-direction:column}.aaco-command-row button{width:100%}.aaco-conversation{min-height:260px}}
</style>
<main class="aaco-layout" aria-label="AACO operator workspace">
 <aside class="aaco-panel"><p class="eyebrow">On demand</p><h2>Ask AACO</h2><p class="aaco-muted">AACO loads VMS data only after a command. Search shows metadata; it never creates a clip.</p><div class="aaco-examples" aria-label="Example commands"><button class="aaco-example" type="button">Show Camera 1</button><button class="aaco-example" type="button">Show the front entrance</button><button class="aaco-example" type="button">Show Camera 2 from 3:15 yesterday</button><button class="aaco-example" type="button">Show person events from the last 2 hours</button><button class="aaco-example" type="button">Which cameras are offline?</button><button class="aaco-example" type="button">Go back 20 minutes</button><button class="aaco-example" type="button">Show previous event</button><button class="aaco-example" type="button">Return to live</button></div></aside>
 <section class="aaco-panel"><form id="aaco-command-form"><label class="eyebrow" for="aaco-command">Command</label><div class="aaco-command-row"><input id="aaco-command" name="command" maxlength="500" autocomplete="off" required placeholder="What would you like to see?"><button class="action-button">Run command</button></div></form><p id="aaco-status" class="aaco-muted" role="status" aria-live="polite">Ready. No historical media is loaded until you ask.</p><div id="aaco-conversation" class="aaco-conversation" aria-live="polite"><div class="aaco-turn operator"><p class="eyebrow">AACO</p>What would you like to see?</div></div><div id="aaco-context" class="aaco-context" hidden></div></section>
</main>
<script>
(()=>{const form=document.getElementById('aaco-command-form'),input=document.getElementById('aaco-command'),status=document.getElementById('aaco-status'),conversation=document.getElementById('aaco-conversation'),contextLine=document.getElementById('aaco-context');let operatorContext=null;const node=(tag,value)=>{const el=document.createElement(tag);el.append(document.createTextNode(String(value??'')));return el};const turn=(who)=>{const el=document.createElement('div');el.className='aaco-turn '+who;const label=node('p',who==='customer'?'You':'AACO');label.className='eyebrow';el.append(label);conversation.append(el);return el};const link=(label,href)=>{const a=node('a',label);a.className='download';a.href=href;return a};function updateContext(value){operatorContext=value||null;if(!operatorContext){contextLine.hidden=true;return}contextLine.hidden=false;contextLine.textContent='Current context: '+operatorContext.camera_id+(operatorContext.playback_at?' · playback selected':'')+(operatorContext.event_at?' · event selected':'')}
function render(body){const box=turn('operator');if(body.kind==='clarification'){box.append(node('div',body.message));return}box.append(node('strong',body.kind==='live'?'Live view':body.kind==='playback'?'Playback':body.kind==='events'?'Requested events':'Camera status'));box.append(node('p',body.message));if(body.kind==='live'||body.kind==='playback'){box.append(link(body.kind==='live'?'Open authorized Live':'Open authorized Playback',body.href));updateContext(body.context||operatorContext);return}const list=document.createElement('div');list.className='aaco-result-list';const rows=body.kind==='events'?body.events||[]:body.cameras||[];if(!rows.length)box.append(node('p',body.kind==='events'?'No authorized events matched this request.':'No authorized cameras are available.'));rows.forEach(row=>{const item=document.createElement('div');item.className='aaco-result-row';const copy=document.createElement('div');copy.append(node('strong',row.label));copy.append(node('span',row.timestamp||row.state||''));item.append(copy);if(row.href)item.append(link('Open in Classic',row.href));if(row.context){const pick=node('button','Use context');pick.type='button';pick.className='ghost-button';pick.addEventListener('click',()=>updateContext(row.context));item.append(pick)}list.append(item)});box.append(list);if(body.context)updateContext(body.context)}
document.querySelectorAll('.aaco-example').forEach(button=>button.addEventListener('click',()=>{input.value=button.textContent.trim();input.focus()}));form.addEventListener('submit',async event=>{event.preventDefault();const command=input.value.trim();if(!command)return;turn('customer').append(node('div',command));status.textContent='Working with your authorized VMS…';conversation.classList.add('aaco-loading');try{const response=await fetch('/api/aaco/command',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({command,context:operatorContext})});const body=await response.json();if(!response.ok){const box=turn('operator');box.append(node('div',body.detail||'AACO could not complete that command.'));status.textContent='Command was not completed.';return}status.textContent=body.message||'Completed.';render(body)}catch(_){const box=turn('operator');box.append(node('div','AACO could not reach the authorized VMS service.'));status.textContent='Connection problem.'}finally{conversation.classList.remove('aaco-loading');conversation.lastElementChild?.scrollIntoView({block:'nearest'});input.focus()}})})();
</script>
"""


def register_aaco_routes(app: FastAPI, page_shell: Callable[..., str], *, identity_provider: Callable[[Request], dict | None], vms_factory: Callable[[Request], object], now: Callable[[], datetime] = datetime.now) -> None:
    """Register an isolated UI that crosses only the injected VMS boundary."""
    log = logging.getLogger("anyaicam.aaco")

    @app.get("/aaco", response_class=HTMLResponse)
    def aaco_workspace(request: Request):
        _require_customer(identity_provider(request))
        log.info("aaco.workspace_opened mode=on_demand historical_autoload=false")
        return page_shell("AACO", "aaco", _workspace())

    @app.post("/api/aaco/command")
    async def aaco_command(request: Request) -> dict[str, Any]:
        identity = _require_customer(identity_provider(request))
        try:
            payload = await request.json()
        except Exception as error:
            raise HTTPException(status_code=400, detail="Command must be valid JSON.") from error
        if not isinstance(payload, dict) or set(payload) - {"command", "context"}:
            raise HTTPException(status_code=400, detail="Malformed AACO command.")
        command_text = payload.get("command")
        if not isinstance(command_text, str) or not command_text.strip() or len(command_text) > MAX_COMMAND_LENGTH:
            raise HTTPException(status_code=400, detail="A command between 1 and 500 characters is required.")
        parsed = DeterministicLanguageAdapter().parse(command_text, now=now(), context=_context(payload.get("context")))
        if isinstance(parsed, Clarification):
            log.info("aaco.command_clarification")
            return {"kind": "clarification", "message": parsed.message}
        try:
            result = execute(parsed, identity=identity, vms=vms_factory(request))
        except PermissionError as error:
            raise HTTPException(status_code=403, detail="Camera is unavailable.") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Unsupported AACO command.") from error
        # unlock_door's own boundary implementation may return a
        # Clarification too (an ambiguous door name matching more than
        # one authorized door) -- the same escape hatch parse() already
        # uses, now also available after execute() for a command that
        # was well-formed but not safely completable as a single
        # deterministic action.
        if isinstance(result, Clarification):
            log.info("aaco.command_clarification")
            return {"kind": "clarification", "message": result.message}
        log.info("aaco.command operation=%s", parsed.operation)
        return _result_payload(result)
