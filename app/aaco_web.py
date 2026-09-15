"""Small, on-demand AACO web surface.

This module intentionally has no database, storage, relay, or media-player
implementation.  It accepts text, asks the Phase-1 command engine to produce
one strict command, and then crosses an injected, already-authorized VMS
boundary.  The production binding lives in ``main.py``; tests inject a fake
boundary so route behavior can be checked without a second VMS stack.
"""
from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from aaco import Clarification, DeterministicLanguageAdapter, execute

CUSTOMER_ROLES = {"customer_owner", "customer_viewer"}
MAX_COMMAND_LENGTH = 500


def _require_customer(identity: dict | None) -> dict:
    if not identity or identity.get("role") not in CUSTOMER_ROLES:
        raise HTTPException(status_code=403, detail="Customer authentication required.")
    return identity


def _context(payload: object) -> dict:
    """Accept only the narrow playback context emitted by this workspace.

    The context is convenience state, never authorization.  Every later
    camera/media operation is independently authorized by the VMS boundary.
    """
    if not isinstance(payload, dict):
        return {}
    camera_id = payload.get("camera_id")
    playback_at = payload.get("playback_at")
    if not isinstance(camera_id, str) or not isinstance(playback_at, str):
        return {}
    try:
        parsed = datetime.fromisoformat(playback_at)
    except ValueError:
        return {}
    return {"camera_id": camera_id, "playback_at": parsed}


def _result_payload(result: object) -> dict[str, Any]:
    """Return only structured VMS presentation data, never arbitrary objects."""
    if not isinstance(result, dict):
        raise HTTPException(status_code=503, detail="AACO capability is unavailable.")
    kind = result.get("kind")
    if kind not in {"live", "playback", "events", "status"}:
        raise HTTPException(status_code=503, detail="AACO capability is unavailable.")
    return result


def _workspace() -> str:
    # There is deliberately no initial fetch.  This is the measurable
    # on-demand rule: page open renders a shell only; a submit creates one
    # bounded VMS request.  Search returns metadata and never calls clips.
    return """
<header class="topbar"><div><p class="eyebrow">AACO</p><h1>Operator workspace</h1></div>
<a class="ghost-button" href="/customer-account">Classic workspace</a></header>
<section class="panel" aria-label="AACO command workspace">
  <p class="health-detail">What would you like to see?</p>
  <form id="aaco-command-form" class="rule-form">
    <label for="aaco-command">Command</label>
    <div style="display:flex;gap:8px;flex-wrap:wrap"><input id="aaco-command" name="command" maxlength="500" autocomplete="off" required style="flex:1;min-width:260px" placeholder="Show Camera 4"><button class="action-button">Run</button></div>
  </form>
  <p id="aaco-status" class="health-detail" role="status" aria-live="polite">Ready. AACO loads VMS data only after a command.</p>
</section>
<section class="panel" style="margin-top:14px" aria-live="polite"><div id="aaco-results" class="empty">No command has been run.</div></section>
<script>
(() => {
  const form=document.getElementById('aaco-command-form'), input=document.getElementById('aaco-command');
  const status=document.getElementById('aaco-status'), results=document.getElementById('aaco-results');
  let playbackContext=null;
  const text=value => document.createTextNode(String(value ?? ''));
  const add=(parent,tag,value)=>{const node=document.createElement(tag);node.append(text(value));parent.append(node);return node};
  const clear=()=>results.replaceChildren();
  function link(label,href){const anchor=document.createElement('a');anchor.className='download';anchor.href=href;anchor.append(text(label));return anchor}
  function render(body){
    clear(); const kind=body.kind;
    if(kind==='live'||kind==='playback'){
      add(results,'h2',kind==='live'?'Live view':'Playback'); add(results,'p',body.message);
      results.append(link(kind==='live'?'Open authorized Live view':'Open authorized Playback',body.href));
      if(kind==='playback'&&body.context) playbackContext=body.context;
      return;
    }
    if(kind==='events'){
      add(results,'h2','Requested events'); add(results,'p',body.message);
      const list=document.createElement('div'); list.className='settings-list';
      (body.events||[]).forEach(event=>{const row=document.createElement('div');row.className='settings-row';add(row,'strong',event.label);add(row,'span',event.timestamp);if(event.href)row.append(link('Open in Classic',event.href));list.append(row)});
      if(!(body.events||[]).length)add(results,'p','No authorized events matched this request.'); results.append(list); return;
    }
    if(kind==='status'){
      add(results,'h2','Camera status'); const list=document.createElement('div'); list.className='settings-list';
      (body.cameras||[]).forEach(camera=>{const row=document.createElement('div');row.className='settings-row';add(row,'strong',camera.label);add(row,'span',camera.state);list.append(row)});results.append(list); return;
    }
  }
  form.addEventListener('submit',async event=>{
    event.preventDefault(); status.textContent='Working…';
    try {
      const response=await fetch('/api/aaco/command',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:input.value,context:playbackContext})});
      const body=await response.json();
      if(!response.ok){status.textContent=body.detail||'AACO could not complete that command.';return;}
      if(body.kind==='clarification'){status.textContent=body.message;clear();add(results,'p',body.message);return;}
      status.textContent=body.message||'Completed.';render(body);
    } catch (_) { status.textContent='AACO could not reach the authorized VMS service.'; }
  });
})();
</script>
"""


def register_aaco_routes(
    app: FastAPI,
    page_shell: Callable[..., str],
    *,
    identity_provider: Callable[[Request], dict | None],
    vms_factory: Callable[[Request], object],
    now: Callable[[], datetime] = datetime.now,
) -> None:
    """Register the isolated page and command endpoint.

    ``vms_factory`` is the only integration seam.  It must use existing,
    tenant-authorized VMS services; this module deliberately cannot query
    storage, create clips, start relays, or access credentials itself.
    """
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
        log.info("aaco.command operation=%s", parsed.operation)
        return _result_payload(result)
