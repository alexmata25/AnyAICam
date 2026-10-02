"""Customer portal for Arm Stay / Arm Away / Disarm and Security Settings
(2026-09-28).

The cloud side of security_modes: the customer changes the site's mode
or its security settings here, every change is audited, and the new
state reaches the site's appliance in the next configuration sync
(appliance_cloud._security_config -> edge_camera_sync._reconcile_security).
The appliance decides per detection from its local copy; nothing here
ever drives hardware directly.

Owner-only for changes (arming/disarming and settings are account
security decisions); a customer_viewer can see the current mode.
"""
from __future__ import annotations

import html

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import security_modes
from partner_db import audit, connection
from partner_portal import partner_identity


def _customer_identity(request: Request) -> dict:
    identity = partner_identity(request)
    if not identity or identity.get('role') not in {'customer_owner', 'customer_viewer'}:
        raise HTTPException(status_code=403, detail='Customer account required.')
    return identity


def _require_owner(identity: dict) -> None:
    if identity.get('role') != 'customer_owner':
        raise HTTPException(status_code=403, detail='Only the account owner can change security settings.')


def _customer_site(db, customer_id: str, site_id: str | None) -> dict:
    """The named site, or the customer's only/first site when none is named.
    Always scoped to this customer."""
    if site_id:
        site = db.execute('SELECT id,name FROM sites WHERE id=? AND customer_id=?', (site_id, customer_id)).fetchone()
    else:
        site = db.execute('SELECT id,name FROM sites WHERE customer_id=? ORDER BY created_at,id LIMIT 1', (customer_id,)).fetchone()
    if not site:
        raise HTTPException(status_code=404, detail='Site not found.')
    return dict(site)


def _site_cameras(db, customer_id: str, site_id: str) -> list[dict]:
    """This site's installed cameras only. Purchased-but-undiscovered slot
    placeholders (camera rows with no device yet, e.g. "Camera 6") are left
    out -- they can't detect anything, so they don't belong in the armed
    camera list -- using the same installed test as /customer-account
    (camera_install_state.camera_is_installed). The placeholder rows
    themselves are never touched."""
    from camera_install_state import camera_is_installed
    rows = db.execute(
        'SELECT c.id,c.name,c.camera_number,c.status,c.device_key,'
        'MAX(COALESCE(acs.online,0)) AS online,MAX(COALESCE(acs.recording,0)) AS recording,'
        'EXISTS(SELECT 1 FROM recordings r WHERE r.camera_id=c.id) AS has_recording '
        'FROM cameras c LEFT JOIN appliance_camera_status acs ON acs.camera_id=c.id '
        'WHERE c.customer_id=? AND c.site_id=? GROUP BY c.id ORDER BY c.camera_number,c.name',
        (customer_id, site_id),
    ).fetchall()
    return [
        {'id': item['id'], 'name': item['name'], 'camera_number': item['camera_number']}
        for item in rows
        if camera_is_installed(
            camera_status=item['status'], device_key=item['device_key'],
            appliance_reported_online=bool(item['online']), appliance_reported_recording=bool(item['recording']),
            has_cloud_recording=bool(item['has_recording']),
        )
    ]


def security_overview(db, identity: dict, site_id: str | None = None) -> dict:
    site = _customer_site(db, identity['customer_id'], site_id)
    state = security_modes.get_state(db, identity['customer_id'], site['id'])
    result = {
        'site': site,
        'mode': state['mode'],
        'mode_label': security_modes.MODE_LABELS[state['mode']],
        'changed_by': state['changed_by'],
        'changed_at': state['changed_at'],
        'can_change': identity.get('role') == 'customer_owner',
    }
    if result['can_change']:
        cameras = _site_cameras(db, identity['customer_id'], site['id'])
        result['settings'] = state['settings']
        result['cameras'] = [
            dict(camera,
                 armed_in_stay=security_modes.camera_is_armed('stay', camera['id'], state['settings']),
                 armed_in_away=security_modes.camera_is_armed('away', camera['id'], state['settings']))
            for camera in cameras
        ]
    return result


def register_security_routes(app: FastAPI, page_shell) -> None:
    @app.get('/api/customer/security')
    def get_security(request: Request, site_id: str | None = None) -> dict:
        identity = _customer_identity(request)
        with connection() as db:
            return security_overview(db, identity, site_id)

    @app.post('/api/customer/security/mode')
    def set_security_mode(request: Request, payload: dict) -> dict:
        identity = _customer_identity(request)
        _require_owner(identity)
        try:
            mode = security_modes.normalize_mode((payload or {}).get('mode'))
        except ValueError:
            raise HTTPException(status_code=400, detail='mode must be disarmed, stay or away.')
        with connection() as db:
            site = _customer_site(db, identity['customer_id'], (payload or {}).get('site_id'))
            previous = security_modes.get_state(db, identity['customer_id'], site['id'])['mode']
            security_modes.set_mode(db, identity['customer_id'], site['id'], mode, actor=identity['email'])
        audit(identity, 'security.mode_changed', 'site', site['id'], {'from': previous, 'to': mode})
        with connection() as db:
            return security_overview(db, identity, site['id'])

    @app.put('/api/customer/security/settings')
    def put_security_settings(request: Request, payload: dict) -> dict:
        identity = _customer_identity(request)
        _require_owner(identity)
        payload = payload or {}
        settings = payload.get('settings')
        if not isinstance(settings, dict):
            raise HTTPException(status_code=400, detail='settings must be an object.')
        with connection() as db:
            site = _customer_site(db, identity['customer_id'], payload.get('site_id'))
            valid = {camera['id'] for camera in _site_cameras(db, identity['customer_id'], site['id'])}
            security_modes.save_settings(db, identity['customer_id'], site['id'], settings,
                                         actor=identity['email'], valid_camera_ids=valid)
        audit(identity, 'security.settings_changed', 'site', site['id'], {'keys': sorted(k for k in settings if k in security_modes.DEFAULT_SETTINGS)})
        with connection() as db:
            return security_overview(db, identity, site['id'])

    @app.get('/customer-security', response_class=HTMLResponse)
    def security_page(request: Request):
        identity = partner_identity(request)
        if not identity or identity.get('role') not in {'customer_owner', 'customer_viewer'}:
            return RedirectResponse('/partner-login', status_code=303)
        with connection() as db:
            overview = security_overview(db, identity)
        return page_shell('Security', 'security', security_page_content(overview), SECURITY_PAGE_SCRIPT)


MODE_BUTTONS = (('stay', 'Arm Stay'), ('away', 'Arm Away'), ('disarmed', 'Disarm'))


def security_mode_control(mode: str, can_change: bool) -> str:
    """The three-button Arm Stay / Arm Away / Disarm control, shared by the
    Security page and the Dashboard. Buttons post through SECURITY_CONTROL_SCRIPT
    (data-security-mode)."""
    buttons = ''.join(
        f'<button type="button" class="sec-mode{" active" if mode == value else ""}" data-security-mode="{value}"'
        f'{"" if can_change else " disabled"} aria-pressed="{str(mode == value).lower()}">{label}</button>'
        for value, label in MODE_BUTTONS
    )
    return (
        f'<div class="sec-control" data-security-control data-mode="{html.escape(mode)}">'
        f'<div class="sec-status sec-{html.escape(mode)}" data-security-status>{html.escape(security_modes.MODE_LABELS.get(mode, mode))}</div>'
        f'<div class="sec-buttons">{buttons}</div>'
        f'<div class="health-detail" data-security-message></div></div>'
    )


SECURITY_CONTROL_CSS = '''
<style>
.sec-control{display:flex;flex-wrap:wrap;align-items:center;gap:12px}
.sec-status{font-weight:700;padding:6px 12px;border-radius:999px;background:#1f2937;color:#e5e7eb}
.sec-status.sec-stay,.sec-status.sec-away{background:#b42318;color:#fff}
.sec-buttons{display:flex;gap:8px;flex-wrap:wrap}
.sec-mode{min-height:44px;padding:8px 16px;border-radius:10px;border:1px solid #475467;background:transparent;color:inherit;font-weight:600;cursor:pointer}
.sec-mode.active{background:#2dd4bf;color:#0b1220;border-color:#2dd4bf}
.sec-mode[data-security-mode="disarmed"].active{background:#e5e7eb;border-color:#e5e7eb}
.sec-mode:disabled{opacity:.55;cursor:not-allowed}
.sec-field{box-sizing:border-box;min-height:40px;padding:8px 11px;border:1px solid rgba(170,196,207,.3);border-radius:9px;background:#111827;color:#fff;font:inherit;font-size:15px}
.sec-cams{width:100%;border-collapse:collapse}.sec-cams td,.sec-cams th{padding:8px;border-bottom:1px solid #334155;text-align:left}
</style>
'''

SECURITY_CONTROL_SCRIPT = '''
<script>
(function(){
  const LABELS={disarmed:'Disarmed',stay:'Armed Stay',away:'Armed Away'};
  function paint(ctrl,mode){
    ctrl.dataset.mode=mode;
    const status=ctrl.querySelector('[data-security-status]');
    status.textContent=LABELS[mode]||mode; status.className='sec-status sec-'+mode;
    ctrl.querySelectorAll('[data-security-mode]').forEach(b=>{const on=b.dataset.securityMode===mode;b.classList.toggle('active',on);b.setAttribute('aria-pressed',String(on));});
  }
  document.querySelectorAll('[data-security-control]').forEach(ctrl=>{
    const msg=ctrl.querySelector('[data-security-message]');
    ctrl.querySelectorAll('[data-security-mode]').forEach(btn=>btn.addEventListener('click',async()=>{
      const mode=btn.dataset.securityMode;
      if(mode===ctrl.dataset.mode)return;
      msg.textContent='Updating…';
      try{
        const r=await fetch('/api/customer/security/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:mode,site_id:ctrl.dataset.siteId||null})});
        const data=await r.json().catch(()=>({}));
        if(!r.ok)throw new Error(data.detail||('HTTP '+r.status));
        paint(ctrl,data.mode);
        msg.textContent=data.mode==='disarmed'?'Disarmed. Intrusion alarms are off.':(LABELS[data.mode]+'. Your cameras will receive this within about a minute.');
      }catch(e){msg.textContent='Could not change the mode: '+e.message;}
    }));
  });
})();
</script>
'''


def security_page_content(overview: dict) -> str:
    site = overview['site']
    control = security_mode_control(overview['mode'], overview['can_change']).replace(
        'data-security-control', f'data-security-control data-site-id="{html.escape(site["id"])}"', 1)
    changed = ''
    if overview.get('changed_at'):
        changed = (f'<div class="health-detail">Last changed <span data-utc-time="{html.escape(str(overview["changed_at"]))}">'
                   f'{html.escape(str(overview["changed_at"])[:16].replace("T", " "))}</span> by {html.escape(str(overview.get("changed_by") or ""))}</div>'
                   '<script>document.querySelectorAll("[data-utc-time]").forEach(function(el){var v=el.dataset.utcTime;'
                   'var d=new Date(/[zZ]|[+-][0-9]{2}:?[0-9]{2}$/.test(v)?v:v+"Z");if(!isNaN(d))el.textContent=d.toLocaleString([],'
                   '{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"});});</script>')
    settings_html = ''
    if overview['can_change']:
        s = overview['settings']
        rows = ''.join(
            f'<tr><td>{html.escape(c.get("name") or c["id"])}</td>'
            f'<td><input type="checkbox" data-stay-cam="{html.escape(c["id"])}"{" checked" if c["armed_in_stay"] else ""}></td>'
            f'<td><input type="checkbox" data-away-cam="{html.escape(c["id"])}"{" checked" if c["armed_in_away"] else ""}></td></tr>'
            for c in overview['cameras']
        ) or '<tr><td colspan="3">No cameras at this site yet.</td></tr>'
        settings_html = f'''
        <section class="panel" id="security-settings">
          <div class="panel-head"><div><h2>Security settings</h2><div class="health-detail">
            Choose which cameras are armed in each mode. Arm Stay is normally the outside cameras only; Arm Away is normally every camera.
            Being armed only affects security lines (intrusion alarms) &mdash; recording and normal alerts are unchanged.</div></div></div>
          <table class="sec-cams"><thead><tr><th>Camera</th><th>Armed in Stay</th><th>Armed in Away</th></tr></thead><tbody>{rows}</tbody></table>
          <p><label><input type="checkbox" id="sec-sms"{" checked" if s.get("notify_sms") else ""}> Send an SMS for intrusion alarms (when SMS alerts are on for your account)</label></p>
          <p><label><input type="checkbox" id="sec-talkdown"{" checked" if s.get("talkdown_on_alarm") else ""}> Speak a warning through the camera speaker when an alarm fires</label></p>
          <p><label>Warning message<br><input type="text" id="sec-talkdown-message" class="sec-field" maxlength="300" style="width:100%" value="{html.escape(s.get("talkdown_message") or "")}"></label></p>
          <p><label><input type="checkbox" id="sec-siren" disabled> Sound a siren automatically when an alarm fires</label>
            <span class="health-detail">Coming soon: no siren is connected to AnyAiCam yet.</span></p>
          <p><label>Minimum time between alarms on the same line (seconds)
            <input type="number" id="sec-cooldown" class="sec-field" style="width:110px" min="10" max="3600" value="{int(s.get("alarm_cooldown_seconds") or 60)}"></label></p>
          <button type="button" class="action-button" id="sec-save" data-site-id="{html.escape(site["id"])}">Save security settings</button>
          <span class="health-detail" id="sec-save-message"></span>
        </section>'''
    return SECURITY_CONTROL_CSS + f'''
    <header class="topbar"><div><p class="eyebrow">Security</p><h1>Arm &amp; disarm &middot; {html.escape(site.get("name") or "")}</h1></div>
      <a class="ghost-button" href="/dashboard">Back to dashboard</a></header>
    <section class="panel">
      <div class="panel-head"><div><h2>Security mode</h2><div class="health-detail">
        When armed, a person who fully crosses a <a class="download" href="/analytics/smart-rules">security line</a> into the protected side
        raises an INTRUSION ALARM: an urgent notification with a link to the live camera, talk-down, and a Call 911 button.
        AnyAiCam never calls 911 for you.</div></div></div>
      {control}{changed}
    </section>{settings_html}'''


SECURITY_PAGE_SCRIPT = SECURITY_CONTROL_SCRIPT + '''
<script>
(function(){
  const save=document.getElementById('sec-save'); if(!save)return;
  save.addEventListener('click',async()=>{
    const msg=document.getElementById('sec-save-message');
    const pick=a=>[...document.querySelectorAll('['+a+']')].filter(i=>i.checked).map(i=>i.getAttribute(a));
    const settings={stay_camera_ids:pick('data-stay-cam'),away_camera_ids:pick('data-away-cam'),
      notify_sms:document.getElementById('sec-sms').checked,talkdown_on_alarm:document.getElementById('sec-talkdown').checked,
      talkdown_message:document.getElementById('sec-talkdown-message').value,
      alarm_cooldown_seconds:Number(document.getElementById('sec-cooldown').value)||60};
    msg.textContent='Saving…';
    try{
      const r=await fetch('/api/customer/security/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({site_id:save.dataset.siteId,settings:settings})});
      const data=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(data.detail||('HTTP '+r.status));
      msg.textContent='Saved. Your cameras will receive this within about a minute.';
    }catch(e){msg.textContent='Could not save: '+e.message;}
  });
})();
</script>
'''


def dashboard_security_panel(request: Request) -> str:
    """The Dashboard's Arm Stay / Arm Away / Disarm strip for a customer
    session ('' for anyone else, or a customer with no site yet)."""
    identity = partner_identity(request)
    if not identity or identity.get('role') not in {'customer_owner', 'customer_viewer'}:
        return ''
    try:
        with connection() as db:
            overview = security_overview(db, identity)
    except HTTPException:
        return ''
    control = security_mode_control(overview['mode'], overview['can_change']).replace(
        'data-security-control', f'data-security-control data-site-id="{html.escape(overview["site"]["id"])}"', 1)
    return SECURITY_CONTROL_CSS + (
        '<section class="panel" id="dashboard-security" aria-label="Security mode">'
        '<div class="panel-head"><div><h2>Security</h2></div>'
        '<a class="ghost-button" href="/customer-security">Security settings</a></div>'
        f'{control}</section>'
    ) + SECURITY_CONTROL_SCRIPT
