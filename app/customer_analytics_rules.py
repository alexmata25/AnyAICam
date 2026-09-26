"""Customer-facing Intrusion Zone / Line-Crossing rule editor (2026-09-21).

Tenant-safe replacement UI/API/storage foundation for a customer to draw and
manage their own intrusion zones and line-crossing lines, per camera. This is
a SEPARATE, additive store from the pre-existing admin-only rule builder
(main.py's ANALYTICS_RULES_FILE / analytics_rules.json, `/analytics/line-
crossing`, `/analytics/intrusion`) and from People Counting's own line -- see
docs/intrusion-line-crossing-customer-ui-gap.md for the full trace of why
that legacy file is not tenant-safe (no customer_id/site_id/appliance_id,
keyed only by a bare integer camera_number unique per appliance, not
per customer) and is therefore never read from or written to here.

Evaluation (2026-09-25): this module only stores rules; the edge
evaluates them. edge_camera_sync.py mirrors every rule onto the camera's
appliance, where customer_analytics_rule_worker.py evaluates "intrusion"
and "line_crossing" rules (for cameras entitled to Smart Motion, whose
person/vehicle detections they build on), and main.py's
people_counting_worker() uses a camera's "people_counting" line (for
cameras entitled to People Counting). A "people_counting" line is a
counting line, not an alert rule -- the rule worker never evaluates it,
and a camera has at most one.

Permission model (mirrors door_access.py's own can_unlock-gated write /
open read split): a customer_owner has implicit full-fleet read+write,
same as everywhere else in this codebase. A customer_viewer may read a
camera's rules if granted can_live OR can_playback for it (i.e. any
existing visibility into that camera at all), but may only create/update/
delete rules if additionally granted can_settings=1 for that camera --
reusing the existing customer_camera_permissions column rather than adding
a new one, since a saved rule is exactly the kind of per-camera
configuration can_settings already exists to gate.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from partner_db import audit, connection
from partner_portal import partner_identity

RULE_TYPES = ("intrusion", "line_crossing", "people_counting", "exclusion")
# Two-point line rules; "people_counting" is People Counting's counting
# line (at most one per camera), "line_crossing" an alert rule.
LINE_RULE_TYPES = ("line_crossing", "people_counting")
# Polygon rules: "intrusion" detects activity inside the zone; "exclusion"
# (2026-09-26) ignores it -- detections centred inside, and pixel motion
# inside, never become events on that camera (detection_exclusion.py).
ZONE_RULE_TYPES = ("intrusion", "exclusion")
LINE_CROSSING_DIRECTIONS = ("both", "inbound", "outbound")
MIN_POLYGON_POINTS = 3
MAX_POLYGON_POINTS = 20


def _customer_identity(request: Request) -> dict:
    identity = partner_identity(request)
    if not identity or identity.get('role') not in {'customer_owner', 'customer_viewer'}:
        raise HTTPException(status_code=403, detail='Customer account required.')
    return identity


def _camera_display_label(camera: dict) -> str:
    """Duplicated from live_view_page.py's own helper of the same name --
    same reasoning as that module's own docstring: main.py imports this
    module to register its routes, so importing back from main.py or
    cross-importing between page modules risks a circular import."""
    name = (camera.get('name') or '').strip()
    if name:
        return name
    number = camera.get('camera_number')
    return f'Camera {number}' if number is not None else str(camera.get('id', 'Camera'))


def _authorized_camera_for_rules(db, camera_id: str, identity: dict) -> tuple[dict, bool]:
    """Tenant-scoped camera lookup plus a single (camera, can_edit) result
    every route below shares, matching camera_access.is_camera_authorized()'s
    own fail-closed shape: raises 404 for a camera not owned by this
    identity's own customer_id (never distinguishable from "does not
    exist" -- this codebase's established no-oracle convention), raises 403
    for a customer_viewer with no visibility into it at all, and otherwise
    returns whether THIS identity may also create/update/delete rules for
    it (customer_owner: always; customer_viewer: only with can_settings=1).
    """
    camera = db.execute(
        'SELECT id,name,camera_number,customer_id,site_id,appliance_id FROM cameras WHERE id=? AND customer_id=?',
        (camera_id, identity['customer_id']),
    ).fetchone()
    if not camera:
        raise HTTPException(status_code=404, detail='Camera not found.')
    camera = dict(camera)
    if identity.get('role') == 'customer_owner':
        return camera, True

    user = db.execute('SELECT id FROM partner_users WHERE email=?', (identity['email'],)).fetchone()
    if not user:
        raise HTTPException(status_code=403, detail='Customer account required.')
    permission = db.execute(
        'SELECT can_live,can_playback,can_settings FROM customer_camera_permissions WHERE user_id=? AND camera_id=?',
        (user['id'], camera_id),
    ).fetchone()
    if not permission or not (permission['can_live'] or permission['can_playback']):
        raise HTTPException(status_code=403, detail='Not authorized for this camera.')
    return camera, bool(permission['can_settings'])


def _require_edit(can_edit: bool) -> None:
    if not can_edit:
        raise HTTPException(
            status_code=403,
            detail='Not authorized to edit rules for this camera -- ask the account owner to grant Camera Settings access.',
        )


def _validate_point(point) -> dict:
    if not isinstance(point, dict):
        raise HTTPException(status_code=400, detail='Each geometry point must be an object with x and y.')
    try:
        x = float(point['x'])
        y = float(point['y'])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail='Each geometry point must have numeric x and y.')
    if not (0.0 <= x <= 1.0) or not (0.0 <= y <= 1.0):
        raise HTTPException(status_code=400, detail='Geometry points must be normalized between 0 and 1.')
    return {'x': x, 'y': y}


def _validate_geometry(rule_type: str, geometry, direction):
    """Returns (normalized_geometry_list, normalized_direction). Fails
    closed on anything malformed rather than clamping/guessing -- a bad
    rule should never silently save as a different, unintended shape."""
    if rule_type not in RULE_TYPES:
        raise HTTPException(status_code=400, detail=f'rule_type must be one of {list(RULE_TYPES)}.')
    if not isinstance(geometry, list):
        raise HTTPException(status_code=400, detail='geometry must be a list of points.')
    points = [_validate_point(point) for point in geometry]
    if rule_type in LINE_RULE_TYPES:
        if len(points) != 2:
            raise HTTPException(status_code=400, detail='A line needs exactly 2 points.')
        if points[0] == points[1]:
            raise HTTPException(status_code=400, detail='A line needs 2 different points.')
        if direction not in LINE_CROSSING_DIRECTIONS:
            raise HTTPException(status_code=400, detail=f'direction must be one of {list(LINE_CROSSING_DIRECTIONS)} for a line.')
        return points, direction
    if len(points) < MIN_POLYGON_POINTS:
        raise HTTPException(status_code=400, detail=f'A zone needs at least {MIN_POLYGON_POINTS} points.')
    if len(points) > MAX_POLYGON_POINTS:
        raise HTTPException(status_code=400, detail=f'A zone supports at most {MAX_POLYGON_POINTS} points.')
    if direction not in (None, ''):
        raise HTTPException(status_code=400, detail='direction does not apply to a zone.')
    return points, None


def _fetch_rule(db, *, camera_id: str, rule_id: str, customer_id: str) -> dict:
    row = db.execute(
        'SELECT * FROM customer_analytics_rules WHERE id=? AND camera_id=? AND customer_id=?',
        (rule_id, camera_id, customer_id),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail='Rule not found.')
    return dict(row)


def _serialize_rule(row: dict) -> dict:
    return {
        'id': row['id'],
        'camera_id': row['camera_id'],
        'rule_type': row['rule_type'],
        'name': row['name'],
        'direction': row['direction'],
        'geometry': json.loads(row['geometry_json']),
        'enabled': bool(row['enabled']),
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
    }


def register_customer_analytics_rules_routes(app: FastAPI, page_shell: Callable) -> None:
    @app.get('/api/customer/cameras/{camera_id}/analytics-rules')
    def list_analytics_rules(request: Request, camera_id: str) -> dict:
        identity = _customer_identity(request)
        with connection() as db:
            camera, can_edit = _authorized_camera_for_rules(db, camera_id, identity)
            rows = db.execute(
                'SELECT * FROM customer_analytics_rules WHERE camera_id=? AND customer_id=? ORDER BY created_at',
                (camera_id, identity['customer_id']),
            ).fetchall()
        return {
            'camera_id': camera_id,
            'camera_name': _camera_display_label(camera),
            'can_edit': can_edit,
            'rules': [_serialize_rule(dict(row)) for row in rows],
        }

    @app.get('/api/customer/cameras/{camera_id}/analytics-rules/{rule_id}')
    def get_analytics_rule(request: Request, camera_id: str, rule_id: str) -> dict:
        identity = _customer_identity(request)
        with connection() as db:
            _authorized_camera_for_rules(db, camera_id, identity)
            rule = _fetch_rule(db, camera_id=camera_id, rule_id=rule_id, customer_id=identity['customer_id'])
        return _serialize_rule(rule)

    @app.post('/api/customer/cameras/{camera_id}/analytics-rules')
    def create_analytics_rule(request: Request, camera_id: str, payload: dict) -> dict:
        identity = _customer_identity(request)
        now = datetime.now().isoformat()
        rule_type = str(payload.get('rule_type') or '')
        name = str(payload.get('name') or '').strip()
        direction = payload.get('direction')
        geometry = payload.get('geometry')
        if not name:
            raise HTTPException(status_code=400, detail='name is required.')
        geometry_points, direction = _validate_geometry(rule_type, geometry, direction)
        with connection() as db:
            camera, can_edit = _authorized_camera_for_rules(db, camera_id, identity)
            _require_edit(can_edit)
            if rule_type == 'people_counting' and db.execute(
                "SELECT 1 FROM customer_analytics_rules WHERE camera_id=? AND customer_id=? AND rule_type='people_counting'",
                (camera_id, identity['customer_id']),
            ).fetchone():
                raise HTTPException(status_code=409, detail='This camera already has a people counting line. Edit it instead.')
            rule_id = uuid.uuid4().hex[:12]
            db.execute(
                'INSERT INTO customer_analytics_rules'
                '(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,direction,geometry_json,enabled,created_at,updated_at,created_by) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (
                    rule_id, identity['customer_id'], camera['site_id'], camera['appliance_id'], camera_id,
                    rule_type, name, direction, json.dumps(geometry_points),
                    1 if payload.get('enabled', True) else 0, now, now, identity['email'],
                ),
            )
            row = _fetch_rule(db, camera_id=camera_id, rule_id=rule_id, customer_id=identity['customer_id'])
        audit(identity, 'camera.analytics_rule_created', 'camera', camera_id, {'rule_id': rule_id, 'rule_type': rule_type})
        return _serialize_rule(row)

    @app.put('/api/customer/cameras/{camera_id}/analytics-rules/{rule_id}')
    def update_analytics_rule(request: Request, camera_id: str, rule_id: str, payload: dict) -> dict:
        identity = _customer_identity(request)
        now = datetime.now().isoformat()
        with connection() as db:
            camera, can_edit = _authorized_camera_for_rules(db, camera_id, identity)
            _require_edit(can_edit)
            existing = _fetch_rule(db, camera_id=camera_id, rule_id=rule_id, customer_id=identity['customer_id'])
            rule_type = existing['rule_type']  # immutable -- a different shape is a new rule, not an edit
            name = str(payload.get('name', existing['name']) or '').strip()
            if not name:
                raise HTTPException(status_code=400, detail='name is required.')
            direction = payload.get('direction', existing['direction'])
            geometry = payload.get('geometry')
            if geometry is None:
                geometry = json.loads(existing['geometry_json'])
            geometry_points, direction = _validate_geometry(rule_type, geometry, direction)
            enabled = payload.get('enabled', bool(existing['enabled']))
            db.execute(
                'UPDATE customer_analytics_rules SET name=?,direction=?,geometry_json=?,enabled=?,updated_at=? WHERE id=?',
                (name, direction, json.dumps(geometry_points), 1 if enabled else 0, now, rule_id),
            )
            row = _fetch_rule(db, camera_id=camera_id, rule_id=rule_id, customer_id=identity['customer_id'])
        audit(identity, 'camera.analytics_rule_updated', 'camera', camera_id, {'rule_id': rule_id})
        return _serialize_rule(row)

    @app.delete('/api/customer/cameras/{camera_id}/analytics-rules/{rule_id}')
    def delete_analytics_rule(request: Request, camera_id: str, rule_id: str) -> dict:
        identity = _customer_identity(request)
        with connection() as db:
            _camera, can_edit = _authorized_camera_for_rules(db, camera_id, identity)
            _require_edit(can_edit)
            _fetch_rule(db, camera_id=camera_id, rule_id=rule_id, customer_id=identity['customer_id'])
            db.execute('DELETE FROM customer_analytics_rules WHERE id=?', (rule_id,))
        audit(identity, 'camera.analytics_rule_deleted', 'camera', camera_id, {'rule_id': rule_id})
        return {'message': 'Rule deleted.'}

    @app.get('/customer/cameras/{camera_id}/analytics-rules', response_class=HTMLResponse)
    def analytics_rules_page(request: Request, camera_id: str):
        identity = partner_identity(request)
        if not identity or identity.get('role') not in {'customer_owner', 'customer_viewer'}:
            return RedirectResponse('/partner-login', status_code=303)
        with connection() as db:
            camera, can_edit = _authorized_camera_for_rules(db, camera_id, identity)
        camera_name = _camera_display_label(camera)
        start_url = f'/api/customer/cameras/{camera_id}/live/start'
        playlist_url = f'/api/customer/cameras/{camera_id}/live/playlist.m3u8'

        content = f'''
        <header class="topbar">
          <div><p class="eyebrow">Detection rules</p><h1>Lines, zones &amp; people counting &middot; {camera_name}</h1></div>
          <a class="ghost-button" href="/customer/cameras/{camera_id}/live">Back to live view</a>
        </header>
        <section class="panel">
          <div class="panel-head"><div><h2>Draw a rule</h2><div class="health-detail">
            Capture a frame from this camera, then draw a line to watch for crossings,
            a zone to watch for activity inside it, or a zone to ignore. Points are saved
            relative to the frame, so a rule still lines up if the camera's resolution ever changes.
          </div></div></div>
          {"" if can_edit else '<div class="health-detail" style="color:#b45309;margin-bottom:12px">You have view-only access to this camera. Ask the account owner to grant Camera Settings access to draw or edit rules.</div>'}
          <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:4px">
            <div>
              <div style="position:relative;width:640px;max-width:100%;background:#111;border-radius:8px;overflow:hidden">
                <video id="rule-video" muted playsinline style="width:100%;display:block"></video>
                <canvas id="rule-canvas" width="640" height="360" style="position:absolute;inset:0;width:100%;height:100%;cursor:crosshair"></canvas>
              </div>
              <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
                <button class="compact-button" id="capture-frame" type="button">Capture frame</button>
                <button class="compact-button" id="clear-drawing" type="button">Clear drawing</button>
              </div>
              <p class="health-detail" id="draw-hint" style="margin-top:8px">Capture a frame, pick a rule type, then click on the image to place points. A line needs 2 points; a zone needs at least 3. Zones to ignore are drawn in red.</p>
            </div>
            <div style="flex:1;min-width:260px;display:grid;gap:12px;align-content:start">
              <label style="display:grid;gap:6px">Rule type
                <select id="rule-type" {"disabled" if not can_edit else ""}>
                  <option value="intrusion">Detect inside zone</option>
                  <option value="line_crossing">Line crossing</option>
                  <option value="exclusion">Ignore detections in zone</option>
                  <option value="people_counting">People counting line</option>
                </select>
              </label>
              <label style="display:grid;gap:6px" id="direction-field">Direction
                <select id="rule-direction" {"disabled" if not can_edit else ""}>
                  <option value="both">Either direction</option>
                  <option value="inbound">Inbound only</option>
                  <option value="outbound">Outbound only</option>
                </select>
              </label>
              <label style="display:grid;gap:6px">Name
                <input id="rule-name" type="text" maxlength="60" placeholder="e.g. Driveway entrance" {"disabled" if not can_edit else ""}>
              </label>
              <label><span><input id="rule-enabled" type="checkbox" checked {"disabled" if not can_edit else ""}> Enabled</span></label>
              <div style="display:flex;gap:8px">
                <button class="action-button" id="save-rule" type="button" {"disabled" if not can_edit else ""}>Save rule</button>
                <button class="ghost-button" id="cancel-edit" type="button" style="display:none">Cancel edit</button>
              </div>
            </div>
          </div>
        </section>
        <section class="panel" style="margin-top:16px">
          <div class="panel-head"><div><h2>Saved rules</h2></div></div>
          <div id="rules-list" class="health-detail">Loading&hellip;</div>
        </section>
        <section class="panel" style="margin-top:16px">
          <div class="panel-head"><div><h2>What this does today</h2></div></div>
          <p class="health-detail">
            Saved rules are sent to this camera's appliance and checked there.
            A line crossing or activity inside a detection zone creates an event
            (and an alert, if you have alerts turned on) on cameras with Smart
            Motion. A people counting line counts people walking across it in
            each direction on cameras with People Counting; each camera has one
            counting line. Anything centred inside a zone to ignore &mdash; people,
            vehicles, motion &mdash; never becomes an event, alert, count or clip on
            this camera, for every analytic; the rest of the picture is unaffected.
            Changes take effect within a few minutes.
          </p>
        </section>
        '''

        scripts = ('<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script><script>(function(){'
                   'const cameraId=' + json.dumps(camera_id) + ';'
                   'const canEdit=' + json.dumps(can_edit) + ';'
                   'const startUrl=' + json.dumps(start_url) + ';'
                   'const playlistUrl=' + json.dumps(playlist_url) + ';' + '''
  const video=document.getElementById('rule-video');
  const canvas=document.getElementById('rule-canvas');
  const ctx=canvas.getContext('2d');
  const ruleType=document.getElementById('rule-type');
  const isLine=t=>t==='line_crossing'||t==='people_counting';
  const isZone=t=>t==='intrusion'||t==='exclusion';
  const typeLabels={line_crossing:'Line crossing',intrusion:'Detect inside zone',exclusion:'Ignore detections in zone',people_counting:'People counting line'};
  const directionField=document.getElementById('direction-field');
  const bgCanvas=document.createElement('canvas');
  let hasFrame=false, points=[], editingRuleId=null, sessionId=null, hls=null, pollTimer=null, stopped=false;

  function drawBackground(){
    if(hasFrame){ctx.drawImage(bgCanvas,0,0,canvas.width,canvas.height);}
    else{
      ctx.fillStyle='#111';ctx.fillRect(0,0,canvas.width,canvas.height);
      ctx.fillStyle='#888';ctx.font='14px sans-serif';
      ctx.fillText('Capture a frame to begin drawing',16,canvas.height/2);
    }
  }

  function redraw(){
    drawBackground();
    if(!points.length)return;
    const color=ruleType.value==='exclusion'?'#ef4444':'#22c55e';
    ctx.strokeStyle=color;ctx.fillStyle=color;ctx.lineWidth=2;
    ctx.beginPath();
    points.forEach((p,i)=>{
      const x=p.x*canvas.width, y=p.y*canvas.height;
      if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);
    });
    if(isZone(ruleType.value)&&points.length>2){
      ctx.closePath();
      if(ruleType.value==='exclusion'){ctx.save();ctx.globalAlpha=0.25;ctx.fill();ctx.restore();}
    }
    ctx.stroke();
    points.forEach(p=>{
      const x=p.x*canvas.width, y=p.y*canvas.height;
      ctx.beginPath();ctx.arc(x,y,4,0,Math.PI*2);ctx.fill();
    });
  }

  function resetForm(){
    editingRuleId=null;
    points=[];
    document.getElementById('rule-name').value='';
    document.getElementById('rule-enabled').checked=true;
    document.getElementById('cancel-edit').style.display='none';
    redraw();
  }

  function toggleDirectionField(){
    directionField.style.display=isLine(ruleType.value)?'':'none';
  }
  toggleDirectionField();
  ruleType.addEventListener('change',()=>{toggleDirectionField();points=[];redraw();});

  document.getElementById('capture-frame').addEventListener('click',()=>{
    if(!video.videoWidth){alert('Live preview is not ready yet.');return;}
    bgCanvas.width=video.videoWidth;bgCanvas.height=video.videoHeight;
    bgCanvas.getContext('2d').drawImage(video,0,0);
    canvas.width=bgCanvas.width;canvas.height=bgCanvas.height;
    hasFrame=true;
    redraw();
  });
  document.getElementById('clear-drawing').addEventListener('click',()=>{points=[];redraw();});

  if(canEdit){
    canvas.addEventListener('click',(e)=>{
      if(!hasFrame)return;
      const rect=canvas.getBoundingClientRect();
      const x=(e.clientX-rect.left)/rect.width;
      const y=(e.clientY-rect.top)/rect.height;
      const cap=isLine(ruleType.value)?2:''' + str(MAX_POLYGON_POINTS) + ''';
      if(points.length>=cap){
        if(isLine(ruleType.value))points=[{x:x,y:y}];
        else return;
      }else{
        points.push({x:x,y:y});
      }
      redraw();
    });
  }

  document.getElementById('cancel-edit').addEventListener('click',resetForm);

  document.getElementById('save-rule').addEventListener('click',async()=>{
    if(!canEdit)return;
    const name=document.getElementById('rule-name').value.trim();
    if(!name){alert('Name is required.');return;}
    const type=ruleType.value;
    if(isLine(type)&&points.length!==2){alert('Draw exactly 2 points for a line.');return;}
    if(isZone(type)&&points.length<''' + str(MIN_POLYGON_POINTS) + '''){alert('Draw at least ''' + str(MIN_POLYGON_POINTS) + ''' points for a zone.');return;}
    const payload={
      rule_type:type,
      name:name,
      direction:isLine(type)?document.getElementById('rule-direction').value:null,
      geometry:points,
      enabled:document.getElementById('rule-enabled').checked,
    };
    const url=editingRuleId
      ?`/api/customer/cameras/${cameraId}/analytics-rules/${editingRuleId}`
      :`/api/customer/cameras/${cameraId}/analytics-rules`;
    const method=editingRuleId?'PUT':'POST';
    try{
      const response=await fetch(url,{method:method,headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      if(!response.ok){const err=await response.json().catch(()=>({}));alert(err.detail||'Could not save this rule.');return;}
      resetForm();
      loadRules();
    }catch(e){alert('Could not save this rule.');}
  });

  function escapeHtml(text){
    const div=document.createElement('div');
    div.textContent=text==null?'':text;
    return div.innerHTML;
  }

  async function loadRules(){
    const list=document.getElementById('rules-list');
    let body;
    try{
      const response=await fetch(`/api/customer/cameras/${cameraId}/analytics-rules`);
      if(!response.ok){list.textContent='Could not load rules.';return;}
      body=await response.json();
    }catch(e){list.textContent='Could not load rules.';return;}
    if(!body.rules.length){list.textContent='No rules yet.';return;}
    list.innerHTML=body.rules.map(rule=>{
      const typeLabel=typeLabels[rule.rule_type]||'Rule';
      const directionLabel=rule.direction?' &middot; '+escapeHtml(rule.direction):'';
      const stateLabel=rule.enabled?'Enabled':'Disabled';
      const actions=canEdit?(
        '<button class="compact-button" data-edit="'+rule.id+'" type="button">Edit</button>'+
        '<button class="compact-button" data-toggle="'+rule.id+'" data-enabled="'+rule.enabled+'" type="button">'+(rule.enabled?'Disable':'Enable')+'</button>'+
        '<button class="compact-button" data-delete="'+rule.id+'" type="button">Delete</button>'
      ):'';
      return '<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;padding:10px 0;border-top:1px solid var(--border,#e5e7eb)">'+
        '<div><strong>'+escapeHtml(rule.name)+'</strong>'+
        '<div style="color:var(--muted)">'+typeLabel+directionLabel+' &middot; '+stateLabel+'</div></div>'+
        '<div style="display:flex;gap:8px;flex-shrink:0">'+actions+'</div></div>';
    }).join('');
    [...list.querySelectorAll('[data-edit]')].forEach(btn=>btn.addEventListener('click',()=>editRule(btn.dataset.edit,body.rules)));
    [...list.querySelectorAll('[data-toggle]')].forEach(btn=>btn.addEventListener('click',()=>toggleRule(btn.dataset.toggle,btn.dataset.enabled==='true')));
    [...list.querySelectorAll('[data-delete]')].forEach(btn=>btn.addEventListener('click',()=>deleteRule(btn.dataset.delete)));
  }

  function editRule(ruleId,rules){
    const rule=rules.find(r=>r.id===ruleId);
    if(!rule)return;
    editingRuleId=ruleId;
    ruleType.value=rule.rule_type;
    toggleDirectionField();
    if(rule.direction)document.getElementById('rule-direction').value=rule.direction;
    document.getElementById('rule-name').value=rule.name;
    document.getElementById('rule-enabled').checked=rule.enabled;
    points=rule.geometry.slice();
    document.getElementById('cancel-edit').style.display='';
    redraw();
  }

  async function toggleRule(ruleId,currentlyEnabled){
    try{
      await fetch(`/api/customer/cameras/${cameraId}/analytics-rules/${ruleId}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!currentlyEnabled})});
      loadRules();
    }catch(e){}
  }

  async function deleteRule(ruleId){
    if(!confirm('Delete this rule?'))return;
    try{
      await fetch(`/api/customer/cameras/${cameraId}/analytics-rules/${ruleId}`,{method:'DELETE'});
      if(editingRuleId===ruleId)resetForm();
      loadRules();
    }catch(e){}
  }

  function stopPolling(){if(pollTimer){clearTimeout(pollTimer);pollTimer=null}}
  function destroyHls(){if(hls){try{hls.destroy()}catch(e){}hls=null}}

  function attachPlayer(){
    stopPolling();
    destroyHls();
    if(window.Hls&&Hls.isSupported()){
      hls=new Hls();
      hls.loadSource(playlistUrl);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED,()=>{video.play().catch(()=>{})});
    }else if(video.canPlayType('application/vnd.apple.mpegurl')){
      video.src=playlistUrl;
      video.addEventListener('loadedmetadata',()=>{video.play().catch(()=>{})});
    }
  }

  async function pollPlaylist(deadline){
    if(stopped)return;
    if(Date.now()>deadline)return;
    let response=null;
    try{response=await fetch(playlistUrl,{cache:'no-store'})}catch(e){}
    if(response&&response.ok){
      const text=await response.text();
      if(text.includes('#EXTINF')){attachPlayer();return}
    }else if(response&&[403,404,409,503].includes(response.status)){
      return;
    }
    pollTimer=setTimeout(()=>pollPlaylist(deadline),2000);
  }

  async function startPreview(){
    let response;
    try{response=await fetch(startUrl,{method:'POST'})}catch(e){return}
    if(!response.ok)return;
    const body=await response.json();
    sessionId=body.session_id;
    pollPlaylist(Date.now()+45000);
  }

  window.addEventListener('beforeunload',()=>{
    if(sessionId){
      try{fetch(`/api/customer/live/sessions/${sessionId}/stop`,{method:'POST',keepalive:true})}catch(e){}
    }
  });

  redraw();
  loadRules();
  startPreview();
})();</script>''')

        return page_shell(f'Detection rules · {camera_name}', 'live', content, scripts)
