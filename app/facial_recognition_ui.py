"""AAC (facial recognition / access-control analytics) routes -- Phase 1.

Mounted the same way every other portal feature area in this codebase
is (register_facial_recognition_routes(app, shell), called once from
main.py next to register_partner_routes/register_customer_platform_
routes), and built on the SAME primitives partner_portal.py's own
routes already use: partner_db.connection()/audit()/allowed(), and
partner_portal.require_partner_access()/partner_identity(). This is a
partner-portal-style admin tool, not a second, parallel auth system.

Tenant isolation, the one property enforced identically on every route
below: every request carries an explicit customer_id (path or query
parameter for staff roles managing a specific customer; implicit --
and REQUIRED to match the session -- for customer_owner/customer_
viewer, who can only ever act on their own tenant). _resolve_customer_id()
is the single choke point this is enforced through; no route below ever
reads a customer_id from anywhere else. Every actual data access then
goes through facial_people.py/facial_events.py, which independently
re-scope every query by that same customer_id -- see those modules'
own docstrings for why that's a deliberate belt-and-suspenders design,
not redundant.

Permissions: 'facial.manage' (enroll/edit/delete people, manage
watchlists, change settings) and 'facial.view' (read people/events/
watchlists/settings) are new ROLE_PERMISSIONS entries added in
partner_db.py -- see that file's own change for exactly which roles
carry which of the two.
"""

from __future__ import annotations

import base64
import os
import binascii
import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

import facial_events
import facial_people
import facial_recognition
from customer_policy import same_customer
from partner_db import allowed, audit, connection

# Every id this module ever builds a filesystem path out of -- customer_id
# (client-supplied for staff roles; see _resolve_customer_id()), plus our
# own server-generated person_id/embedding_id -- must pass this check
# before touching the filesystem. In practice facial_people.enroll_person()
# already refuses a customer_id that doesn't exist in `customers` (a real
# foreign-key constraint, enforced on both SQLite and PostgreSQL), which
# transitively blocks a path-traversal payload from ever reaching
# _save_face_crop() today -- but that protection is implicit and backend-
# dependent, not something this module asserts for itself. This is the
# explicit, local, defense-in-depth check: reject anything containing a
# path separator or a `..` segment before it can become part of a path,
# so a future change elsewhere (a relaxed FK, a different backend, a new
# caller) can never turn this into a path-traversal write.
_SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")


def _require_safe_path_segment(value: str, *, field: str) -> str:
    if not value or not _SAFE_PATH_SEGMENT.match(value):
        raise HTTPException(status_code=400, detail=f"Invalid {field}.")
    return value


def _resolve_customer_id(identity: dict, requested: str | None) -> str:
    """The one place a request's target customer_id is decided.
    customer_owner/customer_viewer are hard-pinned to their own
    session's customer_id -- a mismatched or missing `requested` value
    is rejected outright, never silently coerced to their own id (a
    silent coercion could mask a client bug that thought it was
    querying a different tenant). Staff roles (administrator/
    partner_owner/technician/salesperson) must supply one explicitly;
    there is no "current customer" concept for those roles here."""
    if identity["role"] in ("customer_owner", "customer_viewer"):
        own = identity.get("customer_id")
        if not own or (requested and not same_customer(requested, own)):
            raise HTTPException(status_code=403, detail="You are not authorized for that customer.")
        return _require_safe_path_segment(own, field="customer_id")
    if not requested:
        raise HTTPException(status_code=400, detail="customer_id is required.")
    # Validated here, once, for every caller (not only the enrollment-image
    # upload path that actually touches the filesystem) -- every real
    # customer_id in this codebase is a secrets.token_hex()/simple slug
    # value (see customer_registration.py), so this never rejects a
    # legitimate id; it exists purely to make a path-traversal payload
    # (e.g. "../../etc") fail fast, here, before it can reach any query
    # or, later, any filesystem path built from this value.
    customer_id = _require_safe_path_segment(requested, field="customer_id")
    # Admin portal pass (2026-09-26): staff roles were only format-checked,
    # so a partner owner/technician/salesperson could read or change ANY
    # customer's facial people and biometric enrollments by typing another
    # partner's customer_id. Same tenant rule as every other partner route:
    # a live global administrator reaches all customers, everyone else only
    # their own partner's -- and an unknown or foreign id both answer 404.
    from partner_db import authorize_customer_tenant, connection as _tenant_connection

    with _tenant_connection() as db:
        if not authorize_customer_tenant(db, identity, customer_id):
            raise HTTPException(status_code=404, detail="Customer not found.")
    return customer_id


FACE_PREVIEW_LOCAL_ONLY = "Face preview is available only on the local appliance."


def _runtime_role() -> str:
    from cloud_config import settings as _settings
    return _settings.runtime_role


def facial_allowed(identity: dict, permission: str) -> bool:
    """The one facial.* permission decision for routes and pages. A household
    member (customer_viewer) gets People / Face Access exactly as the account
    owner granted them (household_users.py) -- including facial.manage, which
    the customer_viewer role itself never carries; every other role is the
    role permission, unchanged."""
    if identity.get("role") == "customer_viewer" and permission in ("facial.view", "facial.manage", "facial.profile"):
        import household_users
        return household_users.facial_permission_allowed(identity, permission)
    if permission == "facial.profile":  # every other role: exactly facial.view, unchanged
        return allowed(identity, "facial.view")
    return allowed(identity, permission)


# What a person with only facial.profile (Backup Access alone) may see of a
# person: enough to find themselves and use Backup Access, nothing more.
PROFILE_ONLY_FIELDS = ("id", "display_name", "status")


def _profile_only(person: dict) -> dict:
    return {key: person.get(key) for key in PROFILE_ONLY_FIELDS}


def _require(request: Request, permission: str) -> dict:
    from partner_portal import partner_identity

    identity = partner_identity(request)
    if not identity:
        raise HTTPException(status_code=401, detail="Sign in is required.")
    if not facial_allowed(identity, permission):
        raise HTTPException(status_code=403, detail="You do not have permission for this action.")
    return identity


def _require_face_access(identity: dict) -> None:
    """Face Access settings and enrollment previews belong to the Face Access
    add-on (2026-10-01): a customer account without it is refused here, as
    its pages already show the add-on instead. Staff tools are unaffected."""
    context = _customer_facial_context(identity)
    if context is not None and not context["entitled"]:
        raise HTTPException(status_code=403, detail="Face Access is not active on this account.")


def _customer_facial_context(identity: dict) -> dict | None:
    """2026-09-23 fix: every /aac/* page below rendered the same raw,
    role-unaware admin-tool UI -- a manual "Customer ID" text field the
    visitor had to type into before anything would load. Confirmed live:
    _resolve_customer_id() (above) already hard-pins a customer_owner/
    customer_viewer session's requests to their OWN customer_id and
    rejects any other value outright -- but a real customer has no way
    to ever know that internal id (it's never shown anywhere in the
    customer-facing UI), so this field permanently blocked the entire
    feature for every real customer session, even one that had paid for
    it.

    Returns None for staff roles (administrator/partner_owner/
    technician/salesperson) -- their manual, multi-customer tool is
    correct and deliberate, unchanged by this fix. For a real
    customer_owner/customer_viewer session, returns {"customer_id":
    their own id, "entitled": bool}: entitled reflects whether Face
    Access (analytics_entitlements' "facial_recognition" analytic_key,
    the same add-on subscription_portal_page's own Add-ons list already
    sells) is currently active for this account. Embedding customer_id
    here grants nothing new -- _resolve_customer_id() already forces
    every actual API call to this exact same value regardless; this
    only removes an impossible manual-entry step for a role that could
    never type in any other value anyway."""
    if identity["role"] not in ("customer_owner", "customer_viewer"):
        return None
    from analytics_entitlements import get_active_analytics_for_customer

    customer_id = identity.get("customer_id") or ""
    active = set(get_active_analytics_for_customer(customer_id)) if customer_id else set()
    return {"customer_id": customer_id, "entitled": "facial_recognition" in active}


_FACE_ACCESS_UPSELL = '''<section class="panel">
<h2>Face Access required</h2>
<p class="health-detail">Facial recognition, watchlists, and match events are part of the Face Access add-on. Add it from My subscription to enroll people, manage watchlists, and review match events.</p>
<a class="action-button" href="/subscription-portal">Go to My subscription</a>
</section>'''


def _decode_image(image_base64: str):
    """base64 (optionally a data: URL) in, an OpenCV BGR ndarray out.
    Raises HTTPException(400) for anything malformed -- an enrollment
    upload is user input and must never reach cv2 as a raw, unvalidated
    blob."""
    import cv2
    import numpy as np

    if not image_base64:
        raise HTTPException(status_code=400, detail="image_base64 is required.")
    payload = image_base64.split(",", 1)[-1] if image_base64.startswith("data:") else image_base64
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(status_code=400, detail="image_base64 is not valid base64.") from error
    array = np.frombuffer(raw, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise HTTPException(status_code=400, detail="Could not decode image.")
    return image


def _save_face_crop(image_bgr, observation, *, customer_id: str, person_id: str, embedding_id: str) -> str:
    import cv2

    # customer_id was already validated in _resolve_customer_id(); person_id
    # and embedding_id are re-validated here too, even though both are
    # always this module's own secrets.token_hex()-based ids by the time
    # this function runs (add_reference_image() already required person_id
    # to resolve to a real row before this is ever called) -- see this
    # module's own _require_safe_path_segment() docstring for why that
    # transitive protection alone isn't treated as sufficient here.
    safe_person_id = _require_safe_path_segment(person_id, field="person_id")
    safe_embedding_id = _require_safe_path_segment(embedding_id, field="embedding_id")
    folder = facial_people.AAC_FACES_FOLDER / customer_id / safe_person_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{safe_embedding_id}.jpg"
    crop = image_bgr[
        observation.bbox.y : observation.bbox.y + observation.bbox.height,
        observation.bbox.x : observation.bbox.x + observation.bbox.width,
    ]
    cv2.imwrite(str(path), crop)
    return str(path)


# Admin portal pass (2026-09-26): staff previously had to type a raw
# customer id on every Facial Recognition page. The field keeps its id
# (each page script reads .value) but now offers the customers within the
# caller's tenant reach, by name, from GET /api/aac/customers.
def _customer_picker(input_id: str) -> str:
    return (
        f'<label>Customer<input id="{input_id}" list="aac-customer-options" placeholder="Choose or type a customer id" autocomplete="off"></label>'
        '<datalist id="aac-customer-options"></datalist>'
        "<script>fetch('/api/aac/customers').then(r=>r.ok?r.json():{customers:[]}).then(d=>{const list=document.getElementById('aac-customer-options');"
        "(d.customers||[]).forEach(c=>{const o=document.createElement('option');o.value=c.id;o.label=c.name||c.id;o.textContent=c.name||c.id;list.appendChild(o)})}).catch(()=>{})</script>\n"
    )


def _push_directory_now(customer_id: str) -> None:
    """Reduced access takes effect on the appliance now, not at its next
    5-minute sync (2026-10-01). Best effort: the periodic sync still runs."""
    if os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower() != "cloud":
        return
    try:
        import appliance_control
        appliance_control.notify_facial_directory_changed(customer_id)
    except Exception:
        pass


def _face_previews(image_bgr) -> list[dict]:
    """Faces found in one image, largest first (the one an enrollment would
    use), each with its quality and a small JPEG crop. Nothing is stored."""
    import cv2
    previews = []
    observations = sorted(facial_recognition.detect_and_embed(image_bgr),
                          key=lambda o: o.bbox.width * o.bbox.height, reverse=True)
    height, width = image_bgr.shape[:2]
    for index, observation in enumerate(observations[:5]):
        box = observation.bbox
        pad = int(max(box.width, box.height) * 0.25)
        x0, y0 = max(0, box.x - pad), max(0, box.y - pad)
        x1, y1 = min(width, box.x + box.width + pad), min(height, box.y + box.height + pad)
        ok, encoded = cv2.imencode(".jpg", image_bgr[y0:y1, x0:x1], [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        previews.append({
            "quality": observation.quality, "width": box.width, "height": box.height,
            "used_for_enrollment": index == 0,
            "crop_jpeg_base64": base64.b64encode(encoded.tobytes()).decode() if ok else None,
        })
    return previews


PERSON_PAGE_JS = r'''const PERSON_URL=id=>`/api/aac/people/${encodeURIComponent(id)}`+(FIXED_CUSTOMER_ID?'':`?customer_id=${encodeURIComponent(customerId())}`);
function customerId(){return FIXED_CUSTOMER_ID!==null?FIXED_CUSTOMER_ID:(document.getElementById('e-customer-id')?.value.trim()||'');}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
const $=id=>document.getElementById(id);
let personId=new URLSearchParams(location.search).get('person_id')||null;
let pendingImage=null;
function say(id,text){$(id).textContent=text;}

async function loadPerson(){
  if(!personId)return;
  const r=await fetch(PERSON_URL(personId));
  if(!r.ok){say('e-status','This person could not be loaded.');return;}
  const p=await r.json();
  $('e-heading').textContent=p.display_name;
  $('e-name').value=p.display_name||'';$('e-reference').value=p.external_reference||'';$('e-notes').value=p.notes||'';
  $('e-create').textContent='Save details';
  $('e-after-create').hidden=false;
  const list=$('e-image-list');
  list.innerHTML=(p.reference_images||[]).length?('<p class="health-detail">'+p.reference_images.length+' face photo(s) enrolled.</p>'):'<p class="health-detail">No face photo yet. Add one below.</p>';
  if(typeof CAN_VIEW_DETAILS==='undefined'||CAN_VIEW_DETAILS)loadAccess();
  loadBackup();
}

$('e-create')?.addEventListener('click',async()=>{
  const body={customer_id:customerId(),display_name:$('e-name').value.trim(),external_reference:$('e-reference').value.trim(),notes:$('e-notes').value};
  if(!body.display_name){say('e-status','Enter the person\'s name.');return;}
  const r=personId?await fetch(PERSON_URL(personId),{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
                  :await fetch('/api/aac/people',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const result=await r.json().catch(()=>({}));
  if(!r.ok){say('e-status',result.detail||'Could not save.');return;}
  if(!personId){personId=result.person_id;history.replaceState(null,'',`?person_id=${encodeURIComponent(personId)}`);}
  say('e-status','Saved.');loadPerson();
});

// ---------- face photo: Take Photo / Upload Photo / Enroll from Camera
document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>{
  document.querySelectorAll('[data-tab]').forEach(x=>x.classList.toggle('active',x===b));
  document.querySelectorAll('[data-pane]').forEach(p=>p.hidden=p.dataset.pane!==b.dataset.tab);
  if(b.dataset.tab!=='take')stopCamera();
}));

async function preview(imageBase64,boxId){
  const box=$(boxId);box.innerHTML='<p class="health-detail">Looking for a face…</p>';
  const r=await fetch('/api/aac/face-preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({customer_id:customerId(),image_base64:imageBase64})});
  const data=await r.json().catch(()=>({faces:[]}));
  if(!r.ok||!data.faces.length){box.innerHTML='<p class="health-detail">No face was found. Face the camera in good light and try again.</p>';return null;}
  const face=data.faces[0];
  box.innerHTML=`<div class="face-pick"><img alt="Face to enroll" src="data:image/jpeg;base64,${face.crop_jpeg_base64}"><div><strong>This face will be enrolled.</strong><div class="health-detail">Quality ${Math.round(face.quality*100)}%${data.faces.length>1?` · ${data.faces.length} faces in the picture; the largest is used`:''}</div></div></div>`;
  return face;
}
async function saveFace(imageBase64,statusId){
  const r=await fetch(`/api/aac/people/${encodeURIComponent(personId)}/images`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({customer_id:customerId(),image_base64:imageBase64})});
  const result=await r.json().catch(()=>({}));
  say(statusId,r.ok?'Face photo enrolled.':(result.detail||'The photo could not be enrolled.'));
  if(r.ok)loadPerson();
  return r.ok;
}

let stream=null;
function stopCamera(){if(stream){stream.getTracks().forEach(t=>t.stop());stream=null;}}
$('take-start')?.addEventListener('click',async()=>{
  try{stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'user'},audio:false});$('take-video').srcObject=stream;$('take-video').hidden=false;$('take-snap').hidden=false;say('take-status','Look at the camera, then take the photo.');}
  catch(e){say('take-status','The camera could not be opened. Allow camera access, or use Upload Photo.');}
});
$('take-snap')?.addEventListener('click',async()=>{
  const v=$('take-video');if(!v.videoWidth)return;
  const c=document.createElement('canvas');c.width=v.videoWidth;c.height=v.videoHeight;c.getContext('2d').drawImage(v,0,0);
  pendingImage=c.toDataURL('image/jpeg',0.92);
  if(await preview(pendingImage,'take-preview'))$('take-save').hidden=false;
});
$('take-save')?.addEventListener('click',async()=>{if(pendingImage&&await saveFace(pendingImage,'take-status')){stopCamera();$('take-video').hidden=true;$('take-snap').hidden=true;$('take-save').hidden=true;}});

$('upload-file')?.addEventListener('change',()=>{
  const file=$('upload-file').files[0];if(!file)return;
  const reader=new FileReader();
  reader.onload=async()=>{pendingImage=reader.result;if(await preview(pendingImage,'upload-preview'))$('upload-save').hidden=false;};
  reader.readAsDataURL(file);
});
$('upload-save')?.addEventListener('click',()=>pendingImage&&saveFace(pendingImage,'upload-status'));

let cameraCandidates=[];
$('cam-start')?.addEventListener('click',async()=>{
  const cameraId=$('cam-select').value;if(!cameraId)return;
  $('cam-start').disabled=true;$('cam-candidates').innerHTML='';cameraCandidates=[];
  say('cam-status','Starting the camera… ask the person to look at it.');
  try{await fetch(`/api/customer/cameras/${encodeURIComponent(cameraId)}/live/start`,{method:'POST'});}catch(e){}
  for(let i=0;i<16&&cameraCandidates.length<6;i++){
    await new Promise(r=>setTimeout(r,i?1200:2000));
    say('cam-status',`Capturing… ${i+1}`);
    // The camera's cloud live stream only produces frames while it is being
    // watched (2026-10-01, staging): keep its playlist active while capturing.
    try{await fetch(`/api/customer/cameras/${encodeURIComponent(cameraId)}/live/playlist.m3u8`,{cache:'no-store'});}catch(e){}
    const r=await fetch(`/api/customer/cameras/${encodeURIComponent(cameraId)}/live/still.jpg`,{cache:'no-store'});
    if(!r.ok)continue;
    const b=await r.blob();const data=await new Promise(res=>{const fr=new FileReader();fr.onload=()=>res(fr.result);fr.readAsDataURL(b);});
    const p=await fetch('/api/aac/face-preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({customer_id:customerId(),image_base64:data})});
    const found=await p.json().catch(()=>({faces:[]}));
    if(p.ok&&found.faces.length)cameraCandidates.push({image:data,face:found.faces[0]});
  }
  $('cam-start').disabled=false;
  if(!cameraCandidates.length){say('cam-status','No usable face was captured. Have the person stand closer to the camera, facing it, and try again.');return;}
  cameraCandidates.sort((a,b)=>b.face.quality-a.face.quality);
  say('cam-status','Choose the best picture of this person, then confirm.');
  $('cam-candidates').innerHTML=cameraCandidates.slice(0,4).map((c,i)=>`<label class="face-pick"><input type="radio" name="cam-pick" value="${i}" ${i===0?'checked':''}><img alt="Captured face ${i+1}" src="data:image/jpeg;base64,${c.face.crop_jpeg_base64}"><span class="health-detail">Quality ${Math.round(c.face.quality*100)}%</span></label>`).join('')
    +'<button class="action-button" id="cam-confirm" type="button">Confirm and enroll this face</button>';
  $('cam-confirm').addEventListener('click',()=>{const pick=document.querySelector('[name=cam-pick]:checked');if(pick)saveFace(cameraCandidates[+pick.value].image,'cam-status');});
});

// ---------- Face Access
const DAYS=['mon','tue','wed','thu','fri','sat','sun'],DAY_LABEL={mon:'Mon',tue:'Tue',wed:'Wed',thu:'Thu',fri:'Fri',sat:'Sat',sun:'Sun'};
async function loadAccess(){
  const r=await fetch(PERSON_URL(personId).replace(/(\?|$)/,'/access$1'));
  if(!r.ok)return;
  const a=await r.json();
  $('a-enabled').checked=a.access_enabled;$('a-unit').value=a.unit||'';
  $('a-site').innerHTML='<option value="">—</option>'+a.sites.map(s=>`<option value="${esc(s.id)}" ${s.id===a.site_id?'selected':''}>${esc(s.name)}</option>`).join('');
  $('a-starts').value=a.access_starts_on||'';$('a-expires').value=a.access_expires_on||'';
  $('a-doors').innerHTML=a.doors.length?a.doors.map(d=>`<fieldset class="door-grant" data-camera="${esc(d.camera_id)}"><legend><label><input type="checkbox" class="d-allowed" ${d.allowed?'checked':''} ${CAN_MANAGE?'':'disabled'}> ${esc(d.name)}${d.site?` <span class="health-detail">· ${esc(d.site)}</span>`:''}</label></legend>
      <div class="day-row">${DAYS.map(x=>`<label><input type="checkbox" class="d-day" value="${x}" ${(!d.days.length||d.days.includes(x))?'checked':''} ${CAN_MANAGE?'':'disabled'}>${DAY_LABEL[x]}</label>`).join('')}</div>
      <div class="time-row"><label>From<input type="time" class="d-start" value="${esc(d.start||'')}" ${CAN_MANAGE?'':'disabled'}></label><label>To<input type="time" class="d-end" value="${esc(d.end||'')}" ${CAN_MANAGE?'':'disabled'}></label><span class="health-detail">Leave both empty for any time.</span></div></fieldset>`).join('')
    :'<p class="health-detail">No doors are set up yet. Turn on Face Access for a door camera in its Camera Settings.</p>';
}
$('a-save')?.addEventListener('click',async()=>{
  const doors=[...document.querySelectorAll('.door-grant')].map(f=>{const days=[...f.querySelectorAll('.d-day:checked')].map(x=>x.value);
    return {camera_id:f.dataset.camera,allowed:f.querySelector('.d-allowed').checked,days:days.length===7?[]:days,start:f.querySelector('.d-start').value,end:f.querySelector('.d-end').value};});
  if(doors.some(d=>d.allowed&&d.days.length===0&&[...document.querySelectorAll(`.door-grant[data-camera="${d.camera_id}"] .d-day:checked`)].length===0)){say('a-status','Pick at least one day for each allowed door.');return;}
  const body={customer_id:customerId(),access_enabled:$('a-enabled').checked,unit:$('a-unit').value,site_id:$('a-site').value,access_starts_on:$('a-starts').value,access_expires_on:$('a-expires').value,doors};
  const r=await fetch(PERSON_URL(personId).replace(/(\?|$)/,'/access$1'),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const result=await r.json().catch(()=>({}));
  say('a-status',r.ok?'Face Access saved.':(result.detail||'Could not save Face Access.'));
  if(r.ok)loadAccess();
});
// ---------- Backup Mobile Access
async function loadBackup(){
  const r=await fetch(PERSON_URL(personId).replace(/(\?|$)/,'/backup-access$1'));
  if(!r.ok){$('b-panel').hidden=true;return}
  const b=await r.json();$('b-panel').hidden=false;
  $('b-pin-state').textContent=b.locked?'Too many wrong PINs -- backup access is paused for a few minutes.':(b.pin_set?`A Backup Access PIN is set${b.pin_set_at?' (since '+new Date(b.pin_set_at).toLocaleDateString()+')':''}. It is never shown.`:'No Backup Access PIN is set yet.');
  $('b-pin-manage').hidden=!b.can_manage_pin;$('b-pin-save').textContent=b.pin_set?'Change PIN':'Set PIN';$('b-pin-reset').hidden=!b.pin_set;
  const usable=b.doors.filter(d=>d.open_now);
  $('b-unlock').hidden=!(b.can_unlock&&b.pin_set&&usable.length);
  $('b-door').innerHTML=usable.map(d=>`<option value="${esc(d.camera_id)}">${esc(d.name)}</option>`).join('');
  if(b.can_unlock&&b.pin_set&&!usable.length)say('b-status',b.doors.length?'None of this person’s doors are open to them right now.':'This person has no doors yet.');
}
$('b-pin-save')?.addEventListener('click',async()=>{
  const r=await fetch(PERSON_URL(personId).replace(/(\?|$)/,'/backup-pin$1'),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:$('b-pin-new').value})});
  const x=await r.json().catch(()=>({}));$('b-pin-new').value='';say('b-status',x.message||x.detail||'Could not save the PIN.');if(r.ok)loadBackup();
});
$('b-pin-reset')?.addEventListener('click',async()=>{
  if(!confirm('Remove this person’s Backup Access PIN? They can’t use backup access until a new one is set.'))return;
  const r=await fetch(PERSON_URL(personId).replace(/(\?|$)/,'/backup-pin$1'),{method:'DELETE'});
  const x=await r.json().catch(()=>({}));say('b-status',x.message||x.detail||'');if(r.ok)loadBackup();
});
$('b-go')?.addEventListener('click',async()=>{
  const btn=$('b-go');btn.disabled=true;say('b-status','Checking…');
  const r=await fetch(PERSON_URL(personId).replace(/(\?|$)/,'/backup-unlock$1'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({camera_id:$('b-door').value,pin:$('b-pin').value})});
  const x=await r.json().catch(()=>({}));$('b-pin').value='';btn.disabled=false;
  say('b-status',x.message||x.detail||'The door was not unlocked.');loadBackup();
});
loadPerson();
'''


def register_facial_recognition_routes(app: FastAPI, shell: Callable) -> None:
    # ---------------------------------------------------------------- API

    @app.get("/api/aac/customers")
    def aac_customers(request: Request) -> dict:
        """Customers within the caller's tenant reach (same rule as
        _resolve_customer_id): a customer account sees only itself."""
        identity = _require(request, "facial.view")
        from partner_db import connection as _tenant_connection, tenant_owns_partner

        with _tenant_connection() as db:
            rows = db.execute("SELECT id, name, partner_id FROM customers ORDER BY name").fetchall()
            if identity["role"] in ("customer_owner", "customer_viewer"):
                visible = [row for row in rows if row["id"] == identity.get("customer_id")]
            else:
                visible = [row for row in rows if tenant_owns_partner(db, identity, row["partner_id"])]
        return {"customers": [{"id": row["id"], "name": row["name"] or row["id"]} for row in visible]}

    @app.get("/api/aac/capability")
    def aac_capability(request: Request) -> dict:
        _require(request, "facial.view")
        return facial_recognition.capability()

    @app.get("/api/aac/people")
    def aac_list_people(request: Request, customer_id: str | None = None, site_id: str | None = None, status: str | None = None, search: str | None = None) -> dict:
        identity = _require(request, "facial.profile")
        _require_face_access(identity)
        resolved = _resolve_customer_id(identity, customer_id)
        with connection() as db:
            people = facial_people.list_people(db, customer_id=resolved, site_id=site_id, status=status, search=search)
        if not facial_allowed(identity, "facial.view"):
            people = [_profile_only(person) for person in people]
        return {"people": people}

    @app.post("/api/aac/people")
    def aac_enroll_person(request: Request, payload: dict) -> dict:
        identity = _require(request, "facial.manage")
        _require_face_access(identity)  # licensed Face Access feature (2026-10-02)
        resolved = _resolve_customer_id(identity, payload.get("customer_id"))
        try:
            with connection() as db:
                person_id = facial_people.enroll_person(
                    db,
                    customer_id=resolved,
                    display_name=str(payload.get("display_name", "")),
                    site_id=payload.get("site_id"),
                    external_reference=payload.get("external_reference"),
                    notes=payload.get("notes"),
                    created_by=identity.get("email"),
                    now=datetime.now().isoformat(),
                )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        audit(identity, "facial.person.enrolled", "facial_people", person_id, {"customer_id": resolved})
        return {"person_id": person_id}

    @app.get("/api/aac/people/{person_id}")
    def aac_get_person(request: Request, person_id: str, customer_id: str | None = None) -> dict:
        identity = _require(request, "facial.profile")
        _require_face_access(identity)
        resolved = _resolve_customer_id(identity, customer_id)
        with connection() as db:
            person = facial_people.get_person(db, customer_id=resolved, person_id=person_id)
            if person is None:
                raise HTTPException(status_code=404, detail="Person not found.")
            if not facial_allowed(identity, "facial.view"):
                return _profile_only(person)
            person["reference_images"] = facial_people.list_reference_images(db, customer_id=resolved, person_id=person_id)
            person["watchlists"] = facial_people.person_watchlist_memberships(db, customer_id=resolved, person_id=person_id)
        return person

    @app.patch("/api/aac/people/{person_id}")
    def aac_update_person(request: Request, person_id: str, payload: dict) -> dict:
        identity = _require(request, "facial.manage")
        _require_face_access(identity)  # licensed Face Access feature (2026-10-02)
        resolved = _resolve_customer_id(identity, payload.get("customer_id"))
        try:
            with connection() as db:
                updated = facial_people.update_person(
                    db,
                    customer_id=resolved,
                    person_id=person_id,
                    display_name=payload.get("display_name"),
                    external_reference=payload.get("external_reference"),
                    notes=payload.get("notes"),
                    status=payload.get("status"),
                    now=datetime.now().isoformat(),
                )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if not updated:
            raise HTTPException(status_code=404, detail="Person not found.")
        audit(identity, "facial.person.updated", "facial_people", person_id, {"customer_id": resolved, "fields": list(payload.keys())})
        if payload.get("status") not in (None, "active"):
            _push_directory_now(resolved)  # a disabled person leaves the appliance now
        return {"status": "updated"}

    @app.delete("/api/aac/people/{person_id}")
    def aac_delete_person(request: Request, person_id: str, customer_id: str | None = None) -> dict:
        identity = _require(request, "facial.manage")
        resolved = _resolve_customer_id(identity, customer_id)
        with connection() as db:
            deleted = facial_people.delete_person(db, customer_id=resolved, person_id=person_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Person not found.")
        # Audited AFTER deletion succeeds, deliberately: the audit_logs
        # row records that a real deletion happened, not merely that one
        # was requested -- and delete_person() itself never raises for a
        # tenant-scoped not-found (handled above), so there is no
        # ordering risk of auditing a deletion that silently failed.
        audit(identity, "facial.person.deleted", "facial_people", person_id, {"customer_id": resolved, "biometric_templates": "hard_deleted"})
        _push_directory_now(resolved)
        return {"status": "deleted"}

    @app.get("/api/aac/people/{person_id}/access")
    def aac_person_access(request: Request, person_id: str, customer_id: str | None = None) -> dict:
        identity = _require(request, "facial.view")
        _require_face_access(identity)  # licensed Face Access feature (2026-10-02)
        resolved = _resolve_customer_id(identity, customer_id)
        import face_access_people
        with connection() as db:
            settings = face_access_people.get_access(db, customer_id=resolved, person_id=person_id)
        if settings is None:
            raise HTTPException(status_code=404, detail="Person not found.")
        return settings

    @app.put("/api/aac/people/{person_id}/access")
    def aac_save_person_access(request: Request, person_id: str, payload: dict) -> dict:
        identity = _require(request, "facial.manage")
        _require_face_access(identity)
        resolved = _resolve_customer_id(identity, payload.get("customer_id"))
        import face_access_people
        try:
            with connection() as db:
                if identity.get("role") == "customer_viewer":
                    # Users & household: a delegated Face Access manager only
                    # changes grants on doors they may unlock themselves.
                    import household_users
                    before = face_access_people.get_access(db, customer_id=resolved, person_id=person_id)
                    if before is None:
                        raise LookupError(person_id)
                    payload = household_users.restrict_face_access_payload(before, payload, household_users.unlockable_door_ids(identity))
                outcome = face_access_people.save_access(db, customer_id=resolved, person_id=person_id, payload=payload,
                                                          actor=identity.get("email", ""), now=datetime.now().isoformat())
        except LookupError as error:
            raise HTTPException(status_code=404, detail="Person not found.") from error
        except face_access_people.AccessSettingsError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        settings = outcome["settings"]
        audit(identity, "facial.person.access_updated", "facial_people", person_id, {
            "customer_id": resolved, "access_enabled": settings["access_enabled"], "unit": settings["unit"],
            "starts_on": settings["access_starts_on"], "expires_on": settings["access_expires_on"],
            "doors": [d["camera_id"] for d in settings["doors"] if d["allowed"]],
        })
        if outcome["access_reduced"]:
            _push_directory_now(resolved)
        return settings

    @app.post("/api/aac/face-preview")
    def aac_face_preview(request: Request, payload: dict) -> dict:
        """Take Photo / Enroll from Camera: which faces this image holds and
        which one an enrollment would use, before anything is saved."""
        identity = _require(request, "facial.manage")
        _require_face_access(identity)
        _resolve_customer_id(identity, payload.get("customer_id"))
        image = _decode_image(str(payload.get("image_base64", "")))
        return {"faces": _face_previews(image)}

    @app.post("/api/aac/people/{person_id}/images")
    def aac_add_reference_image(request: Request, person_id: str, payload: dict) -> dict:
        identity = _require(request, "facial.manage")
        _require_face_access(identity)  # licensed Face Access feature (2026-10-02)
        resolved = _resolve_customer_id(identity, payload.get("customer_id"))
        image = _decode_image(str(payload.get("image_base64", "")))
        observations = facial_recognition.detect_and_embed(image)
        if not observations:
            raise HTTPException(status_code=422, detail="No face was found in the uploaded image.")
        # The largest detected face is used when more than one face
        # appears in an enrollment photo -- the enrolled subject is
        # assumed to be the dominant, closest-to-camera face, matching
        # how a typical ID-badge-style enrollment photo is framed.
        best = max(observations, key=lambda observation: observation.bbox.width * observation.bbox.height)
        now = datetime.now().isoformat()
        with connection() as db:
            embedding_id = facial_people.add_reference_image(
                db,
                customer_id=resolved,
                person_id=person_id,
                embedding=best.embedding,
                engine=best.engine,
                engine_version=best.engine_version,
                source_image_path=None,
                quality=best.quality,
                now=now,
            )
            if embedding_id is None:
                raise HTTPException(status_code=404, detail="Person not found.")
            # Only the aligned face crop is ever written to disk -- the
            # original upload (`image`, decoded above) is never persisted
            # anywhere, matching this module's own "avoid unnecessary
            # duplication of original enrollment images" requirement.
            crop_path = _save_face_crop(image, best, customer_id=resolved, person_id=person_id, embedding_id=embedding_id)
            db.execute("UPDATE facial_embeddings SET source_image_path=? WHERE id=? AND customer_id=?", (crop_path, embedding_id, resolved))
        audit(identity, "facial.person.image_added", "facial_people", person_id, {"customer_id": resolved, "embedding_id": embedding_id})
        return {"embedding_id": embedding_id, "quality": best.quality, "faces_detected": len(observations)}

    @app.delete("/api/aac/people/{person_id}/images/{embedding_id}")
    def aac_delete_reference_image(request: Request, person_id: str, embedding_id: str, customer_id: str | None = None) -> dict:
        identity = _require(request, "facial.manage")
        _require_face_access(identity)  # licensed Face Access feature (2026-10-02)
        resolved = _resolve_customer_id(identity, customer_id)
        with connection() as db:
            deleted = facial_people.delete_reference_image(db, customer_id=resolved, person_id=person_id, embedding_id=embedding_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Reference image not found.")
        audit(identity, "facial.person.image_deleted", "facial_people", person_id, {"customer_id": resolved, "embedding_id": embedding_id})
        return {"status": "deleted"}

    @app.get("/api/aac/watchlists")
    def aac_list_watchlists(request: Request, customer_id: str | None = None) -> dict:
        identity = _require(request, "facial.view")
        _require_face_access(identity)  # licensed Face Access feature (2026-10-02)
        resolved = _resolve_customer_id(identity, customer_id)
        with connection() as db:
            watchlists = facial_people.list_watchlists(db, customer_id=resolved)
        return {"watchlists": watchlists}

    @app.post("/api/aac/watchlists")
    def aac_create_watchlist(request: Request, payload: dict) -> dict:
        identity = _require(request, "facial.manage")
        _require_face_access(identity)  # licensed Face Access feature (2026-10-02)
        resolved = _resolve_customer_id(identity, payload.get("customer_id"))
        try:
            with connection() as db:
                watchlist_id = facial_people.create_watchlist(
                    db,
                    customer_id=resolved,
                    name=str(payload.get("name", "")),
                    site_id=payload.get("site_id"),
                    classification=payload.get("classification", "alert"),
                    description=payload.get("description"),
                    created_by=identity.get("email"),
                    now=datetime.now().isoformat(),
                )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        audit(identity, "facial.watchlist.created", "facial_watchlists", watchlist_id, {"customer_id": resolved})
        return {"watchlist_id": watchlist_id}

    @app.delete("/api/aac/watchlists/{watchlist_id}")
    def aac_delete_watchlist(request: Request, watchlist_id: str, customer_id: str | None = None) -> dict:
        identity = _require(request, "facial.manage")
        _require_face_access(identity)  # licensed Face Access feature (2026-10-02)
        resolved = _resolve_customer_id(identity, customer_id)
        with connection() as db:
            deleted = facial_people.delete_watchlist(db, customer_id=resolved, watchlist_id=watchlist_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Watchlist not found.")
        audit(identity, "facial.watchlist.deleted", "facial_watchlists", watchlist_id, {"customer_id": resolved})
        return {"status": "deleted"}

    @app.get("/api/aac/watchlists/{watchlist_id}/members")
    def aac_list_watchlist_members(request: Request, watchlist_id: str, customer_id: str | None = None) -> dict:
        identity = _require(request, "facial.view")
        _require_face_access(identity)  # licensed Face Access feature (2026-10-02)
        resolved = _resolve_customer_id(identity, customer_id)
        with connection() as db:
            members = facial_people.list_watchlist_members(db, customer_id=resolved, watchlist_id=watchlist_id)
        return {"members": members}

    @app.post("/api/aac/watchlists/{watchlist_id}/members")
    def aac_add_watchlist_member(request: Request, watchlist_id: str, payload: dict) -> dict:
        identity = _require(request, "facial.manage")
        _require_face_access(identity)  # licensed Face Access feature (2026-10-02)
        resolved = _resolve_customer_id(identity, payload.get("customer_id"))
        with connection() as db:
            added = facial_people.add_watchlist_member(
                db,
                customer_id=resolved,
                watchlist_id=watchlist_id,
                person_id=str(payload.get("person_id", "")),
                added_by=identity.get("email"),
                now=datetime.now().isoformat(),
            )
        if not added:
            raise HTTPException(status_code=404, detail="Watchlist or person not found.")
        audit(identity, "facial.watchlist.member_added", "facial_watchlists", watchlist_id, {"customer_id": resolved, "person_id": payload.get("person_id")})
        return {"status": "added"}

    @app.delete("/api/aac/watchlists/{watchlist_id}/members/{person_id}")
    def aac_remove_watchlist_member(request: Request, watchlist_id: str, person_id: str, customer_id: str | None = None) -> dict:
        identity = _require(request, "facial.manage")
        _require_face_access(identity)  # licensed Face Access feature (2026-10-02)
        resolved = _resolve_customer_id(identity, customer_id)
        with connection() as db:
            removed = facial_people.remove_watchlist_member(db, customer_id=resolved, watchlist_id=watchlist_id, person_id=person_id)
        if not removed:
            raise HTTPException(status_code=404, detail="Watchlist not found.")
        audit(identity, "facial.watchlist.member_removed", "facial_watchlists", watchlist_id, {"customer_id": resolved, "person_id": person_id})
        return {"status": "removed"}

    @app.get("/api/aac/events")
    def aac_list_events(
        request: Request,
        customer_id: str | None = None,
        site_id: str | None = None,
        camera_id: str | None = None,
        person_id: str | None = None,
        match_state: str | None = None,
        watchlist_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        identity = _require(request, "facial.view")
        _require_face_access(identity)  # licensed Face Access feature (2026-10-02)
        resolved = _resolve_customer_id(identity, customer_id)
        with connection() as db:
            events = facial_events.list_events(
                db,
                customer_id=resolved,
                site_id=site_id,
                camera_id=camera_id,
                person_id=person_id,
                match_state=match_state,
                watchlist_id=watchlist_id,
                start=start,
                end=end,
                limit=min(max(limit, 1), 500),
                offset=max(offset, 0),
            )
        return {"events": events}

    @app.get("/api/aac/events/{event_id}")
    def aac_event_detail(request: Request, event_id: str, customer_id: str | None = None) -> dict:
        identity = _require(request, "facial.view")
        _require_face_access(identity)  # licensed Face Access feature (2026-10-02)
        resolved = _resolve_customer_id(identity, customer_id)
        with connection() as db:
            event = facial_events.get_event_detail(db, customer_id=resolved, event_id=event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        # Face crops stay on the appliance that saw the face (owner policy,
        # 2026-10-02: no cloud face images at launch). Say plainly whether a
        # preview can be shown here, never a path or a broken image.
        path = event.pop("face_thumbnail_path", None)
        event["face_preview_available"] = bool(path) and Path(path).is_file()
        if not event["face_preview_available"]:
            event["face_preview_note"] = (FACE_PREVIEW_LOCAL_ONLY if _runtime_role() == "cloud"
                                          else "No face preview was saved for this match.")
        return event

    @app.get("/api/aac/settings")
    def aac_get_settings(request: Request, customer_id: str | None = None) -> dict:
        identity = _require(request, "facial.view")
        _require_face_access(identity)  # licensed Face Access feature (2026-10-02)
        resolved = _resolve_customer_id(identity, customer_id)
        with connection() as db:
            return facial_people.get_settings(db, customer_id=resolved)

    @app.put("/api/aac/settings")
    def aac_update_settings(request: Request, payload: dict) -> dict:
        identity = _require(request, "facial.manage")
        _require_face_access(identity)  # licensed Face Access feature (2026-10-02)
        resolved = _resolve_customer_id(identity, payload.get("customer_id"))
        min_confidence = payload.get("min_confidence")
        if min_confidence is not None and not (0.0 <= float(min_confidence) <= 1.0):
            raise HTTPException(status_code=400, detail="min_confidence must be between 0 and 1.")
        with connection() as db:
            settings = facial_people.update_settings(
                db,
                customer_id=resolved,
                min_confidence=min_confidence,
                unknown_person_events_enabled=payload.get("unknown_person_events_enabled"),
                debounce_seconds=payload.get("debounce_seconds"),
                engine=payload.get("engine"),
                now=datetime.now().isoformat(),
            )
        audit(identity, "facial.settings.updated", "facial_settings", resolved, {"customer_id": resolved})
        return settings

    # --------------------------------------------------------------- pages

    @app.get("/aac/people", response_class=HTMLResponse)
    def aac_people_page(request: Request):
        identity = _require(request, "facial.profile")
        facial_ctx = _customer_facial_context(identity)
        if facial_ctx and not facial_ctx["entitled"]:
            return shell("Facial Recognition · People", "aac", '<header class="topbar"><div><p class="eyebrow">Facial Recognition</p><h1>People</h1></div></header>' + _FACE_ACCESS_UPSELL)
        customer_id_field = "" if facial_ctx else _customer_picker("aac-customer-id")
        empty_message = "Loading your people…" if facial_ctx else "Choose a customer and refresh."
        fixed_customer_id_js = "const FIXED_CUSTOMER_ID=" + (json.dumps(facial_ctx["customer_id"]) if facial_ctx else "null") + ";"
        content = ('''<header class="topbar"><div><p class="eyebrow">Facial Recognition</p><h1>People</h1></div>
<a class="action-button" href="/aac/people/enroll">Add person</a></header>
<section class="panel">''' + customer_id_field + '''<label>Search<input id="aac-search"></label><button class="ghost-button" id="aac-refresh">Refresh</button></section>
<section class="panel" id="aac-people-list"><p class="health-detail">''' + empty_message + '''</p></section>''')
        scripts = '''<script>
''' + fixed_customer_id_js + '''
function aacGetCustomerId(){return FIXED_CUSTOMER_ID!==null?FIXED_CUSTOMER_ID:(document.getElementById('aac-customer-id')?.value.trim()||'');}
function aacEsc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function aacLoadPeople(){
  const customerId=aacGetCustomerId();
  const search=document.getElementById('aac-search').value.trim();
  if(!customerId)return;
  const params=new URLSearchParams({customer_id:customerId});
  if(search)params.set('search',search);
  const response=await fetch('/api/aac/people?'+params.toString());
  const box=document.getElementById('aac-people-list');
  if(!response.ok){box.innerHTML='<p class="health-detail">Unable to load people.</p>';return;}
  const data=await response.json();
  if(!data.people.length){box.innerHTML='<p class="empty">No enrolled people yet.</p>';return;}
  // display_name/external_reference are free text a 'facial.manage' user
  // chose (enroll_person()/update_person() accept any string) -- every
  // value below is HTML-escaped before it reaches innerHTML so a
  // maliciously-crafted name can never execute as markup/script in
  // another user's (e.g. an administrator's) browser session.
  // Phone-first cards (2026-10-01): name, unit and Face Access at a glance.
  box.innerHTML='<div class="people-cards">'+data.people.map(p=>{const access=p.status!=='active'?'Disabled':(p.access_enabled===0||p.access_enabled===false?'Face Access off':'Face Access on');
    return `<div class="setting-link"><a href="/aac/people/enroll?person_id=${encodeURIComponent(p.id)}${FIXED_CUSTOMER_ID?'':'&customer_id='+encodeURIComponent(customerId)}"><strong>${aacEsc(p.display_name)}</strong><div class="health-detail">${p.unit?'Unit '+aacEsc(p.unit)+' · ':''}${aacEsc(access)}${p.external_reference?' · '+aacEsc(p.external_reference):''}</div></a>
    <button class="ghost-button" type="button" onclick="aacDeletePerson('${p.id}','${customerId}')">Remove</button></div>`;}).join('')+'</div>';
}
async function aacDeletePerson(personId,customerId){
  if(!confirm('Remove this person? Their face templates and door access are deleted now; past recognition and door history is kept.'))return;
  await fetch(`/api/aac/people/${personId}?customer_id=${customerId}`,{method:'DELETE'});
  aacLoadPeople();
}
document.getElementById('aac-refresh').addEventListener('click',aacLoadPeople);
document.getElementById('aac-search').addEventListener('keydown',e=>{if(e.key==='Enter')aacLoadPeople();});
if(FIXED_CUSTOMER_ID!==null)aacLoadPeople();
</script>'''
        return shell("Facial Recognition · People", "aac", content, scripts)

    @app.get("/aac/people/enroll", response_class=HTMLResponse)
    def aac_enroll_page(request: Request):
        """Add or edit one person (2026-10-01): details; a face photo by Take
        Photo, Upload Photo or Enroll from Camera (each previews the face that
        will be enrolled before anything is saved); and Face Access -- on/off,
        unit/apartment, site/building, start and expiry dates, and the doors
        they may open on which days and hours. Phone-first layout."""
        identity = _require(request, "facial.profile")
        facial_ctx = _customer_facial_context(identity)
        title = "Person"
        if facial_ctx and not facial_ctx["entitled"]:
            return shell(title, "aac", '<header class="topbar"><div><p class="eyebrow">Facial Recognition</p><h1>Enroll person</h1></div></header>' + _FACE_ACCESS_UPSELL)
        can_manage = facial_allowed(identity, "facial.manage")
        # Backup Access alone (2026-10-02): the face photo and Face Access
        # panels are not theirs to see; their data is not sent either.
        can_view_details = facial_allowed(identity, "facial.view")
        details_hidden = "" if can_view_details else " hidden"
        customer_id_field = "" if facial_ctx else _customer_picker("e-customer-id")
        cameras = []
        if facial_ctx:
            with connection() as db:
                cameras = [dict(row) for row in db.execute(
                    "SELECT id,name FROM cameras WHERE customer_id=? AND camera_number IS NOT NULL AND (status IS NULL OR status<>'deleted') ORDER BY name", (facial_ctx["customer_id"],)
                ).fetchall()]
        camera_options = "".join(f'<option value="{html.escape(c["id"], quote=True)}">{html.escape(c["name"] or "Camera")}</option>' for c in cameras)
        ro = "" if can_manage else " disabled"
        content = ('''<style>
.person-page .panel{max-width:760px}.person-page label{display:grid;gap:6px;margin:10px 0}
.person-page input,.person-page select,.person-page textarea{width:100%;box-sizing:border-box}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0 12px}.tabs button{flex:1 1 auto;min-height:44px}.tabs button.active{outline:2px solid var(--brand-soft,#42e4dc)}
.face-pick{display:flex;gap:12px;align-items:center;margin:10px 0}.face-pick img{width:96px;height:96px;object-fit:cover;border-radius:10px}
#take-video{width:100%;max-width:480px;border-radius:10px;background:#000}
.door-grant{border:1px solid rgba(170,196,207,.25);border-radius:10px;padding:10px;margin:10px 0}
.day-row{display:flex;flex-wrap:wrap;gap:4px 12px}.day-row label{display:flex;gap:4px;align-items:center;margin:4px 0}
.day-row input{width:auto}.time-row{display:grid;grid-template-columns:1fr 1fr;gap:8px;align-items:end}.time-row .health-detail{grid-column:1/-1}
.toggle{display:flex!important;gap:10px;align-items:center}.toggle input{width:auto}
</style>
<div class="person-page"><header class="topbar"><div><p class="eyebrow">Facial Recognition</p><h1 id="e-heading">Add person</h1></div><a class="ghost-button" href="/aac/people">People</a></header>
<section class="panel"><h2>Details</h2>''' + customer_id_field + f'''
<label>Name (person or tenant)<input id="e-name" required{ro}></label>
<label>Reference (optional, e.g. lease or employee number)<input id="e-reference"{ro}></label>
<label>Notes<textarea id="e-notes"{ro}></textarea></label>
''' + ('<button class="action-button" id="e-create" type="button">Add person</button>' if can_manage else "") + '''<p role="status" id="e-status" class="health-detail"></p></section>
<div id="e-after-create" hidden>
<section class="panel" id="e-photo-panel"__DETAILS_HIDDEN__><h2>Face photo</h2><div id="e-image-list"></div>''' + ('''
<div class="tabs" role="tablist"><button class="ghost-button active" data-tab="take" type="button">Take Photo</button><button class="ghost-button" data-tab="upload" type="button">Upload Photo</button><button class="ghost-button" data-tab="camera" type="button">Enroll from Camera</button></div>
<div data-pane="take"><p class="health-detail">Use this phone, tablet or computer camera. Face the camera in good light.</p>
<button class="ghost-button" id="take-start" type="button">Open camera</button><video id="take-video" autoplay playsinline muted hidden></video>
<button class="action-button" id="take-snap" type="button" hidden>Take photo</button><div id="take-preview"></div>
<button class="action-button" id="take-save" type="button" hidden>Enroll this face</button><p class="health-detail" id="take-status" role="status"></p></div>
<div data-pane="upload" hidden><label>Choose a clear, front-facing photo<input id="upload-file" type="file" accept="image/*"></label><div id="upload-preview"></div>
<button class="action-button" id="upload-save" type="button" hidden>Enroll this face</button><p class="health-detail" id="upload-status" role="status"></p></div>
<div data-pane="camera" hidden><p class="health-detail">Have the person stand in front of one of your cameras and look at it for a few seconds. Several frames are captured; you choose the best face before it is enrolled.</p>
<label>Camera<select id="cam-select">''' + camera_options + '''</select></label><button class="ghost-button" id="cam-start" type="button">Capture from this camera</button>
<p class="health-detail" id="cam-status" role="status"></p><div id="cam-candidates"></div></div>
<p class="health-detail">Only the face area is stored as the template&#39;s reference -- the full photo is never saved.</p>''' if can_manage else "") + f'''</section>
<section class="panel" id="a-panel"__DETAILS_HIDDEN__><h2>Face Access</h2>
<label class="toggle"><input type="checkbox" id="a-enabled"{ro}> Face Access enabled for this person</label>
<label>Unit / apartment (optional)<input id="a-unit" maxlength="40"{ro}></label>
<label>Site / building<select id="a-site"{ro}></select></label>
<div class="time-row"><label>Start date<input type="date" id="a-starts"{ro}></label><label>Expiration date<input type="date" id="a-expires"{ro}></label><span class="health-detail">Leave empty for no limit. Turning Face Access off or an expired date stops every automatic unlock right away; history is kept.</span></div>
<h3>Doors this person may open</h3><div id="a-doors"></div>
''' + ('<button class="action-button" id="a-save" type="button">Save Face Access</button>' if can_manage else "") + '''<p class="health-detail" id="a-status" role="status"></p></section>
<section class="panel" id="b-panel" hidden><h2>Backup access</h2>
<p class="health-detail">If face recognition doesn't let this person in, they can unlock a door they're allowed through from this page with their own PIN. Keys and normal exits always keep working without AnyAiCam.</p>
<p id="b-pin-state" class="health-detail"></p>
<div id="b-pin-manage" hidden><label>New Backup Access PIN (6&ndash;10 digits)<input id="b-pin-new" type="password" inputmode="numeric" autocomplete="new-password" maxlength="10"></label>
<div style="display:flex;gap:8px;flex-wrap:wrap"><button class="ghost-button" id="b-pin-save" type="button">Set PIN</button><button class="ghost-button" id="b-pin-reset" type="button" hidden>Remove PIN</button></div></div>
<div id="b-unlock" hidden><label>Door<select id="b-door"></select></label>
<label>PIN<input id="b-pin" type="password" inputmode="numeric" autocomplete="off" maxlength="10"></label>
<button class="action-button" id="b-go" type="button">Unlock door</button></div>
<p class="health-detail" id="b-status" role="status" aria-live="polite"></p></section>
</div></div>''')
        content = content.replace("__DETAILS_HIDDEN__", details_hidden)
        scripts = ("<script>const FIXED_CUSTOMER_ID=" + (json.dumps(facial_ctx["customer_id"]) if facial_ctx else "null")
                   + ";const CAN_MANAGE=" + json.dumps(bool(can_manage)) + ";const CAN_VIEW_DETAILS=" + json.dumps(bool(can_view_details))
                   + ";\n" + PERSON_PAGE_JS + "</script>")
        return shell(title, "aac", content, scripts)

    @app.get("/aac/watchlists", response_class=HTMLResponse)
    def aac_watchlists_page(request: Request):
        identity = _require(request, "facial.view")
        facial_ctx = _customer_facial_context(identity)
        if facial_ctx and not facial_ctx["entitled"]:
            return shell("Facial Recognition · Watchlists", "aac", '<header class="topbar"><div><p class="eyebrow">Facial Recognition</p><h1>Watchlists</h1></div></header>' + _FACE_ACCESS_UPSELL)
        customer_id_field = "" if facial_ctx else _customer_picker("w-customer-id")
        fixed_customer_id_js = "const FIXED_CUSTOMER_ID=" + (json.dumps(facial_ctx["customer_id"]) if facial_ctx else "null") + ";"
        auto_load_js = "wLoad();" if facial_ctx else ""
        content = ('''<header class="topbar"><div><p class="eyebrow">Facial Recognition</p><h1>Watchlists</h1></div></header>
<section class="panel rule-form">''' + customer_id_field + '''<label>New watchlist name<input id="w-name"></label>
<label>Classification<select id="w-classification"><option value="alert">Alert</option><option value="allow">Allow (access rules)</option></select></label>
<button class="action-button" id="w-create">Create watchlist</button></section>
<section class="panel" id="w-list"></section>''')
        scripts = '''<script>
''' + fixed_customer_id_js + '''
function wGetCustomerId(){return FIXED_CUSTOMER_ID!==null?FIXED_CUSTOMER_ID:(document.getElementById('w-customer-id')?.value.trim()||'');}
function aacEsc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function wLoad(){
  const customerId=wGetCustomerId();
  if(!customerId)return;
  const response=await fetch('/api/aac/watchlists?customer_id='+encodeURIComponent(customerId));
  const box=document.getElementById('w-list');
  if(!response.ok){box.innerHTML='';return;}
  const data=await response.json();
  // w.name is free text a 'facial.manage' user chose -- escaped before
  // reaching innerHTML (see aac_people_page's own comment on this).
  box.innerHTML=data.watchlists.map(w=>`<div class="panel"><strong>${aacEsc(w.name)}</strong> (${aacEsc(w.classification)})
    <button class="ghost-button" onclick="wDelete('${w.id}','${customerId}')">Delete</button></div>`).join('')||'<p class="empty">No watchlists yet.</p>';
}
async function wDelete(id,customerId){await fetch(`/api/aac/watchlists/${id}?customer_id=${customerId}`,{method:'DELETE'});wLoad();}
document.getElementById('w-create').addEventListener('click',async()=>{
  const customerId=wGetCustomerId();
  await fetch('/api/aac/watchlists',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({customer_id:customerId,name:document.getElementById('w-name').value,classification:document.getElementById('w-classification').value})});
  wLoad();
});
document.getElementById('w-customer-id')?.addEventListener('change',wLoad);
''' + auto_load_js + '''
</script>'''
        return shell("Facial Recognition · Watchlists", "aac", content, scripts)

    @app.get("/aac/events", response_class=HTMLResponse)
    def aac_events_page(request: Request):
        identity = _require(request, "facial.view")
        facial_ctx = _customer_facial_context(identity)
        if facial_ctx and not facial_ctx["entitled"]:
            return shell("Facial Recognition · Events", "aac", '<header class="topbar"><div><p class="eyebrow">Facial Recognition</p><h1>Events</h1></div></header>' + _FACE_ACCESS_UPSELL)
        customer_id_field = "" if facial_ctx else _customer_picker("ev-customer-id")
        fixed_customer_id_js = "const FIXED_CUSTOMER_ID=" + (json.dumps(facial_ctx["customer_id"]) if facial_ctx else "null") + ";"
        content = ('''<header class="topbar"><div><p class="eyebrow">Facial Recognition</p><h1>Events</h1></div></header>
<section class="panel">''' + customer_id_field + '''<label>Match state<select id="ev-state"><option value="">All</option><option value="known">Known</option><option value="unknown">Unknown</option><option value="watchlist">Watchlist</option></select></label>
<button class="ghost-button" id="ev-refresh">Refresh</button></section>
<section class="panel" id="ev-list"></section>''')
        scripts = '''<script>
''' + fixed_customer_id_js + '''
function evGetCustomerId(){return FIXED_CUSTOMER_ID!==null?FIXED_CUSTOMER_ID:(document.getElementById('ev-customer-id')?.value.trim()||'');}
function aacEsc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
document.getElementById('ev-refresh').addEventListener('click',async()=>{
  const customerId=evGetCustomerId();
  if(!customerId)return;
  const params=new URLSearchParams({customer_id:customerId});
  const state=document.getElementById('ev-state').value;
  if(state)params.set('match_state',state);
  const response=await fetch('/api/aac/events?'+params.toString());
  const box=document.getElementById('ev-list');
  if(!response.ok){box.innerHTML='';return;}
  const data=await response.json();
  // matched_person_name is a denormalized snapshot of a display_name a
  // 'facial.manage' user chose -- escaped before reaching innerHTML (see
  // aac_people_page's own comment on this).
  box.innerHTML='<table class="data-table"><thead><tr><th>Time</th><th>Camera</th><th>State</th><th>Person</th><th>Confidence</th></tr></thead><tbody>'+
    data.events.map(e=>`<tr><td><a href="/aac/events/${encodeURIComponent(e.id)}?customer_id=${encodeURIComponent(customerId)}">${aacEsc(e.event_timestamp)}</a></td><td>${aacEsc(e.camera_id)}</td><td>${aacEsc(e.match_state)}</td><td>${aacEsc(e.matched_person_name||'—')}</td><td>${aacEsc(e.confidence)}</td></tr>`).join('')+'</tbody></table>';
});
</script>'''
        return shell("Facial Recognition · Events", "aac", content, scripts)

    @app.get("/api/aac/events/{event_id}/thumbnail")
    def aac_event_thumbnail(request: Request, event_id: str, customer_id: str | None = None):
        identity = _require(request, "facial.view")
        _require_face_access(identity)  # licensed Face Access feature (2026-10-02)
        resolved = _resolve_customer_id(identity, customer_id)
        with connection() as db:
            event = facial_events.get_event_detail(db, customer_id=resolved, event_id=event_id)
        # The path is read only from this already-tenant-scoped DB row --
        # never from any client-supplied path -- so there is no path-
        # traversal surface here regardless of what event_id looks like:
        # a caller can, at most, select WHICH of their own tenant's
        # already-recorded thumbnail paths gets served, never an
        # arbitrary filesystem path of their own choosing.
        path = Path(event["face_thumbnail_path"]) if event and event.get("face_thumbnail_path") else None
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="Thumbnail not found.")
        return FileResponse(str(path), media_type="image/jpeg")

    @app.get("/aac/events/{event_id}", response_class=HTMLResponse)
    def aac_event_detail_page(request: Request, event_id: str):
        identity = _require(request, "facial.view")
        facial_ctx = _customer_facial_context(identity)
        if facial_ctx and not facial_ctx["entitled"]:
            return shell("Facial Recognition · Match detail", "aac", '<header class="topbar"><div><p class="eyebrow">Facial Recognition</p><h1>Match detail</h1></div></header>' + _FACE_ACCESS_UPSELL)
        fixed_customer_id_js = "const FIXED_CUSTOMER_ID=" + (json.dumps(facial_ctx["customer_id"]) if facial_ctx else "null") + ";"
        # event_id is a raw URL path segment -- HTML-escaped here (server
        # side, before it ever reaches the response body) so a crafted
        # URL cannot break out of the data-event-id attribute.
        content = ('''<header class="topbar"><div><p class="eyebrow">Facial Recognition</p><h1>Match detail</h1></div></header>
<section class="panel" id="d-detail" data-event-id="''' + html.escape(event_id) + '''"></section>''')
        scripts = '''<script>
''' + fixed_customer_id_js + '''
function aacEsc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
(async()=>{
  const box=document.getElementById('d-detail');
  const eventId=box.dataset.eventId;
  const customerId=new URLSearchParams(location.search).get('customer_id')||FIXED_CUSTOMER_ID||'';
  const response=await fetch(`/api/aac/events/${encodeURIComponent(eventId)}?customer_id=${encodeURIComponent(customerId)}`);
  if(!response.ok){box.innerHTML='<p class="health-detail">Event not found.</p>';return;}
  const e=await response.json();
  // matched_person_name/matched_watchlist_name are denormalized snapshots
  // of free text a 'facial.manage' user chose -- escaped before reaching
  // innerHTML (see aac_people_page's own comment on this). The face
  // preview is shown only when the server says this appliance holds the
  // crop; otherwise its note says why (local-only, or none saved).
  const preview=e.face_preview_available
    ?`<img src="/api/aac/events/${encodeURIComponent(eventId)}/thumbnail?customer_id=${encodeURIComponent(customerId)}" alt="Face preview" style="max-width:200px;border-radius:8px;display:block;margin-bottom:12px">`
    :`<p class="health-detail" id="d-preview-note">${aacEsc(e.face_preview_note||'Face preview is not available.')}</p>`;
  box.innerHTML=preview+`
  <div class="health-row"><span>Time</span><strong>${aacEsc(e.event_timestamp)}</strong></div>
  <div class="health-row"><span>Camera</span><strong>${aacEsc(e.camera_id)}</strong></div>
  <div class="health-row"><span>State</span><strong>${aacEsc(e.match_state)}</strong></div>
  <div class="health-row"><span>Matched person</span><strong>${aacEsc(e.matched_person_name||'—')}</strong></div>
  <div class="health-row"><span>Watchlist</span><strong>${aacEsc(e.matched_watchlist_name||'—')}</strong></div>
  <div class="health-row"><span>Confidence</span><strong>${aacEsc(e.confidence)}</strong></div>
  <div class="health-row"><span>Engine</span><strong>${aacEsc(e.engine)}</strong></div>`;
})();
</script>'''
        return shell("Facial Recognition · Match detail", "aac", content, scripts)

    @app.get("/aac/settings", response_class=HTMLResponse)
    def aac_settings_page(request: Request):
        identity = _require(request, "facial.view")
        facial_ctx = _customer_facial_context(identity)
        if facial_ctx and not facial_ctx["entitled"]:
            return shell("Facial Recognition · Settings", "aac", '<header class="topbar"><div><p class="eyebrow">Facial Recognition</p><h1>Settings</h1></div></header>' + _FACE_ACCESS_UPSELL)
        customer_id_field = "" if facial_ctx else _customer_picker("s-customer-id")
        fixed_customer_id_js = "const FIXED_CUSTOMER_ID=" + (json.dumps(facial_ctx["customer_id"]) if facial_ctx else "null") + ";"
        auto_load_js = "sLoad();" if facial_ctx else ""
        content = ('''<header class="topbar"><div><p class="eyebrow">Facial Recognition</p><h1>Settings</h1></div></header>
<section class="panel rule-form" style="max-width:520px">
''' + customer_id_field + '''<button class="ghost-button" id="s-load">Load</button>
<label>Minimum confidence (0-1)<input id="s-confidence" type="number" min="0" max="1" step="0.01"></label>
<label><input id="s-unknown" type="checkbox"> Create events for unknown faces</label>
<label>Debounce seconds<input id="s-debounce" type="number" min="0"></label>
<button class="action-button" id="s-save">Save settings</button>
<p role="status" id="s-status"></p>
<div class="health-row"><span>Active face engine</span><strong id="s-engine">&mdash;</strong></div>
<p class="health-detail">The engine is a deployment-wide setting (ANYAICAM_FACE_ENGINE), shared by every customer/camera on this appliance -- shown here for information only, not editable per customer.</p>
</section>''')
        scripts = '''<script>
''' + fixed_customer_id_js + '''
function sGetCustomerId(){return FIXED_CUSTOMER_ID!==null?FIXED_CUSTOMER_ID:(document.getElementById('s-customer-id')?.value.trim()||'');}
function aacEsc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function sLoad(){
  const customerId=sGetCustomerId();
  if(!customerId)return;
  const response=await fetch('/api/aac/settings?customer_id='+customerId);
  if(!response.ok)return;
  const s=await response.json();
  document.getElementById('s-confidence').value=s.min_confidence;
  document.getElementById('s-unknown').checked=!!s.unknown_person_events_enabled;
  document.getElementById('s-debounce').value=s.debounce_seconds;
  document.getElementById('s-engine').textContent=aacEsc(s.engine)+' (v'+aacEsc(s.engine_version)+')';
}
document.getElementById('s-load').addEventListener('click',sLoad);
document.getElementById('s-save').addEventListener('click',async()=>{
  const customerId=sGetCustomerId();
  const response=await fetch('/api/aac/settings',{method:'PUT',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({customer_id:customerId,min_confidence:Number(document.getElementById('s-confidence').value),
    unknown_person_events_enabled:document.getElementById('s-unknown').checked,
    debounce_seconds:Number(document.getElementById('s-debounce').value)})});
  document.getElementById('s-status').textContent=response.ok?'Saved.':'Save failed.';
});
''' + auto_load_js + '''
</script>'''
        return shell("Facial Recognition · Settings", "aac", content, scripts)
