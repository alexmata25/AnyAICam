import json
import secrets
import string
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

DATA_FILE = Path('/app/recordings/account_management.json')


class Site(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:10])
    name: str
    site_type: str = 'Home'
    camera_count: int = Field(default=4, ge=1, le=64)


class User(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:10])
    email: str
    role: str
    site_ids: list[str] = Field(default_factory=list)
    camera_ids: list[int] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)


class Appliance(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:10])
    cloud_id: str = Field(default_factory=lambda: 'AIC-' + uuid4().hex[:8].upper())
    serial_number: str = Field(default_factory=lambda: 'SN-' + uuid4().hex[:10].upper())
    appliance_type: str = 'AnyAiCam mini PC'
    site_id: str = ''
    status: str = 'offline'
    last_check_in: str | None = None
    software_version: str = '1.0.0'
    ip_address: str = 'Not connected'
    camera_capacity: int = 16


def default_data() -> dict:
    return {'customer': {}, 'sites': [], 'users': [], 'appliances': [], 'pricing': {}, 'branding': {'company_name': 'AnyAiCam', 'primary_color': '#43d1cc', 'accent_color': '#4b4de2', 'appearance': 'dark', 'support': ''}}


def load_data() -> dict:
    try:
        data = json.loads(DATA_FILE.read_text(encoding='utf-8')) if DATA_FILE.exists() else default_data()
        base = default_data(); base.update(data); return base
    except (OSError, json.JSONDecodeError):
        return default_data()


def save_data(data: dict) -> None:
    temp = DATA_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    temp.replace(DATA_FILE)


def temp_password() -> str:
    alphabet = string.ascii_letters + string.digits + '!@#$%'
    return ''.join(secrets.choice(alphabet) for _ in range(14))


def panel(title: str, body: str) -> str:
    return f'<section class="panel"><div class="panel-head"><h2>{title}</h2></div>{body}</section>'


def register_business_routes(app: FastAPI, shell: Callable) -> None:
    @app.get('/api/account-management')
    def account_data() -> dict:
        return load_data()

    @app.post('/api/setup/complete')
    def complete_setup(payload: dict) -> dict:
        data = load_data(); password = temp_password(); customer_id = uuid4().hex[:10]
        data['customer'] = {'id': customer_id, 'name': payload.get('customer_name','New customer'), 'email': payload.get('email',''), 'login': payload.get('email',''), 'temporary_password': password, 'created_at': datetime.now().isoformat()}
        sites = payload.get('sites') or [{'name':'Home','site_type':'Home','camera_count':payload.get('camera_count',4)}]
        data['sites'] = [Site(**site).model_dump() for site in sites]
        appliance = Appliance(appliance_type=payload.get('appliance_type','AnyAiCam mini PC'), site_id=data['sites'][0]['id'], camera_capacity=max(8, int(payload.get('camera_count',4))))
        data['appliances'] = [appliance.model_dump()]
        data['pricing'] = payload.get('pricing', {})
        save_data(data)
        return {'status':'complete','message':'Customer setup completed.','login':data['customer']['login'],'temporary_password':password,'customer_id':customer_id}

    @app.post('/api/sites')
    def add_site(payload: dict) -> dict:
        data=load_data(); site=Site(**payload); data['sites'].append(site.model_dump()); save_data(data); return {'status':'complete','site':site.model_dump(),'message':'Site added.'}

    @app.post('/api/users')
    def invite_user(payload: dict) -> dict:
        data=load_data(); user=User(**payload); data['users'].append(user.model_dump()); save_data(data); return {'status':'complete','user':user.model_dump(),'message':'User invitation saved.'}

    @app.post('/api/branding')
    def update_branding(payload: dict) -> dict:
        data=load_data(); data['branding'].update(payload); save_data(data); return {'status':'complete','message':'Branding settings saved.','branding':data['branding']}

    @app.get('/setup-legacy', response_class=HTMLResponse, include_in_schema=False)
    def setup_page() -> str:
        retention=''.join(f'<option>{d}</option>' for d in [2,7,14,30,60,90,180,365])
        body='''<div class="mock-banner">Analytics remains in demo mode until the Ryzen mini PC is ready.</div><form id="setup-form" class="rule-form"><label>Customer name<input id="customer-name" required></label><label>Customer email<input id="customer-email" type="email" required></label><label>First site name<input id="site-name" value="Home" required></label><label>Appliance type<select id="appliance-type"><option>AnyAiCam mini PC</option><option>Customer-owned computer</option></select></label><label>Camera count<input id="camera-count" type="number" min="1" max="64" value="4"></label><label>Resolution<select id="resolution"><option>1080p</option><option>2K</option><option>4K</option></select></label><label>Retention<select id="retention">'''+retention+'''</select> days</label><label>Recording type<select id="recording-type"><option>Continuous</option><option>Motion</option></select></label><label>Analytics package<select id="analytics-package"><option>Demo analytics</option><option>Security essentials</option><option>Business intelligence</option></select></label><button class="action-button" type="submit">Generate installation package</button></form><div id="setup-result" class="panel" hidden></div>'''
        scripts='''<script>document.getElementById('setup-form').addEventListener('submit',async e=>{e.preventDefault();const payload={customer_name:document.getElementById('customer-name').value,email:document.getElementById('customer-email').value,appliance_type:document.getElementById('appliance-type').value,camera_count:Number(document.getElementById('camera-count').value),sites:[{name:document.getElementById('site-name').value,site_type:'Home',camera_count:Number(document.getElementById('camera-count').value)}],pricing:{resolution:document.getElementById('resolution').value,retention:Number(document.getElementById('retention').value),recording_type:document.getElementById('recording-type').value,analytics_package:document.getElementById('analytics-package').value}},response=await fetch('/api/setup/complete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),result=await response.json(),box=document.getElementById('setup-result');box.hidden=false;box.innerHTML=`<h2>Installation checklist</h2><p><strong>Login:</strong> ${result.login}<br><strong>Temporary password:</strong> ${result.temporary_password}</p><ol><li>Prepare appliance and network</li><li>Connect and verify cameras</li><li>Confirm recording and retention</li><li>Change temporary password</li><li>Test remote Tailscale access</li><li>Review demo analytics status</li></ol>`;showToast(result.message)});</script>'''
        return shell('Setup wizard','setup','<header class="topbar"><div><p class="eyebrow">Installer workflow</p><h1>Customer setup wizard</h1></div></header>'+panel('Customer and installation details',body),scripts)

    @app.get('/sites-management', response_class=HTMLResponse)
    def sites_page() -> str:
        data=load_data(); cards=''.join(f'<article class="feature-card"><div class="feature-icon">⌂</div><h2>{s["name"]}</h2><p>{s["site_type"]} · {s["camera_count"]} cameras</p><a class="download" href="/">Open cameras</a></article>' for s in data['sites']) or '<div class="empty">No sites configured.</div>'
        switcher='<select class="date-filter"><option>All sites</option>'+''.join(f'<option>{s["name"]}</option>' for s in data['sites'])+'</select>'
        return shell('Sites','sites','<header class="topbar"><div><p class="eyebrow">Multi-site account</p><h1>Sites</h1></div>'+switcher+'</header><div class="feature-grid">'+cards+'</div>')

    @app.get('/users', response_class=HTMLResponse)
    def users_page() -> str:
        data=load_data(); rows=''.join(f'<tr><td>{u["email"]}</td><td>{u["role"]}</td><td>{len(u["site_ids"])} site(s)</td><td>{", ".join(u["permissions"])}</td></tr>' for u in data['users']) or '<tr><td colspan="4">No invited users.</td></tr>'
        roles=''.join(f'<option>{r}</option>' for r in ['owner','administrator','installer','employee','family member','viewer'])
        body=f'''<form id="invite-form" class="clip-form"><label>Email<input id="invite-email" type="email" required></label><label>Role<select id="invite-role">{roles}</select></label><label>Permissions<input id="invite-permissions" value="live view, playback"></label><button class="action-button">Invite user</button></form><table class="data-table" style="margin-top:22px"><thead><tr><th>Email</th><th>Role</th><th>Site access</th><th>Permissions</th></tr></thead><tbody>{rows}</tbody></table>'''
        scripts='''<script>document.getElementById('invite-form').addEventListener('submit',async e=>{e.preventDefault();const payload={email:document.getElementById('invite-email').value,role:document.getElementById('invite-role').value,permissions:document.getElementById('invite-permissions').value.split(',').map(v=>v.trim()),site_ids:[],camera_ids:[]},response=await fetch('/api/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),result=await response.json();showToast(result.message);setTimeout(()=>location.reload(),600)});</script>'''
        return shell('Users','users','<header class="topbar"><div><p class="eyebrow">Access control</p><h1>Users and permissions</h1></div></header>'+panel('Invite and manage users',body),scripts)

    @app.get('/pricing-legacy', response_class=HTMLResponse, include_in_schema=False)
    def pricing_page() -> str:
        return shell('Pricing','pricing','''<header class="topbar"><div><p class="eyebrow">Configurable pricing model</p><h1>Subscription estimator</h1></div></header><div class="mock-banner">Estimate only — this is not live billing.</div><section class="health-grid"><form class="panel rule-form" id="price-form"><label>Camera count<input id="price-cameras" type="number" value="4" min="1"></label><label>Retention<select id="price-retention"><option>2</option><option selected>7</option><option>14</option><option>30</option><option>60</option><option>90</option><option>180</option><option>365</option></select></label><label>Recording<select id="price-recording"><option>Motion</option><option>Continuous</option></select></label><label>Resolution<select id="price-resolution"><option>1080p</option><option>2K</option><option>4K</option></select></label><label>Analytics<select id="price-analytics"><option>Demo</option><option>Essentials</option><option>Business</option></select></label><label>Appliance cost<input id="price-appliance" type="number" value="399"></label><label>Installation cost<input id="price-install" type="number" value="199"></label></form><div class="panel"><h2>Estimate</h2><div class="stat-value" id="monthly-price">$0/month</div><p id="one-time-price"></p><p class="health-detail">Pricing formula is configurable and does not charge customers.</p></div></section>''','''<script>const inputs=document.querySelectorAll('#price-form input,#price-form select');function estimate(){const c=Number(document.getElementById('price-cameras').value),r=Number(document.getElementById('price-retention').value),continuous=document.getElementById('price-recording').value==='Continuous'?1.5:1,res={'1080p':1,'2K':1.35,'4K':2}[document.getElementById('price-resolution').value],analytics={'Demo':0,'Essentials':4,'Business':8}[document.getElementById('price-analytics').value],monthly=c*((4+r*.18)*continuous*res+analytics);document.getElementById('monthly-price').textContent='$'+monthly.toFixed(2)+'/month';document.getElementById('one-time-price').textContent='$'+(Number(document.getElementById('price-appliance').value)+Number(document.getElementById('price-install').value)).toFixed(2)+' estimated one-time cost'}inputs.forEach(i=>i.addEventListener('input',estimate));estimate();</script>''')

    @app.get('/appliances', response_class=HTMLResponse)
    def appliances_page() -> str:
        data=load_data(); rows=''.join(f'<tr><td>{a["cloud_id"]}</td><td>{a["serial_number"]}</td><td>{a["status"]}</td><td>{a.get("last_check_in") or "Never"}</td><td>{a["software_version"]}</td><td>{a["ip_address"]}</td><td>{a["camera_capacity"]}</td><td><button class="download" onclick="comingSoon(\'Restart appliance\')">Restart</button> · <button class="download" onclick="comingSoon(\'Software update\')">Update</button></td></tr>' for a in data['appliances']) or '<tr><td colspan="8">No appliances assigned.</td></tr>'
        return shell('Appliances','appliances','<header class="topbar"><div><p class="eyebrow">Fleet management</p><h1>Appliances</h1></div></header>'+panel('AnyAiCam and customer-owned computers',f'<table class="data-table"><thead><tr><th>Cloud ID</th><th>Serial</th><th>Status</th><th>Last check-in</th><th>Version</th><th>IP</th><th>Capacity</th><th>Actions</th></tr></thead><tbody>{rows}</tbody></table><div class="health-detail" style="margin-top:16px">CPU, memory, and disk metrics appear after hardware check-in.</div>'))

    @app.get('/branding', response_class=HTMLResponse)
    def branding_page() -> str:
        b=load_data()['branding']; body=f'''<form id="branding-form" class="rule-form"><label>Company name<input id="brand-company" value="{b['company_name']}"></label><label>Primary color<input id="brand-primary" type="color" value="{b['primary_color']}"></label><label>Accent color<input id="brand-accent" type="color" value="{b['accent_color']}"></label><label>Appearance<select id="brand-appearance"><option>dark</option><option>light</option></select></label><label>Support information<input id="brand-support" value="{b['support']}"></label><label>Logo upload<input id="brand-logo-upload" type="file" accept="image/png,image/jpeg,image/svg+xml"></label><div class="health-detail">Logo preview is local until upload storage is connected.</div><button class="action-button">Save branding</button></form>'''; scripts='''<script>document.getElementById('branding-form').addEventListener('submit',async e=>{e.preventDefault();const payload={company_name:document.getElementById('brand-company').value,primary_color:document.getElementById('brand-primary').value,accent_color:document.getElementById('brand-accent').value,appearance:document.getElementById('brand-appearance').value,support:document.getElementById('brand-support').value},response=await fetch('/api/branding',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),result=await response.json();showToast(result.message)});</script>'''; return shell('Branding','branding','<header class="topbar"><div><p class="eyebrow">White-label settings</p><h1>Branding</h1></div></header>'+panel('Company appearance',body),scripts)
