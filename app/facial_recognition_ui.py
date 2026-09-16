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
import binascii
import html
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
    return _require_safe_path_segment(requested, field="customer_id")


def _require(request: Request, permission: str) -> dict:
    from partner_portal import partner_identity

    identity = partner_identity(request)
    if not identity:
        raise HTTPException(status_code=401, detail="Sign in is required.")
    if not allowed(identity, permission):
        raise HTTPException(status_code=403, detail="You do not have permission for this action.")
    return identity


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


def register_facial_recognition_routes(app: FastAPI, shell: Callable) -> None:
    # ---------------------------------------------------------------- API

    @app.get("/api/aac/capability")
    def aac_capability(request: Request) -> dict:
        _require(request, "facial.view")
        return facial_recognition.capability()

    @app.get("/api/aac/people")
    def aac_list_people(request: Request, customer_id: str | None = None, site_id: str | None = None, status: str | None = None, search: str | None = None) -> dict:
        identity = _require(request, "facial.view")
        resolved = _resolve_customer_id(identity, customer_id)
        with connection() as db:
            people = facial_people.list_people(db, customer_id=resolved, site_id=site_id, status=status, search=search)
        return {"people": people}

    @app.post("/api/aac/people")
    def aac_enroll_person(request: Request, payload: dict) -> dict:
        identity = _require(request, "facial.manage")
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
        identity = _require(request, "facial.view")
        resolved = _resolve_customer_id(identity, customer_id)
        with connection() as db:
            person = facial_people.get_person(db, customer_id=resolved, person_id=person_id)
            if person is None:
                raise HTTPException(status_code=404, detail="Person not found.")
            person["reference_images"] = facial_people.list_reference_images(db, customer_id=resolved, person_id=person_id)
            person["watchlists"] = facial_people.person_watchlist_memberships(db, customer_id=resolved, person_id=person_id)
        return person

    @app.patch("/api/aac/people/{person_id}")
    def aac_update_person(request: Request, person_id: str, payload: dict) -> dict:
        identity = _require(request, "facial.manage")
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
        return {"status": "deleted"}

    @app.post("/api/aac/people/{person_id}/images")
    def aac_add_reference_image(request: Request, person_id: str, payload: dict) -> dict:
        identity = _require(request, "facial.manage")
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
        resolved = _resolve_customer_id(identity, customer_id)
        with connection() as db:
            watchlists = facial_people.list_watchlists(db, customer_id=resolved)
        return {"watchlists": watchlists}

    @app.post("/api/aac/watchlists")
    def aac_create_watchlist(request: Request, payload: dict) -> dict:
        identity = _require(request, "facial.manage")
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
        resolved = _resolve_customer_id(identity, customer_id)
        with connection() as db:
            members = facial_people.list_watchlist_members(db, customer_id=resolved, watchlist_id=watchlist_id)
        return {"members": members}

    @app.post("/api/aac/watchlists/{watchlist_id}/members")
    def aac_add_watchlist_member(request: Request, watchlist_id: str, payload: dict) -> dict:
        identity = _require(request, "facial.manage")
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
        resolved = _resolve_customer_id(identity, customer_id)
        with connection() as db:
            event = facial_events.get_event_detail(db, customer_id=resolved, event_id=event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        return event

    @app.get("/api/aac/settings")
    def aac_get_settings(request: Request, customer_id: str | None = None) -> dict:
        identity = _require(request, "facial.view")
        resolved = _resolve_customer_id(identity, customer_id)
        with connection() as db:
            return facial_people.get_settings(db, customer_id=resolved)

    @app.put("/api/aac/settings")
    def aac_update_settings(request: Request, payload: dict) -> dict:
        identity = _require(request, "facial.manage")
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

    def _page(request: Request, title: str, content: str, scripts: str = "") -> str:
        _require(request, "facial.view")
        return shell(title, "aac", content, scripts)

    @app.get("/aac/people", response_class=HTMLResponse)
    def aac_people_page(request: Request):
        content = '''<header class="topbar"><div><p class="eyebrow">AAC</p><h1>People</h1></div>
<a class="action-button" href="/aac/people/enroll">Enroll person</a></header>
<section class="panel"><label>Customer ID<input id="aac-customer-id" placeholder="cust-..."></label>
<label>Search<input id="aac-search"></label><button class="ghost-button" id="aac-refresh">Refresh</button></section>
<section class="panel" id="aac-people-list"><p class="health-detail">Enter a customer id and refresh.</p></section>'''
        scripts = '''<script>
function aacEsc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function aacLoadPeople(){
  const customerId=document.getElementById('aac-customer-id').value.trim();
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
  box.innerHTML='<table class="data-table"><thead><tr><th>Name</th><th>Reference</th><th>Status</th><th></th></tr></thead><tbody>'+
    data.people.map(p=>`<tr><td><a href="/aac/people/enroll?person_id=${encodeURIComponent(p.id)}&customer_id=${encodeURIComponent(customerId)}">${aacEsc(p.display_name)}</a></td><td>${aacEsc(p.external_reference||'')}</td><td>${aacEsc(p.status)}</td>
    <td><button class="ghost-button" onclick="aacDeletePerson('${p.id}','${customerId}')">Delete</button></td></tr>`).join('')+'</tbody></table>';
}
async function aacDeletePerson(personId,customerId){
  if(!confirm('Delete this person and all biometric templates? This cannot be undone.'))return;
  await fetch(`/api/aac/people/${personId}?customer_id=${customerId}`,{method:'DELETE'});
  aacLoadPeople();
}
document.getElementById('aac-refresh').addEventListener('click',aacLoadPeople);
</script>'''
        return _page(request, "AAC People", content, scripts)

    @app.get("/aac/people/enroll", response_class=HTMLResponse)
    def aac_enroll_page(request: Request):
        content = '''<header class="topbar"><div><p class="eyebrow">AAC</p><h1>Enroll person</h1></div></header>
<section class="panel rule-form" style="max-width:640px">
<label>Customer ID<input id="e-customer-id" placeholder="cust-..."></label>
<label>Display name<input id="e-name" required></label>
<label>Employee/customer reference<input id="e-reference"></label>
<label>Notes<textarea id="e-notes"></textarea></label>
<button class="action-button" id="e-create">Create person</button>
<p role="status" id="e-status"></p>
<div id="e-images" style="display:none">
<h2>Reference images</h2>
<label>Upload a clear, front-facing photo<input id="e-file" type="file" accept="image/*"></label>
<button class="action-button" id="e-upload">Add reference image</button>
<p class="health-detail">Only the detected face crop is stored -- the original photo is never saved.</p>
<div id="e-image-list"></div>
</div>
</section>'''
        scripts = '''<script>
let currentPersonId=null;
document.getElementById('e-create').addEventListener('click',async()=>{
  const customerId=document.getElementById('e-customer-id').value.trim();
  const response=await fetch('/api/aac/people',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({customer_id:customerId,display_name:document.getElementById('e-name').value,
    external_reference:document.getElementById('e-reference').value,notes:document.getElementById('e-notes').value})});
  const result=await response.json();
  if(!response.ok){document.getElementById('e-status').textContent=result.detail;return;}
  currentPersonId=result.person_id;
  document.getElementById('e-status').textContent='Person created. Add at least one reference image.';
  document.getElementById('e-images').style.display='block';
});
document.getElementById('e-upload').addEventListener('click',async()=>{
  const file=document.getElementById('e-file').files[0];
  const customerId=document.getElementById('e-customer-id').value.trim();
  if(!file||!currentPersonId)return;
  const reader=new FileReader();
  reader.onload=async()=>{
    const response=await fetch(`/api/aac/people/${currentPersonId}/images`,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({customer_id:customerId,image_base64:reader.result})});
    const result=await response.json();
    document.getElementById('e-status').textContent=response.ok?`Reference image added (quality ${result.quality}).`:result.detail;
  };
  reader.readAsDataURL(file);
});
</script>'''
        return _page(request, "Enroll person", content, scripts)

    @app.get("/aac/watchlists", response_class=HTMLResponse)
    def aac_watchlists_page(request: Request):
        content = '''<header class="topbar"><div><p class="eyebrow">AAC</p><h1>Watchlists</h1></div></header>
<section class="panel rule-form"><label>Customer ID<input id="w-customer-id"></label>
<label>New watchlist name<input id="w-name"></label>
<label>Classification<select id="w-classification"><option value="alert">Alert</option><option value="allow">Allow (access rules)</option></select></label>
<button class="action-button" id="w-create">Create watchlist</button></section>
<section class="panel" id="w-list"></section>'''
        scripts = '''<script>
function aacEsc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function wLoad(){
  const customerId=document.getElementById('w-customer-id').value.trim();
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
  const customerId=document.getElementById('w-customer-id').value.trim();
  await fetch('/api/aac/watchlists',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({customer_id:customerId,name:document.getElementById('w-name').value,classification:document.getElementById('w-classification').value})});
  wLoad();
});
document.getElementById('w-customer-id').addEventListener('change',wLoad);
</script>'''
        return _page(request, "AAC Watchlists", content, scripts)

    @app.get("/aac/events", response_class=HTMLResponse)
    def aac_events_page(request: Request):
        content = '''<header class="topbar"><div><p class="eyebrow">AAC</p><h1>Facial events</h1></div></header>
<section class="panel"><label>Customer ID<input id="ev-customer-id"></label>
<label>Match state<select id="ev-state"><option value="">All</option><option value="known">Known</option><option value="unknown">Unknown</option><option value="watchlist">Watchlist</option></select></label>
<button class="ghost-button" id="ev-refresh">Refresh</button></section>
<section class="panel" id="ev-list"></section>'''
        scripts = '''<script>
function aacEsc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
document.getElementById('ev-refresh').addEventListener('click',async()=>{
  const customerId=document.getElementById('ev-customer-id').value.trim();
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
        return _page(request, "AAC Facial events", content, scripts)

    @app.get("/api/aac/events/{event_id}/thumbnail")
    def aac_event_thumbnail(request: Request, event_id: str, customer_id: str | None = None):
        identity = _require(request, "facial.view")
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
        # event_id is a raw URL path segment -- HTML-escaped here (server
        # side, before it ever reaches the response body) so a crafted
        # URL cannot break out of the data-event-id attribute.
        content = f'''<header class="topbar"><div><p class="eyebrow">AAC</p><h1>Match detail</h1></div></header>
<section class="panel" id="d-detail" data-event-id="{html.escape(event_id)}"></section>'''
        scripts = '''<script>
function aacEsc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
(async()=>{
  const box=document.getElementById('d-detail');
  const eventId=box.dataset.eventId;
  const customerId=new URLSearchParams(location.search).get('customer_id')||'';
  const response=await fetch(`/api/aac/events/${encodeURIComponent(eventId)}?customer_id=${encodeURIComponent(customerId)}`);
  if(!response.ok){box.innerHTML='<p class="health-detail">Event not found.</p>';return;}
  const e=await response.json();
  // matched_person_name/matched_watchlist_name are denormalized snapshots
  // of free text a 'facial.manage' user chose -- escaped before reaching
  // innerHTML (see aac_people_page's own comment on this). The thumbnail
  // <img> below is intentionally allowed to fail silently (onerror hides
  // it) -- not every event has one yet (e.g. no face crop could be saved).
  box.innerHTML=`<img src="/api/aac/events/${encodeURIComponent(eventId)}/thumbnail?customer_id=${encodeURIComponent(customerId)}" alt="Face thumbnail" style="max-width:200px;border-radius:8px;display:block;margin-bottom:12px" onerror="this.style.display='none'">
  <div class="health-row"><span>Time</span><strong>${aacEsc(e.event_timestamp)}</strong></div>
  <div class="health-row"><span>Camera</span><strong>${aacEsc(e.camera_id)}</strong></div>
  <div class="health-row"><span>State</span><strong>${aacEsc(e.match_state)}</strong></div>
  <div class="health-row"><span>Matched person</span><strong>${aacEsc(e.matched_person_name||'—')}</strong></div>
  <div class="health-row"><span>Watchlist</span><strong>${aacEsc(e.matched_watchlist_name||'—')}</strong></div>
  <div class="health-row"><span>Confidence</span><strong>${aacEsc(e.confidence)}</strong></div>
  <div class="health-row"><span>Engine</span><strong>${aacEsc(e.engine)}</strong></div>`;
})();
</script>'''
        return _page(request, "AAC Match detail", content, scripts)

    @app.get("/aac/settings", response_class=HTMLResponse)
    def aac_settings_page(request: Request):
        content = '''<header class="topbar"><div><p class="eyebrow">AAC</p><h1>Settings</h1></div></header>
<section class="panel rule-form" style="max-width:520px">
<label>Customer ID<input id="s-customer-id"></label>
<button class="ghost-button" id="s-load">Load</button>
<label>Minimum confidence (0-1)<input id="s-confidence" type="number" min="0" max="1" step="0.01"></label>
<label><input id="s-unknown" type="checkbox"> Create events for unknown faces</label>
<label>Debounce seconds<input id="s-debounce" type="number" min="0"></label>
<button class="action-button" id="s-save">Save settings</button>
<p role="status" id="s-status"></p>
<div class="health-row"><span>Active face engine</span><strong id="s-engine">&mdash;</strong></div>
<p class="health-detail">The engine is a deployment-wide setting (ANYAICAM_FACE_ENGINE), shared by every customer/camera on this appliance -- shown here for information only, not editable per customer.</p>
</section>'''
        scripts = '''<script>
function aacEsc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function sLoad(){
  const customerId=document.getElementById('s-customer-id').value.trim();
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
  const customerId=document.getElementById('s-customer-id').value.trim();
  const response=await fetch('/api/aac/settings',{method:'PUT',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({customer_id:customerId,min_confidence:Number(document.getElementById('s-confidence').value),
    unknown_person_events_enabled:document.getElementById('s-unknown').checked,
    debounce_seconds:Number(document.getElementById('s-debounce').value)})});
  document.getElementById('s-status').textContent=response.ok?'Saved.':'Save failed.';
});
</script>'''
        return _page(request, "AAC Settings", content, scripts)
