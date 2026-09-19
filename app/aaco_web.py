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


_AACO_CLIENT_CORE_JS = """
window.aacoSubmitCommand=function(commandText,context,onSuccess,onError){
return fetch('/api/aaco/command',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:commandText,context:context||null})})
.then(function(response){return response.json().then(function(body){return {ok:response.ok,status:response.status,body:body}}).catch(function(){return {ok:false,status:response.status,body:{detail:'AACO returned an unreadable response.'}}})})
.then(function(result){if(result.ok){onSuccess(result.body)}else{onError(result.body||{detail:'AACO could not complete that command.'})}})
.catch(function(){onError({detail:'AACO could not reach the authorized VMS service.'})})
};
"""
# The one place any page ever calls POST /api/aaco/command from the
# browser. Both the standalone /aaco workspace (_workspace() below)
# and the embedded Live View panel (live_view_page.render_aaco_
# command_panel(), imported from this module) call this exact same
# function -- AACO's own parsing/execution/authorization stays
# entirely server-side either way (this function only ever sends the
# raw command text; it neither knows nor needs to know how a command
# gets interpreted), and "do not duplicate AACO logic in the browser"
# is true by construction: there is only one browser-side
# implementation of "how to ask AACO something" in this whole
# application, not one per embedding page.


def render_aaco_command_panel(*, id_prefix: str, on_result_js_fn: str, placeholder: str = "What would you like to see?", intro: str = "") -> str:
    """A compact, embeddable AACO command box: one text input, one
    submit button, one status line, and a tap-to-record microphone
    control -- everything a page needs to let a customer speak or type
    an AACO command without writing its own fetch/CSRF/error-handling
    plumbing (that part lives once, in _AACO_CLIENT_CORE_JS above). The
    embedding page supplies only on_result_js_fn: the name of a JS
    function IT defines, called with the parsed response body on
    success (this module never assumes what a page should do with a
    "live"/"playback"/"events"/"door_unlock"/"status" result -- that is
    legitimately page-specific presentation, not AACO logic, which is
    why it stays out of this shared helper). id_prefix keeps this
    embeddable more than once per page (or alongside the standalone
    workspace's own element ids) without a collision.

    Voice input, exactly as narrow as the requirement asks: the
    browser's own SpeechRecognition API (feature-detected; the button
    stays disabled with an explanatory tooltip when unsupported)
    converts speech to text entirely client-side -- this file never
    receives audio, never opens a WebSocket, never calls any speech
    API of its own, local or external. The ONLY thing that ever
    reaches this page's own code is the recognized text string, which
    is written into the exact same <input> a typed command uses and
    submitted through the exact same form 'submit' handler below via
    form.requestSubmit() -- there is no second code path to AACO, and
    no way for recognized speech to reach /api/aaco/command by any
    route other than the one a typed command already uses. A denied
    microphone permission, no speech detected, or the browser lacking
    SpeechRecognition at all are all handled explicitly and always
    leave typing fully available -- voice is additive, never a
    replacement path.

    2026-09-19: a temporary in-page diagnostics block lived here while
    a real staging browser report ("typed works, voice doesn't") was
    investigated. Real result: window.SpeechRecognition was present,
    the mic button enabled, recognition genuinely started (onstart
    fired, meaning permission was already granted -- Chrome never
    reaches onstart otherwise) -- and every attempt ended in a
    no-speech error with no transcript. That rules out Permissions-
    Policy, a broken build, and unsupported-browser; it points at the
    recognition session simply not hearing usable audio in whatever
    window it allotted before giving up -- a real, well-known Web
    Speech API characteristic (no-speech fires on its own internal
    silence timeout, which the API gives no way for page script to
    lengthen) compounded by the reaction-time gap between clicking and
    actually speaking, and it is exactly as likely to be a Windows/
    browser microphone *input selection* issue (wrong default
    recording device, muted/very quiet input) as an app defect --
    onstart already proved the browser itself has a working
    permission grant. Two small, real fixes applied given that: (1)
    interimResults=true, so partial words appear in the status line
    the moment the recognizer hears anything -- the fastest way for a
    customer (or an operator debugging this) to tell "the mic is
    picking up audio, it just hasn't finished a phrase yet" apart from
    "nothing is reaching the recognizer at all"; (2) exactly one
    silent, automatic retry on a first no-speech error before
    reporting failure, directly covering the "ended too quickly before
    I started talking" case this investigation was asked to check.
    Neither fix can distinguish an environment-level microphone
    selection problem from an occasional real timeout -- if no-speech
    still recurs after both, the final message says so plainly and
    points at system microphone settings rather than guessing further
    in the browser layer. The diagnostics block itself is removed
    (its whole purpose was reaching this conclusion) -- see
    docs/aaco-voice-no-speech-investigation.md for the full writeup."""
    input_id, mic_id, status_id, form_id = (
        f"{id_prefix}-command", f"{id_prefix}-mic", f"{id_prefix}-status", f"{id_prefix}-form",
    )
    return f"""
<style>
.aaco-mic-listening{{background:#c0392b !important;color:#fff !important;animation:aaco-mic-pulse 1.1s ease-in-out infinite}}
@keyframes aaco-mic-pulse{{0%,100%{{opacity:1}}50%{{opacity:.55}}}}
</style>
<div class="aaco-embed-panel" data-aaco-embed="{id_prefix}">
{f'<p class="aaco-muted">{intro}</p>' if intro else ''}
<form id="{form_id}" class="aaco-command-row" style="display:flex;gap:8px;align-items:center">
<input id="{input_id}" name="command" maxlength="500" autocomplete="off" required placeholder="{placeholder}" style="min-width:0;flex:1">
<button type="button" class="camera-tool aaco-mic-button" id="{mic_id}" title="Voice commands are not supported in this browser" aria-label="Voice commands are not supported in this browser" aria-pressed="false" disabled>🎤</button>
<button class="action-button" type="submit">Ask AACO</button>
</form>
<p id="{status_id}" class="aaco-muted" role="status" aria-live="polite" style="margin-top:6px"></p>
</div>
<script>{_AACO_CLIENT_CORE_JS}
(function(){{
var form=document.getElementById({form_id!r}),input=document.getElementById({input_id!r}),status=document.getElementById({status_id!r}),micButton=document.getElementById({mic_id!r});
if(!form)return;
form.addEventListener('submit',function(event){{
event.preventDefault();
var text=input.value.trim();
if(!text)return;
status.textContent='Working with your authorized VMS…';
window.aacoSubmitCommand(text,null,function(body){{
status.textContent=body.message||'';
if(typeof window[{on_result_js_fn!r}]==='function'){{window[{on_result_js_fn!r}](body)}}
input.value='';
input.focus();
}},function(error){{
status.textContent=error.detail||'AACO could not complete that command.';
}});
}});
// Voice input: tap to start listening, tap again (or a final
// result) to stop. Recognized speech is never sent to AACO directly
// by this code -- it is written into the same <input> a typed
// command uses, then submitted through the exact same submit
// listener above via form.requestSubmit(), never a parallel call to
// window.aacoSubmitCommand of its own.
var SpeechRecognitionCtor=window.SpeechRecognition||window.webkitSpeechRecognition;
if(micButton&&SpeechRecognitionCtor){{
micButton.disabled=false;
micButton.title='Press to speak a command';
micButton.setAttribute('aria-label','Press to speak a command');
var recognition=null,listening=false;
function stopListening(){{
listening=false;
micButton.classList.remove('aaco-mic-listening');
micButton.setAttribute('aria-pressed','false');
if(recognition){{try{{recognition.stop()}}catch(e){{}}}}
}}
// isRetry=true marks the one automatic re-attempt after a first
// no-speech error -- see the function docstring above for why this
// exists. A retry's own onend must NOT drop the visual "listening"
// state (the retrying flag suppresses that single onend), otherwise
// the mic would flicker to idle for an instant between the two
// attempts even though a fresh recognition session starts
// immediately after.
function attemptRecognition(isRetry){{
var retrying=false;
try{{recognition=new SpeechRecognitionCtor()}}catch(e){{stopListening();status.textContent='Voice input is unavailable right now. Type your command instead.';return}}
recognition.lang=navigator.language||'en-US';
recognition.interimResults=true;
recognition.maxAlternatives=1;
recognition.onresult=function(event){{
var result=event.results&&event.results[event.results.length-1];
var alt=result&&result[0];
var transcript=alt&&alt.transcript;
if(!transcript)return;
if(!result.isFinal){{status.textContent='Hearing: “'+transcript+'”…';return}}
stopListening();
input.value=transcript;
status.textContent='Heard: “'+transcript+'” — sending to AACO…';
if(typeof form.requestSubmit==='function'){{form.requestSubmit()}}else{{form.dispatchEvent(new Event('submit',{{cancelable:true}}))}}
}};
recognition.onerror=function(event){{
if(event.error==='no-speech'&&!isRetry){{
retrying=true;
status.textContent='Still listening — go ahead and speak your command.';
attemptRecognition(true);
return;
}}
stopListening();
if(event.error==='not-allowed'||event.error==='permission-denied'){{status.textContent='Microphone permission was denied. Type your command instead.'}}
else if(event.error==='no-speech'){{status.textContent='No speech detected. Check that the correct microphone is selected and unmuted in your system sound settings, then try again or type your command.'}}
else{{status.textContent='Voice input is unavailable right now. Type your command instead.'}}
}};
recognition.onend=function(){{if(!retrying)stopListening()}};
try{{recognition.start()}}catch(e){{stopListening();status.textContent='Voice input could not start. Type your command instead.'}}
}}
micButton.addEventListener('click',function(){{
if(listening){{stopListening();return}}
listening=true;
micButton.classList.add('aaco-mic-listening');
micButton.setAttribute('aria-pressed','true');
status.textContent='Listening…';
attemptRecognition(false);
}});
}}
}})();
</script>
"""


def _workspace() -> str:
    # Page open deliberately makes zero VMS/media requests. The first fetch is
    # inside submit(), so the request-driven loading rule is observable.
    # %%AACO_CORE_JS%% is substituted below (not an f-string): this
    # markup's own CSS is full of literal {}, which would otherwise
    # need escaping throughout just to insert one shared JS constant.
    return """
<header class="topbar"><div><p class="eyebrow">AnyAiCam Operator</p><h1>AACO</h1></div><a class="ghost-button" href="/customer-account">Classic workspace</a></header>
<style>
.aaco-layout{display:grid;grid-template-columns:minmax(240px,.72fr) minmax(0,2fr);gap:16px}.aaco-panel{border:1px solid rgba(170,196,207,.18);border-radius:14px;background:rgba(24,33,50,.92);padding:18px}.aaco-panel h2{margin:0 0 6px}.aaco-muted{color:var(--muted);font-size:12px;line-height:1.5}.aaco-examples{display:grid;gap:8px;margin-top:14px}.aaco-example{width:100%;text-align:left;padding:10px;border:1px solid var(--line);border-radius:9px;background:#111827;color:#dce7ee;font:inherit;font-size:12px;cursor:pointer}.aaco-example:hover{border-color:var(--brand)}.aaco-command-row{display:flex;gap:8px}.aaco-command-row input{min-width:0;flex:1}.aaco-conversation{display:grid;gap:12px;min-height:350px}.aaco-turn{max-width:min(92%,700px);padding:12px 14px;border-radius:12px;line-height:1.45}.aaco-turn.customer{justify-self:end;background:#285d5a}.aaco-turn.operator{background:#111827;border:1px solid rgba(170,196,207,.15)}.aaco-turn .eyebrow{margin:0 0 5px;font-size:10px}.aaco-result-list{display:grid;gap:8px;margin-top:10px}.aaco-result-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px;border:1px solid rgba(170,196,207,.15);border-radius:9px}.aaco-result-row span{color:var(--muted);font-size:11px}.aaco-context{margin-top:12px;padding:9px 11px;border-left:3px solid var(--brand);color:#b9cfda;font-size:11px}.aaco-loading{opacity:.7}@media(max-width:760px){.aaco-layout{grid-template-columns:1fr}.aaco-command-row{flex-direction:column}.aaco-command-row button{width:100%}.aaco-conversation{min-height:260px}}
</style>
<main class="aaco-layout" aria-label="AACO operator workspace">
 <aside class="aaco-panel"><p class="eyebrow">On demand</p><h2>Ask AACO</h2><p class="aaco-muted">AACO loads VMS data only after a command. Search shows metadata; it never creates a clip.</p><div class="aaco-examples" aria-label="Example commands"><button class="aaco-example" type="button">Show Camera 1</button><button class="aaco-example" type="button">Show the front entrance</button><button class="aaco-example" type="button">Show Camera 2 from 3:15 yesterday</button><button class="aaco-example" type="button">Show person events from the last 2 hours</button><button class="aaco-example" type="button">Which cameras are offline?</button><button class="aaco-example" type="button">Go back 20 minutes</button><button class="aaco-example" type="button">Show previous event</button><button class="aaco-example" type="button">Return to live</button></div></aside>
 <section class="aaco-panel"><form id="aaco-command-form"><label class="eyebrow" for="aaco-command">Command</label><div class="aaco-command-row"><input id="aaco-command" name="command" maxlength="500" autocomplete="off" required placeholder="What would you like to see?"><button class="action-button">Run command</button></div></form><p id="aaco-status" class="aaco-muted" role="status" aria-live="polite">Ready. No historical media is loaded until you ask.</p><div id="aaco-conversation" class="aaco-conversation" aria-live="polite"><div class="aaco-turn operator"><p class="eyebrow">AACO</p>What would you like to see?</div></div><div id="aaco-context" class="aaco-context" hidden></div></section>
</main>
<script>%%AACO_CORE_JS%%
(()=>{const form=document.getElementById('aaco-command-form'),input=document.getElementById('aaco-command'),status=document.getElementById('aaco-status'),conversation=document.getElementById('aaco-conversation'),contextLine=document.getElementById('aaco-context');let operatorContext=null;const node=(tag,value)=>{const el=document.createElement(tag);el.append(document.createTextNode(String(value??'')));return el};const turn=(who)=>{const el=document.createElement('div');el.className='aaco-turn '+who;const label=node('p',who==='customer'?'You':'AACO');label.className='eyebrow';el.append(label);conversation.append(el);return el};const link=(label,href)=>{const a=node('a',label);a.className='download';a.href=href;return a};function updateContext(value){operatorContext=value||null;if(!operatorContext){contextLine.hidden=true;return}contextLine.hidden=false;contextLine.textContent='Current context: '+operatorContext.camera_id+(operatorContext.playback_at?' · playback selected':'')+(operatorContext.event_at?' · event selected':'')}
const KIND_LABELS={live:'Live view',playback:'Playback',events:'Requested events',door_unlock:'Door',status:'Camera status'};
function render(body){const box=turn('operator');if(body.kind==='clarification'){box.append(node('div',body.message));return}box.append(node('strong',KIND_LABELS[body.kind]||'Camera status'));box.append(node('p',body.message));if(body.kind==='live'||body.kind==='playback'){box.append(link(body.kind==='live'?'Open authorized Live':'Open authorized Playback',body.href));updateContext(body.context||operatorContext);return}if(body.kind==='door_unlock'){if(body.context)updateContext(body.context);return}const list=document.createElement('div');list.className='aaco-result-list';const rows=body.kind==='events'?body.events||[]:body.cameras||[];if(!rows.length)box.append(node('p',body.kind==='events'?'No authorized events matched this request.':'No authorized cameras are available.'));rows.forEach(row=>{const item=document.createElement('div');item.className='aaco-result-row';const copy=document.createElement('div');copy.append(node('strong',row.label));copy.append(node('span',row.timestamp||row.state||''));item.append(copy);if(row.href)item.append(link('Open in Classic',row.href));if(row.context){const pick=node('button','Use context');pick.type='button';pick.className='ghost-button';pick.addEventListener('click',()=>updateContext(row.context));item.append(pick)}list.append(item)});box.append(list);if(body.context)updateContext(body.context)}
document.querySelectorAll('.aaco-example').forEach(button=>button.addEventListener('click',()=>{input.value=button.textContent.trim();input.focus()}));form.addEventListener('submit',event=>{event.preventDefault();const command=input.value.trim();if(!command)return;turn('customer').append(node('div',command));status.textContent='Working with your authorized VMS…';conversation.classList.add('aaco-loading');window.aacoSubmitCommand(command,operatorContext,body=>{status.textContent=body.message||'Completed.';render(body);conversation.classList.remove('aaco-loading');conversation.lastElementChild?.scrollIntoView({block:'nearest'});input.focus()},error=>{const box=turn('operator');box.append(node('div',error.detail||'AACO could not complete that command.'));status.textContent='Command was not completed.';conversation.classList.remove('aaco-loading');conversation.lastElementChild?.scrollIntoView({block:'nearest'});input.focus()})});
</script>
""".replace("%%AACO_CORE_JS%%", _AACO_CLIENT_CORE_JS)


def register_aaco_routes(app: FastAPI, page_shell: Callable[..., str], *, identity_provider: Callable[[Request], dict | None], vms_factory: Callable[[Request], object], now: Callable[[], datetime] = datetime.now, language_adapter_factory: Callable[[], object] = DeterministicLanguageAdapter) -> None:
    """Register an isolated UI that crosses only the injected VMS boundary.

    language_adapter_factory defaults to the plain regex grammar,
    unchanged -- passing app.aaco_llm.NaturalAacoLanguageAdapter here
    (main.py's own call site decides this, based on
    ANYAICAM_AACO_LOCAL_LLM_ENABLED) is strictly additive: that class
    itself always falls back to a DeterministicLanguageAdapter of its
    own whenever local inference is disabled/unavailable/invalid, so
    every existing fixed-phrase command keeps parsing identically
    either way. Constructed once per app, matching how vms_factory is
    a *callable that builds one per request* while this is a *callable
    that builds the (possibly model-loading) adapter once* -- an LLM
    interpreter should load its model once, not on every command."""
    log = logging.getLogger("anyaicam.aaco")
    language_adapter = language_adapter_factory()

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
        parsed = language_adapter.parse(command_text, now=now(), context=_context(payload.get("context")))
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
