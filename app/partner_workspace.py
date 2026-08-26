from cloud_config import settings as cloud_settings
import json
import secrets
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from partner_portal import partner_identity, require_partner_access
from pricing_config import calculate_partner_quote, calculate_quote, load_pricing
from appliance_protocol import encrypt_camera_credentials
from partner_db import audit, connection, password_hash, require_permission, row, rows, verify_password
from email_service import get_email_service

CUSTOMERS_FILE = Path('/app/recordings/partner_customers.json')
ACCOUNT_FILE = Path('/app/recordings/account_management.json')
STATUSES = {'active', 'pending_installation', 'trial', 'suspended', 'cancelled'}


def _read(path: Path, fallback):
    try: return json.loads(path.read_text(encoding='utf-8')) if path.exists() else fallback
    except (OSError, json.JSONDecodeError): return fallback


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8'); temporary.replace(path)


def _temporary_password() -> str:
    return secrets.token_urlsafe(12)


def register_partner_workspace_routes(app: FastAPI, shell: Callable) -> None:
    def customer_owner(request: Request) -> dict:
        identity=partner_identity(request)
        if not identity or identity.get('role')!='customer_owner': raise HTTPException(status_code=403,detail='Customer owner permission required.')
        return identity

    @app.get('/api/partner/workspace/customers')
    def workspace_customers(request: Request) -> dict:
        identity=require_partner_access(request)
        try: require_permission(identity,'customer.view')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        return {'customers': rows('SELECT * FROM customers WHERE partner_id=? ORDER BY created_at DESC',(identity.get('partner_id') or 'anyaicam-primary',))}

    @app.post('/api/partner/onboarding/drafts')
    def save_onboarding_draft(request: Request,payload: dict) -> dict:
        identity=require_partner_access(request)
        try: require_permission(identity,'customer.create')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        draft_id=str(payload.get('id') or secrets.token_hex(6)); existing=row('SELECT id FROM onboarding_drafts WHERE id=? AND actor_email=?',(draft_id,identity['email']))
        with connection() as db:
            if existing: db.execute('UPDATE onboarding_drafts SET current_step=?,data_json=?,updated_at=? WHERE id=?',(int(payload.get('current_step',1)),json.dumps(payload.get('data',{})),datetime.now().isoformat(),draft_id))
            else: db.execute('INSERT INTO onboarding_drafts(id,actor_email,current_step,data_json,status,updated_at) VALUES(?,?,?,?,?,?)',(draft_id,identity['email'],int(payload.get('current_step',1)),json.dumps(payload.get('data',{})),'draft',datetime.now().isoformat()))
        return {'status':'saved','draft_id':draft_id,'message':'Onboarding progress saved.'}

    @app.get('/api/partner/onboarding/drafts/{draft_id}')
    def get_onboarding_draft(request: Request,draft_id: str) -> dict:
        identity=require_partner_access(request); draft=row('SELECT * FROM onboarding_drafts WHERE id=? AND actor_email=?',(draft_id,identity['email']))
        if not draft: raise HTTPException(status_code=404,detail='Onboarding draft not found.')
        draft['data']=json.loads(draft.pop('data_json')); return draft

    @app.post('/api/partner/customers/onboard')
    def onboard_customer(request: Request, payload: dict) -> dict:
        identity=require_partner_access(request)
        try: require_permission(identity,'customer.create'); require_permission(identity,'quote.create')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        status=str(payload.get('status','pending_installation'))
        if status not in STATUSES: raise HTTPException(status_code=400, detail='Unsupported customer status.')
        sites=payload.get('sites') or []
        if not sites: raise HTTPException(status_code=400, detail='Add at least one customer site.')
        pricing_selection=payload.get('pricing',{})
        try: quote=calculate_partner_quote(pricing_selection)
        except ValueError as error: raise HTTPException(status_code=409, detail=str(error)) from error
        email=str(payload.get('email','')).strip().lower()
        if row('SELECT id FROM customers WHERE email=?',(email,)): raise HTTPException(status_code=409,detail='A customer with this email already exists.')
        now=datetime.now().isoformat(); partner_id=identity.get('partner_id') or 'anyaicam-primary'; customer_id=secrets.token_hex(5); password=_temporary_password(); quote_id=secrets.token_hex(5); invitation_id=secrets.token_hex(5); plan_id=secrets.token_hex(5)
        email_preview=f'''Subject: Welcome to AnyAiCam\n\nHello {payload.get('name','')},\nYour AnyAiCam account is ready.\nLogin: {email}\nTemporary password: {password}\nPlease change your password after signing in.'''
        created_sites=[]; activation_tokens=[]
        with connection() as db:
            db.execute('INSERT INTO customers(id,partner_id,name,company,email,phone,status,trial_status,billing_status,source,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(customer_id,partner_id,payload.get('name',''),payload.get('company',''),email,payload.get('phone',''),status,'eligible','placeholder','real',now,identity['email']))
            for site_data in sites:
                site_id=secrets.token_hex(5); db.execute('INSERT INTO sites(id,customer_id,name,address,site_type,created_at) VALUES(?,?,?,?,?,?)',(site_id,customer_id,site_data.get('name','Site'),site_data.get('address',''),site_data.get('site_type','Customer site'),now))
                cloud_id='AIC-'+secrets.token_hex(4).upper(); activation=secrets.token_urlsafe(24); activation_tokens.append({'site':site_data.get('name','Site'),'cloud_id':cloud_id,'activation_token':activation})
                appliance_id=secrets.token_hex(5); db.execute('INSERT INTO appliances(id,customer_id,site_id,cloud_id,appliance_type,serial_number,software_version,last_check_in,online_status,ip_address,cpu,memory,disk,camera_capacity,activation_token_hash,activation_token_created_at,shipping_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(appliance_id,customer_id,site_id,cloud_id,payload.get('appliance_type','AnyAiCam mini PC'),payload.get('serial_number','Pending'),'Not installed',None,'offline','Not connected',0,0,0,max(16,quote['quantity']),password_hash(activation),now,'not_ordered',now))
                db.execute('INSERT INTO appliance_activation_tokens(id,appliance_id,token_hash,expires_at,created_at,created_by) VALUES(?,?,?,?,?,?)',(secrets.token_hex(6),appliance_id,password_hash(activation),(datetime.now()+timedelta(hours=24)).isoformat(),now,identity['email']))
                created_sites.append({'id':site_id,'name':site_data.get('name','Site'),'appliance_id':appliance_id})
            primary_site=created_sites[0]
            for camera_number in range(1,quote['quantity']+1): db.execute('INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,resolution,status,created_at) VALUES(?,?,?,?,?,?,?,?)',(secrets.token_hex(5),customer_id,primary_site['id'],primary_site['appliance_id'],f'Camera {camera_number}',quote['resolution'],'pending_installation',now))
            db.execute('INSERT INTO plans(id,customer_id,resolution,recording_mode,retention_days,camera_quantity,retail_monthly,partner_monthly,monthly_recurring_profit,annual_total,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(plan_id,customer_id,quote['resolution'],quote['recording'],quote['retention_days'],quote['quantity'],quote['monthly_customer_revenue'],quote['monthly_partner_charge'],quote['monthly_recurring_profit'],quote['annual_total'],'quote',now))
            for analytic in quote['addons']: db.execute('INSERT INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,status,monthly_retail,monthly_partner,created_at) VALUES(?,?,?,?,?,?,?,?)',(secrets.token_hex(5),customer_id,created_sites[0]['id'],analytic,'pending',load_pricing()['addons'][analytic]['price'],None,now))
            db.execute('INSERT INTO quotes(id,customer_id,partner_id,status,selection_json,totals_json,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)',(quote_id,customer_id,partner_id,'estimate',json.dumps(pricing_selection),json.dumps(quote),now,identity['email']))
            db.execute('INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,must_change_password) VALUES(?,?,?,?,?,?,?,?,?,1)',(secrets.token_hex(5),partner_id,email,payload.get('name','Customer'),'customer_owner',password_hash(password),1,customer_id,now))
            db.execute('INSERT INTO invitations(id,email,role,customer_id,status,temporary_password_hash,email_preview,expires_at,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)',(invitation_id,email,'customer_owner',customer_id,'preview',password_hash(password),email_preview,None,now,identity['email']))
            db.execute('INSERT INTO service_history(customer_id,event,details,created_at,created_by) VALUES(?,?,?,?,?)',(customer_id,'Customer onboarding created','Quote, login, sites, appliance assignments, and plan created.',now,identity['email']))
            if payload.get('draft_id'): db.execute("UPDATE onboarding_drafts SET status='complete',updated_at=? WHERE id=?",(now,payload['draft_id']))
        for action,entity,entity_id in [('customer.created','customer',customer_id),('quote.created','quote',quote_id),('user.invited','invitation',invitation_id),('appliance.assigned','customer',customer_id),('plan.changed','plan',plan_id)]: audit(identity,action,entity,entity_id)
        try:
            delivery=get_email_service().send('invitation',email,'Welcome to AnyAiCam',email_preview,metadata={'customer_id':customer_id,'invitation_id':invitation_id})
            with connection() as db: db.execute('UPDATE invitations SET status=? WHERE id=?',(delivery['status'],invitation_id))
        except Exception as error:
            delivery={'status':'error','error':str(error)}
        customer={'id':customer_id,'name':payload.get('name',''),'quote':quote,'sites':created_sites}
        return {'status':'complete','message':'Customer, quote, account, and provisioning records created.','customer':customer,
                'login':email,'temporary_password':password,'invitation':f'Invitation status: {delivery["status"]}.',
                'invitation_email_preview':email_preview,'activation_tokens':activation_tokens,
                'checklist':['Confirm customer quote','Order or prepare appliance','Assign sites and cameras','Verify network and streams','Activate cloud recording','Confirm retention','Send customer invitation','Test remote access']}

    @app.post('/api/partner/appliances/{appliance_id}/activation-token')
    def regenerate_activation_token(request: Request,appliance_id: str) -> dict:
        identity=require_partner_access(request)
        try: require_permission(identity,'appliance.assign')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        token=secrets.token_urlsafe(24); now=datetime.now().isoformat()
        with connection() as db: db.execute('UPDATE appliances SET activation_token_hash=?,activation_token_created_at=? WHERE id=?',(password_hash(token),now,appliance_id))
        audit(identity,'appliance.activation_token_generated','appliance',appliance_id); return {'activation_token':token,'message':'New one-time activation token generated.'}

    @app.post('/api/partner/appliances/{appliance_id}/{action}')
    def appliance_action(request: Request,appliance_id: str,action: str) -> dict:
        identity=require_partner_access(request)
        try: require_permission(identity,'appliance.action')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        if action not in {'restart','update'}: raise HTTPException(status_code=400,detail='Unsupported appliance action.')
        audit(identity,f'appliance.{action}_requested','appliance',appliance_id); return {'status':'placeholder','message':f'Appliance {action} request recorded; hardware execution is not connected yet.'}

    @app.post('/api/partner/customers/{customer_id}/notes')
    def add_customer_note(request: Request,customer_id: str,payload: dict) -> dict:
        identity=require_partner_access(request)
        try: require_permission(identity,'customer.edit')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        note=str(payload.get('note','')).strip()
        if not note: raise HTTPException(status_code=400,detail='Note is required.')
        with connection() as db: db.execute('INSERT INTO customer_notes(customer_id,note,created_at,created_by) VALUES(?,?,?,?)',(customer_id,note,datetime.now().isoformat(),identity['email']))
        audit(identity,'customer.note_added','customer',customer_id); return {'message':'Customer note saved.'}

    @app.post('/api/partner/users/invite')
    def invite_portal_user(request: Request,payload: dict) -> dict:
        identity=require_partner_access(request)
        try: require_permission(identity,'user.invite')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        role=str(payload.get('role','customer_viewer')); valid={'partner_owner','salesperson','technician','customer_owner','customer_viewer'}
        if role not in valid: raise HTTPException(status_code=400,detail='Unsupported invitation role.')
        email=str(payload.get('email','')).strip().lower(); password=_temporary_password(); now=datetime.now().isoformat(); user_id=secrets.token_hex(5); invitation_id=secrets.token_hex(5); customer_id=payload.get('customer_id')
        preview=f'Subject: AnyAiCam invitation\n\nYou were invited as {role.replace("_"," ")}.\nLogin: {email}\nTemporary password: {password}'
        try:
            with connection() as db:
                db.execute('INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,must_change_password) VALUES(?,?,?,?,?,?,?,?,?,1)',(user_id,identity.get('partner_id') or 'anyaicam-primary',email,payload.get('name',''),role,password_hash(password),1,customer_id,now))
                db.execute('INSERT INTO invitations(id,email,role,customer_id,status,temporary_password_hash,email_preview,expires_at,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)',(invitation_id,email,role,customer_id,'preview',password_hash(password),preview,None,now,identity['email']))
        except Exception as error:
            raise HTTPException(status_code=409,detail='A user with this email may already exist.') from error
        audit(identity,'user.invited','partner_user',user_id,{'role':role}); audit(identity,'permission.changed','partner_user',user_id,{'role':role})
        return {'message':'Invitation preview created.','temporary_password':password,'email_preview':preview}

    @app.put('/api/partner/customers/{customer_id}/plan')
    def change_customer_plan(request: Request,customer_id: str,payload: dict) -> dict:
        identity=require_partner_access(request)
        try: require_permission(identity,'customer.edit'); require_permission(identity,'quote.create')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        try: quote=calculate_partner_quote(payload)
        except ValueError as error: raise HTTPException(status_code=409,detail=str(error)) from error
        now=datetime.now().isoformat(); plan_id=secrets.token_hex(5)
        with connection() as db: db.execute('INSERT INTO plans(id,customer_id,resolution,recording_mode,retention_days,camera_quantity,retail_monthly,partner_monthly,monthly_recurring_profit,annual_total,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(plan_id,customer_id,quote['resolution'],quote['recording'],quote['retention_days'],quote['quantity'],quote['monthly_customer_revenue'],quote['monthly_partner_charge'],quote['monthly_recurring_profit'],quote['annual_total'],'pending_confirmation',now))
        audit(identity,'plan.changed','plan',plan_id,{'customer_id':customer_id}); return {'message':'Plan change estimate saved.','quote':quote}

    @app.get('/partner/onboarding',response_class=HTMLResponse)
    def onboarding_page(request: Request):
        identity=partner_identity(request)
        if not identity: return RedirectResponse('/partner-login',status_code=303)
        try: require_permission(identity,'customer.create')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        content='''<header class="topbar"><div><p class="eyebrow">Resumable customer onboarding</p><h1>Add New Customer</h1></div><a class="ghost-button" href="/partner">Back to Partner Portal</a></header><div class="mock-banner">Progress is saved after each step. Partner pricing remains confidential.</div><section class="panel"><div class="workspace-tabs" id="wizard-tabs"><button class="workspace-tab active">1 Customer</button><button class="workspace-tab">2 Sites</button><button class="workspace-tab">3 System</button><button class="workspace-tab">4 Pricing</button><button class="workspace-tab">5 Review</button></div><form id="onboarding-wizard" class="rule-form"><div class="wizard-step" data-step="1"><h2>Customer and company</h2><label>Customer owner name<input id="w-name" required></label><label>Company<input id="w-company"></label><label>Email<input id="w-email" type="email" required></label><label>Phone<input id="w-phone"></label><label>Status<select id="w-status"><option value="pending_installation">Pending installation</option><option value="trial">Trial</option><option value="active">Active</option></select></label></div><div class="wizard-step" data-step="2" hidden><h2>Sites</h2><label>Site names, one per line<textarea id="w-sites" rows="6" required>Primary site</textarea></label><p class="health-detail">Each line creates a separate site and appliance assignment.</p></div><div class="wizard-step" data-step="3" hidden><h2>Appliance and cameras</h2><label>Computer type<select id="w-appliance"><option>AnyAiCam mini PC</option><option>Customer-owned computer</option></select></label><label>Camera quantity<input id="w-quantity" type="number" min="1" max="128" value="4"></label><label>Resolution<select id="w-resolution"><option value="2mp">2MP / 1080p</option><option value="4mp">4MP</option><option value="8mp">8MP / 4K</option></select></label><label>Recording<select id="w-recording"><option value="motion">Motion</option><option value="continuous">Continuous</option></select></label><label>Retention<select id="w-retention"><option value="2">2 days</option><option value="7">7 days</option><option value="14">14 days</option><option value="30">30 days</option></select></label><fieldset><legend>Analytics</legend><label><input class="w-addon" type="checkbox" value="smart_motion"> Smart Motion</label><label><input class="w-addon" type="checkbox" value="people_counting"> People Counting</label><label><input class="w-addon" type="checkbox" value="lpr"> LPR</label><label><input class="w-addon" type="checkbox" value="ppe"> PPE Monitoring</label></fieldset></div><div class="wizard-step" data-step="4" hidden><h2>Customer price</h2><label>Approved customer selling price per camera<input id="w-selling" type="number" min="0" step="0.01" placeholder="Defaults to retail price"></label><button class="ghost-button" id="calculate-onboarding" type="button">Calculate retail, partner price, and margin</button><div id="wizard-pricing" class="panel"></div></div><div class="wizard-step" data-step="5" hidden><h2>Review and create</h2><div id="wizard-review" class="panel"></div><button class="action-button" type="submit">Generate quote and create customer</button></div><div class="dialog-actions"><button class="ghost-button" id="wizard-back" type="button" hidden>Back</button><button class="action-button" id="wizard-next" type="button">Save and continue</button></div></form><section class="panel" id="wizard-result" hidden></section></section>'''
        scripts='''<script>let step=1,draftId=new URLSearchParams(location.search).get('draft');const form=document.getElementById('onboarding-wizard'),steps=[...document.querySelectorAll('.wizard-step')],tabs=[...document.querySelectorAll('#wizard-tabs .workspace-tab')];function data(){const selling=document.getElementById('w-selling').value;return{name:document.getElementById('w-name').value,company:document.getElementById('w-company').value,email:document.getElementById('w-email').value,phone:document.getElementById('w-phone').value,status:document.getElementById('w-status').value,sites:document.getElementById('w-sites').value.split('\\n').map(name=>({name:name.trim()})).filter(x=>x.name),appliance_type:document.getElementById('w-appliance').value,pricing:{resolution:document.getElementById('w-resolution').value,recording:document.getElementById('w-recording').value,retention:Number(document.getElementById('w-retention').value),quantity:Number(document.getElementById('w-quantity').value),addons:[...document.querySelectorAll('.w-addon:checked')].map(x=>x.value),...(selling?{selling_price_per_camera:Number(selling)}:{})}}}function show(){steps.forEach(x=>x.hidden=Number(x.dataset.step)!==step);tabs.forEach((x,i)=>x.classList.toggle('active',i===step-1));document.getElementById('wizard-back').hidden=step===1;document.getElementById('wizard-next').hidden=step===5;if(step===5)document.getElementById('wizard-review').innerHTML=`<strong>${data().name}</strong><p>${data().sites.length} site(s) · ${data().pricing.quantity} cameras · ${data().pricing.resolution.toUpperCase()} · ${data().pricing.retention} days</p>`}async function save(){const response=await fetch('/api/partner/onboarding/drafts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:draftId,current_step:step,data:data()})}),result=await response.json();draftId=result.draft_id;history.replaceState(null,'',`?draft=${draftId}`)}document.getElementById('wizard-next').onclick=async()=>{if(step===1&&(!data().name||!data().email))return showToast('Customer name and email are required.');await save();step++;show()};document.getElementById('wizard-back').onclick=()=>{step--;show()};async function calculate(){const response=await fetch('/api/partner/calculate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data().pricing)}),r=await response.json(),box=document.getElementById('wizard-pricing');box.innerHTML=response.ok?`<div class="health-row"><span>Retail monthly</span><strong>$${r.monthly_customer_revenue.toFixed(2)}</strong></div><div class="health-row"><span>Partner monthly</span><strong>$${r.monthly_partner_charge.toFixed(2)}</strong></div><div class="health-row"><span>Monthly recurring profit</span><strong>$${r.monthly_recurring_profit.toFixed(2)}</strong></div><div class="health-row"><span>First-year profit</span><strong>$${r.first_year_profit.toFixed(2)}</strong></div>`:`<div class="mock-banner">${r.detail}</div>`}document.getElementById('calculate-onboarding').onclick=calculate;form.addEventListener('submit',async e=>{e.preventDefault();await save();const payload={...data(),draft_id:draftId},response=await fetch('/api/partner/customers/onboard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),r=await response.json(),box=document.getElementById('wizard-result');box.hidden=false;if(!response.ok){box.innerHTML=`<div class="mock-banner">${r.detail}</div>`;return}box.innerHTML=`<h2>Customer created</h2><p><strong>Login:</strong> ${r.login}<br><strong>Temporary password:</strong> ${r.temporary_password}</p><pre style="white-space:pre-wrap">${r.invitation_email_preview}</pre><h2>Installation checklist</h2><ol>${r.checklist.map(x=>`<li>${x}</li>`).join('')}</ol><a class="action-button" href="/partner/customers/${r.customer.id}">Open customer record</a>`});show();</script>'''
        return shell('Customer onboarding','partner',content,scripts)

    @app.get('/partner/customers/{customer_id}',response_class=HTMLResponse)
    def customer_detail(request: Request,customer_id: str):
        identity=require_partner_access(request)
        try: require_permission(identity,'customer.view')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        customer=row('SELECT * FROM customers WHERE id=?',(customer_id,))
        if not customer: raise HTTPException(status_code=404,detail='Customer not found.')
        sites=rows('SELECT * FROM sites WHERE customer_id=?',(customer_id,)); appliances=rows('SELECT * FROM appliances WHERE customer_id=?',(customer_id,)); cameras=rows('SELECT * FROM cameras WHERE customer_id=?',(customer_id,)); plans=rows('SELECT * FROM plans WHERE customer_id=? ORDER BY created_at DESC',(customer_id,)); analytics=rows('SELECT * FROM analytics_subscriptions WHERE customer_id=?',(customer_id,)); history=rows('SELECT * FROM service_history WHERE customer_id=? ORDER BY created_at DESC',(customer_id,)); notes=rows('SELECT * FROM customer_notes WHERE customer_id=? ORDER BY created_at DESC',(customer_id,))
        site_cards=''.join(f'<article class="feature-card"><h2>{escape(x["name"])}</h2><p>{escape(x.get("address") or "No address entered")}</p></article>' for x in sites) or '<div class="empty">No sites.</div>'
        appliance_rows=''.join(f'<tr><td>{escape(x["cloud_id"])}</td><td>{escape(x.get("serial_number") or "Pending")}</td><td>{escape(x.get("online_status") or "offline")}</td><td>{escape(x.get("software_version") or "Not installed")}</td><td>{escape(x.get("ip_address") or "Not connected")}</td><td>{x.get("cpu",0)} / {x.get("memory",0)} / {x.get("disk",0)}</td><td><button class="download appliance-action" data-id="{x["id"]}" data-action="restart">Restart</button> · <button class="download appliance-action" data-id="{x["id"]}" data-action="update">Update</button></td></tr>' for x in appliances) or '<tr><td colspan="7">No appliances.</td></tr>'
        current=plans[0] if plans else {}; history_html=''.join(f'<div class="activity-row"><div><strong>{escape(x["event"])}</strong><div class="health-detail">{escape(x.get("details") or "")}</div></div><span class="activity-time">{escape(x["created_at"][:19])}</span></div>' for x in history) or '<div class="empty">No service history.</div>'; notes_html=''.join(f'<div class="activity-row"><div>{escape(x["note"])}</div><span class="activity-time">{escape(x["created_at"][:19])}</span></div>' for x in notes) or '<div class="empty">No notes.</div>'
        content=f'''<header class="topbar"><div><p class="eyebrow">Real customer record</p><h1>{escape(customer['name'])}</h1><div class="health-detail">{escape(customer.get('company') or '')} · {escape(customer['status'])}</div></div><a class="ghost-button" href="/partner">Back to customers</a></header><section class="summary"><div class="stat"><span class="stat-label">Sites</span><span class="stat-value">{len(sites)}</span></div><div class="stat"><span class="stat-label">Cameras</span><span class="stat-value">{len(cameras)} / {current.get('camera_quantity',0)}</span></div><div class="stat"><span class="stat-label">Trial / billing</span><span class="stat-value">{escape(customer.get('trial_status') or '—')} / {escape(customer.get('billing_status') or '—')}</span></div></section><h2>Sites</h2><div class="feature-grid" style="margin:14px 0 24px">{site_cards}</div><section class="panel" style="overflow:auto"><h2>Appliances</h2><table class="data-table"><thead><tr><th>Cloud ID</th><th>Serial</th><th>Status</th><th>Version</th><th>IP</th><th>CPU / Memory / Disk</th><th>Actions</th></tr></thead><tbody>{appliance_rows}</tbody></table></section><section class="account-grid" style="margin-top:18px"><div class="panel"><h2>Current plan</h2><p>{escape(str(current.get('resolution','—')).upper())} · {escape(str(current.get('recording_mode','—')))} · {current.get('retention_days','—')} days</p><p>Retail ${current.get('retail_monthly',0):,.2f} · Partner ${current.get('partner_monthly',0):,.2f}</p></div><div class="panel"><h2>Analytics</h2><p>{', '.join(escape(x['analytic_key'].replace('_',' ').title()) for x in analytics) or 'None selected'}</p></div><div class="panel"><h2>Service history</h2>{history_html}</div><div class="panel"><h2>Notes</h2>{notes_html}<form id="note-form" class="rule-form"><label>Add note<textarea id="customer-note" required></textarea></label><button class="action-button">Save note</button></form></div></section>'''
        scripts=f'''<script>document.querySelectorAll('.appliance-action').forEach(button=>button.onclick=async()=>{{const response=await fetch(`/api/partner/appliances/${{button.dataset.id}}/${{button.dataset.action}}`,{{method:'POST'}}),r=await response.json();showToast(r.message)}});document.getElementById('note-form').addEventListener('submit',async e=>{{e.preventDefault();const response=await fetch('/api/partner/customers/{customer_id}/notes',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{note:document.getElementById('customer-note').value}})}}),r=await response.json();showToast(r.message);setTimeout(()=>location.reload(),500)}})</script>'''
        return shell('Customer detail','partner',content,scripts)

    @app.get('/customer-account',response_class=HTMLResponse)
    def customer_account(request: Request):
        identity=partner_identity(request)
        if not identity or identity.get('role') not in {'customer_owner','customer_viewer'}:
            return RedirectResponse('/partner-login',status_code=303)

        customer=row('SELECT * FROM customers WHERE id=?',(identity.get('customer_id'),))
        if not customer:
            raise HTTPException(status_code=404,detail='Customer account not found.')

        activated=row(
            "SELECT id FROM appliances WHERE customer_id=? AND activation_status='activated'",
            (customer['id'],)
        )
        # An activated appliance is necessary but not sufficient: a
        # customer can have a linked, activated appliance and still
        # have zero real cameras (nothing discovered/confirmed yet) --
        # that customer belongs in Setup too, not looking at an empty
        # or placeholder-only camera list on their own account page.
        commissioned_camera=row(
            'SELECT id FROM cameras WHERE customer_id=? AND (cameras.status=? OR EXISTS(SELECT 1 FROM recordings r WHERE r.camera_id=cameras.id)) LIMIT 1',
            (customer['id'],'configured')
        )
        if identity['role']=='customer_owner' and not (activated and commissioned_camera):
            return RedirectResponse('/customer/setup',status_code=303)

        cameras=rows(
            'SELECT id,name,camera_number,status FROM cameras '
            'WHERE customer_id=? AND (cameras.status=? OR EXISTS(SELECT 1 FROM recordings r WHERE r.camera_id=cameras.id)) ORDER BY camera_number',
            (customer['id'],'configured')
        )

        camera_cards=''.join(
            f'''<article class="feature-card">
                <div class="feature-icon">▣</div>
                <h2>{escape(camera.get("name") or f"Camera {camera.get('camera_number') or ''}")}</h2>
                <p>{escape(camera.get("status") or "configured")}</p>
                <a class="action-button" href="/customer/cameras/{escape(camera['id'],quote=True)}/live">Live view</a>
            </article>'''
            for camera in cameras
        ) or '<div class="empty">No cameras are assigned to this customer account.</div>'

        content=f'''<header class="topbar">
            <div>
                <p class="eyebrow">Customer VMS</p>
                <h1>{escape(customer.get("name") or "My Cameras")}</h1>
            </div>
            <form method="post" action="/partner-logout">
                <button class="ghost-button" type="submit">Sign out</button>
            </form>
        </header>
        <section class="panel">
            <div class="panel-head">
                <div>
                    <h2>Your cameras</h2>
                    <div class="health-detail">Live video is delivered through the AnyAiCam cloud relay.</div>
                </div>
            </div>
            <div class="feature-grid">{camera_cards}</div>
        </section>'''

        return shell('Customer VMS','dashboard',content)

    @app.get('/customer/setup',response_class=HTMLResponse)
    def customer_first_setup(request: Request):
        identity=partner_identity(request)
        if not identity: return RedirectResponse('/partner-login',status_code=303)
        if identity.get('role')!='customer_owner': raise HTTPException(status_code=403,detail='Customer owner permission required.')
        customer=row('SELECT * FROM customers WHERE id=?',(identity['customer_id'],)); sites=rows('SELECT * FROM sites WHERE customer_id=?',(identity['customer_id'],)); appliances=rows('SELECT * FROM appliances WHERE customer_id=?',(identity['customer_id'],)); cameras=rows('SELECT * FROM cameras WHERE customer_id=?',(identity['customer_id'],)); plan=row('SELECT * FROM plans WHERE customer_id=? ORDER BY created_at DESC LIMIT 1',(identity['customer_id'],)) or {}
        if not customer: raise HTTPException(status_code=404,detail='Customer account not found.')
        appliance_options=''.join(f'<option value="{a["id"]}">{escape(a["cloud_id"])} · {escape(a.get("online_status") or "offline")}</option>' for a in appliances)
        camera_rows=''.join(f'<tr><td>{escape(c["name"])}</td><td><input class="setup-camera-name" data-id="{c["id"]}" value="{escape(c["name"],quote=True)}"></td><td><select class="setup-camera-site">'+''.join(f'<option value="{s["id"]}" {"selected" if s["id"]==c["site_id"] else ""}>{escape(s["name"])}</option>' for s in sites)+'</select></td><td>'+escape(c.get('status') or 'pending')+'</td></tr>' for c in cameras) or '<tr><td colspan="4">No cameras discovered or preconfigured yet.</td></tr>'
        content=f'''<header class="topbar"><div><p class="eyebrow">First-time customer onboarding</p><h1>Welcome, {escape(customer['name'])}</h1></div><form method="post" action="/partner-logout"><button class="ghost-button">Sign out</button></form></header><section class="panel"><div class="workspace-tabs" id="customer-setup-tabs" style="grid-template-columns:repeat(7,minmax(120px,1fr));overflow:auto"><button class="workspace-tab active">1 Welcome</button><button class="workspace-tab">2 Add appliance</button><button class="workspace-tab">3 Status</button><button class="workspace-tab">4 Discover</button><button class="workspace-tab">5 Cameras</button><button class="workspace-tab">6 Review</button><button class="workspace-tab">7 Confirm</button></div>
        <div class="customer-setup-step" data-step="1"><h2>Welcome to AnyAiCam</h2><p>This setup links your appliance, requests camera discovery from that appliance, and saves your camera and subscription settings.</p><div class="mock-banner">The browser does not scan the local network. Camera discovery runs on the assigned appliance.</div></div>
        <div class="customer-setup-step" data-step="2" hidden><h2>Add appliance</h2><label>Cloud ID<input id="customer-cloud-id" placeholder="AIC-XXXXXXXX"></label><label>Activation token<input id="customer-activation-token" type="password"></label><label>Or scan a provisioning QR image<input id="customer-qr-file" type="file" accept="image/*"></label><button class="action-button" id="link-customer-appliance">Link appliance</button><p id="link-message" class="health-detail"></p></div>
        <div class="customer-setup-step" data-step="3" hidden><h2>Appliance status</h2><select id="customer-appliance">{appliance_options}</select><div id="appliance-status" class="panel" style="margin-top:14px"></div></div>
        <div class="customer-setup-step" data-step="4" hidden><h2>Discover cameras</h2><p>The selected appliance performs discovery. This page only submits the job and displays its progress.</p><button class="action-button" id="start-camera-scan">Request appliance scan</button><div class="storage-bar"><span id="scan-progress" style="width:0%"></span></div><p id="scan-message" class="health-detail"></p><div id="scan-results"></div></div>
        <div class="customer-setup-step" data-step="5" hidden><h2>Camera setup</h2><div style="overflow:auto"><table class="data-table"><thead><tr><th>Camera</th><th>Rename</th><th>Location/site</th><th>Status</th></tr></thead><tbody>{camera_rows}</tbody></table></div><p class="health-detail">Recording, retention, analytics, and notifications use the selected customer plan. Per-camera overrides can be added after activation.</p><button class="ghost-button" id="save-camera-setup">Save camera setup</button></div>
        <div class="customer-setup-step" data-step="6" hidden><h2>Review subscription</h2><div id="customer-subscription-review" class="panel"><p>{escape(str(plan.get('resolution','—')).upper())} · {escape(str(plan.get('recording_mode','—')))} · {plan.get('retention_days','—')} days · {plan.get('camera_quantity',0)} cameras</p><p>Estimated monthly subscription: <strong>${float(plan.get('retail_monthly') or 0):,.2f}</strong></p><p class="health-detail">Estimate only until the order is confirmed.</p></div></div>
        <div class="customer-setup-step" data-step="7" hidden><h2>Confirm and save</h2><p>Confirm the appliance, camera assignments, recording plan, retention, analytics, and notification preferences.</p><button class="action-button" id="confirm-customer-setup">Confirm and open dashboard</button></div>
        <div class="dialog-actions"><button class="ghost-button" id="customer-setup-back" hidden>Back</button><button class="action-button" id="customer-setup-next">Save and continue</button></div></section>'''
        appliance_json=json.dumps(appliances).replace('</','<\\/')
        scripts=f'''<script>let setupStep=1,scanJob=null;const setupSteps=[...document.querySelectorAll('.customer-setup-step')],setupTabs=[...document.querySelectorAll('#customer-setup-tabs .workspace-tab')],appliances={appliance_json};function selectedAppliance(){{return document.getElementById('customer-appliance').value}}function showSetup(){{setupSteps.forEach(x=>x.hidden=Number(x.dataset.step)!==setupStep);setupTabs.forEach((x,i)=>x.classList.toggle('active',i===setupStep-1));document.getElementById('customer-setup-back').hidden=setupStep===1;document.getElementById('customer-setup-next').hidden=setupStep===7;if(setupStep===3){{const a=appliances.find(x=>x.id===selectedAppliance())||{{}};document.getElementById('appliance-status').innerHTML=`<div class="health-row"><span>Cloud ID</span><strong>${{a.cloud_id||'—'}}</strong></div><div class="health-row"><span>Software</span><strong>${{a.software_version||'Not installed'}}</strong></div><div class="health-row"><span>Status</span><strong>${{a.online_status||'offline'}}</strong></div><div class="health-row"><span>Last check-in</span><strong>${{a.last_check_in||'Never'}}</strong></div><div class="health-row"><span>Assigned site</span><strong>${{a.site_id||'—'}}</strong></div>`}}}}async function saveProgress(){{await fetch('/api/customer/setup/progress',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{current_step:setupStep,data:{{appliance_id:selectedAppliance(),scan_job:scanJob}}}})}})}}document.getElementById('customer-setup-next').onclick=async()=>{{await saveProgress();setupStep++;showSetup()}};document.getElementById('customer-setup-back').onclick=()=>{{setupStep--;showSetup()}};document.getElementById('customer-appliance').onchange=showSetup;document.getElementById('link-customer-appliance').onclick=async()=>{{const response=await fetch('/api/customer/appliances/link',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{cloud_id:document.getElementById('customer-cloud-id').value,activation_token:document.getElementById('customer-activation-token').value}})}}),r=await response.json();document.getElementById('link-message').textContent=response.ok?r.message:r.detail;if(response.ok)location.reload()}};document.getElementById('customer-qr-file').onchange=async event=>{{if(!('BarcodeDetector'in window))return showToast('QR image scanning is unavailable in this browser. Enter the Cloud ID and token manually.');const bitmap=await createImageBitmap(event.target.files[0]),codes=await new BarcodeDetector({{formats:['qr_code']}}).detect(bitmap);if(!codes.length)return showToast('No QR code found.');const parts=codes[0].rawValue.split('|');document.getElementById('customer-cloud-id').value=parts[0]||'';document.getElementById('customer-activation-token').value=parts[1]||'';showToast('QR provisioning details loaded.')}};document.getElementById('start-camera-scan').onclick=async()=>{{const response=await fetch(`/api/customer/appliances/${{selectedAppliance()}}/scan`,{{method:'POST'}}),r=await response.json();scanJob=r.job_id;document.getElementById('scan-message').textContent=r.message;document.getElementById('scan-progress').style.width=`${{r.progress}}%`;if(scanJob)setTimeout(pollScan,1200)}};async function pollScan(){{const response=await fetch(`/api/customer/camera-scans/${{scanJob}}`),r=await response.json();document.getElementById('scan-message').textContent=r.message;document.getElementById('scan-progress').style.width=`${{r.progress}}%`;document.getElementById('scan-results').innerHTML=(r.results||[]).map(x=>`<div class="health-row"><span>${{x.name||x.ip}}</span><strong>${{x.status||'discovered'}}</strong></div>`).join('');if(['queued','running'].includes(r.status))setTimeout(pollScan,1500)}}document.getElementById('save-camera-setup').onclick=async()=>{{const cameras=[...document.querySelectorAll('.setup-camera-name')].map((input,index)=>({{id:input.dataset.id,name:input.value,site_id:document.querySelectorAll('.setup-camera-site')[index].value,resolution:'{escape(str(plan.get('resolution','2mp')))}',status:'configured'}})),response=await fetch('/api/customer/cameras',{{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{cameras}})}}),r=await response.json();showToast(r.message)}};document.getElementById('confirm-customer-setup').onclick=async()=>{{const response=await fetch('/api/customer/setup/confirm',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{appliance_id:selectedAppliance()}})}}),r=await response.json();if(response.ok)location.href=r.redirect;else showToast(r.detail)}};showSetup();</script>'''
        return shell('Customer setup','users',content,scripts)

    @app.get('/api/customer/setup/status')
    def customer_setup_status(request: Request) -> dict:
        identity=customer_owner(request); customer_id=identity['customer_id']; appliances=rows('SELECT id,cloud_id,serial_number,software_version,last_check_in,online_status,ip_address,site_id,activation_status FROM appliances WHERE customer_id=?',(customer_id,)); draft=row('SELECT * FROM customer_setup_drafts WHERE customer_id=?',(customer_id,)); return {'customer_id':customer_id,'appliances':appliances,'draft':draft}

    @app.post('/api/customer/setup/progress')
    def save_customer_setup(request: Request,payload: dict) -> dict:
        identity=customer_owner(request); now=datetime.now().isoformat(); data=json.dumps(payload.get('data',{})); step=max(1,min(7,int(payload.get('current_step',1))))
        with connection() as db: db.execute('INSERT INTO customer_setup_drafts(customer_id,current_step,data_json,updated_at) VALUES(?,?,?,?) ON CONFLICT(customer_id) DO UPDATE SET current_step=excluded.current_step,data_json=excluded.data_json,updated_at=excluded.updated_at',(identity['customer_id'],step,data,now))
        return {'message':'Setup progress saved.','current_step':step}

    @app.post('/api/customer/appliances/link')
    def link_customer_appliance(request: Request,payload: dict) -> dict:
        identity=customer_owner(request)
        try: require_permission(identity,'appliance.self.link')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        cloud_id=str(payload.get('cloud_id','')).strip().upper(); token=str(payload.get('activation_token','')).strip(); appliance=row('SELECT * FROM appliances WHERE cloud_id=? AND customer_id=?',(cloud_id,identity['customer_id']))
        if not appliance: raise HTTPException(status_code=404,detail='Cloud ID was not found on this customer account.')
        if not token or not verify_password(token,appliance.get('activation_token_hash') or ''): raise HTTPException(status_code=403,detail='Activation token is invalid.')
        with connection() as db: db.execute("UPDATE appliances SET activation_status='linked' WHERE id=?",(appliance['id'],))
        audit(identity,'appliance.linked','appliance',appliance['id']); return {'message':'Appliance linked to customer account.','appliance_id':appliance['id']}

    # Real state lifecycle for a scan job -- a customer must always get
    # honest feedback, never an indefinite silent "queued". Terminal
    # states never move again; timed_out is applied lazily on read (see
    # _maybe_time_out_scan_job()) rather than via a separate scheduled
    # sweep, since every real read already has the row in hand and
    # nothing else needs to scan this table proactively.
    # Canonical vocabulary: 'queued'/'waiting_for_appliance' (customer
    # request) -> 'running' (appliance accepted, see appliance_cloud.py's
    # secure_scan_jobs()) -> 'complete'/'error' (see secure_scan_results()).
    # Previously this set said 'completed'/'failed'/'scanning', which never
    # matched what the only real appliance-side poller
    # (appliance-agent/anyaicam_agent/service.py, via appliance_cloud.py)
    # actually writes -- a job that legitimately finished as 'complete'
    # was never recognized as terminal here and got force-timed-out ~180s
    # later. Fixed to match the one canonical lifecycle.
    CAMERA_SCAN_TERMINAL_STATES={'complete','error','timed_out','cancelled'}
    CAMERA_SCAN_QUEUE_TIMEOUT_SECONDS=600   # queued/waiting_for_appliance, never picked up
    CAMERA_SCAN_ACTIVE_TIMEOUT_SECONDS=180  # running, appliance accepted but never finished

    def _maybe_time_out_scan_job(job: dict) -> dict:
        if job['status'] in CAMERA_SCAN_TERMINAL_STATES: return job
        try: updated=datetime.fromisoformat(job['updated_at'])
        except (KeyError,TypeError,ValueError): return job
        limit=CAMERA_SCAN_ACTIVE_TIMEOUT_SECONDS if job['status']=='running' else CAMERA_SCAN_QUEUE_TIMEOUT_SECONDS
        if (datetime.now()-updated).total_seconds()<=limit: return job
        message='Discovery timed out: the appliance accepted this job but never finished.' if job['status']=='running' else 'Discovery timed out: the appliance never picked up this request in time.'
        now=datetime.now().isoformat()
        with connection() as db: db.execute("UPDATE camera_scan_jobs SET status='timed_out',message=?,updated_at=? WHERE id=? AND status=?",(message,now,job['id'],job['status']))
        job['status']='timed_out'; job['message']=message; job['updated_at']=now; return job

    @app.post('/api/customer/appliances/{appliance_id}/scan')
    def request_camera_scan(request: Request,appliance_id: str) -> dict:
        identity=customer_owner(request); appliance=row('SELECT * FROM appliances WHERE id=? AND customer_id=?',(appliance_id,identity['customer_id']))
        if not appliance: raise HTTPException(status_code=404,detail='Appliance not found.')
        job_id=secrets.token_hex(6); now=datetime.now().isoformat(); online=appliance.get('online_status')=='online'; status='queued' if online else 'waiting_for_appliance'; message='Discovery request queued for the appliance.' if online else 'Appliance is offline. Discovery will begin after it checks in.'
        with connection() as db: db.execute('INSERT INTO camera_scan_jobs(id,customer_id,appliance_id,status,progress,results_json,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',(job_id,identity['customer_id'],appliance_id,status,0,'[]',message,now,now))
        audit(identity,'camera.discovery_requested','appliance',appliance_id,{'job_id':job_id}); return {'job_id':job_id,'status':status,'progress':0,'message':message}

    @app.post('/api/customer/camera-scans/{job_id}/cancel')
    def cancel_camera_scan(request: Request,job_id: str) -> dict:
        identity=customer_owner(request); job=row('SELECT * FROM camera_scan_jobs WHERE id=? AND customer_id=?',(job_id,identity['customer_id']))
        if not job: raise HTTPException(status_code=404,detail='Camera discovery job not found.')
        if job['status'] in CAMERA_SCAN_TERMINAL_STATES: return {'message':'Job already finished.','status':job['status']}
        now=datetime.now().isoformat()
        with connection() as db: db.execute("UPDATE camera_scan_jobs SET status='cancelled',message='Cancelled by customer.',updated_at=? WHERE id=?",(now,job_id))
        audit(identity,'camera.discovery_cancelled','appliance',job['appliance_id'],{'job_id':job_id}); return {'message':'Discovery cancelled.','status':'cancelled'}

    @app.get('/api/customer/camera-scans/{job_id}')
    def camera_scan_status(request: Request,job_id: str) -> dict:
        identity=customer_owner(request); job=row('SELECT * FROM camera_scan_jobs WHERE id=? AND customer_id=?',(job_id,identity['customer_id']))
        if not job: raise HTTPException(status_code=404,detail='Camera discovery job not found.')
        job=_maybe_time_out_scan_job(job)
        job['results']=json.loads(job.pop('results_json')); return job

    # NOTE: appliance-facing scan-job polling/submission (previously
    # appliance_agent() + the two /api/appliance-legacy/{cloud_id}/scan-jobs
    # routes here) has been removed. It was dead code with no caller --
    # the only real appliance-side poller (appliance-agent's service.py)
    # has always called appliance_cloud.py's /api/appliance/{cloud_id}/
    # scan-jobs routes (authenticate_appliance(), not a bearer-vs-
    # activation-token check) -- and kept the two implementations racing
    # on the same camera_scan_jobs table with two different status
    # vocabularies. See docs/AI_HANDOFF.md for the Stage 2 auth-hardening
    # note. Camera-provisioning's appliance-facing routes are similarly
    # re-homed to appliance_cloud.py below (not deleted -- provisioning
    # had no working caller yet, so nothing regresses).

    # ---- Camera provisioning: turns one selected, discovered device
    # into a real, commissioned camera. Credentials (when the camera
    # needs them) are encrypted at rest for the short window between
    # the customer submitting them and the appliance's next poll, and
    # the ciphertext is cleared the instant the appliance retrieves it
    # -- never retained longer than that wait, never logged, never
    # echoed back to the browser. See ANYAICAM_CAMERA_CREDENTIAL_KEY's
    # own comment for the encryption details.
    CAMERA_PROVISIONING_TERMINAL_STATES={'provisioned','failed'}
    CAMERA_PROVISIONING_TIMEOUT_SECONDS=300

    # Encryption itself now lives in appliance_protocol.py, shared with
    # appliance_cloud.py's decrypt-on-poll (see the import at the top of
    # this file) -- one source of truth for ANYAICAM_CAMERA_CREDENTIAL_KEY
    # instead of a duplicate copy on each side.

    def _maybe_time_out_provisioning_job(job: dict) -> dict:
        if job['status'] in CAMERA_PROVISIONING_TERMINAL_STATES: return job
        try: updated=datetime.fromisoformat(job['updated_at'])
        except (KeyError,TypeError,ValueError): return job
        if (datetime.now()-updated).total_seconds()<=CAMERA_PROVISIONING_TIMEOUT_SECONDS: return job
        message='Provisioning timed out: the appliance never confirmed this camera in time.'
        now=datetime.now().isoformat()
        with connection() as db: db.execute("UPDATE camera_provisioning_requests SET status='failed',message=?,updated_at=? WHERE id=? AND status=?",(message,now,job['id'],job['status']))
        job['status']='failed'; job['message']=message; job['updated_at']=now; return job

    @app.post('/api/customer/cameras/provision')
    def request_camera_provisioning(request: Request,payload: dict) -> dict:
        identity=customer_owner(request)
        try: require_permission(identity,'camera.self.configure')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        appliance_id=str(payload.get('appliance_id','')); device_key=str(payload.get('device_key','')).strip(); name=str(payload.get('name','')).strip() or 'Camera'
        if not appliance_id or not device_key: raise HTTPException(status_code=400,detail='appliance_id and device_key are required.')
        appliance=row('SELECT * FROM appliances WHERE id=? AND customer_id=?',(appliance_id,identity['customer_id']))
        if not appliance: raise HTTPException(status_code=404,detail='Appliance not found.')
        site_id=str(payload.get('site_id') or appliance['site_id'])
        site=row('SELECT id FROM sites WHERE id=? AND customer_id=?',(site_id,identity['customer_id']))
        if not site: raise HTTPException(status_code=404,detail='Site not found on this customer account.')
        username=payload.get('username'); password=payload.get('password'); encrypted=None
        if username or password:
            encrypted=encrypt_camera_credentials(str(username or ''),str(password or ''))
            if encrypted is None: raise HTTPException(status_code=503,detail='Camera credential handling is not configured on this deployment yet. Contact support before adding a credentialed camera.')
        job_id=secrets.token_hex(6); now=datetime.now().isoformat()
        with connection() as db:
            db.execute(
                'INSERT INTO camera_provisioning_requests(id,customer_id,appliance_id,site_id,device_key,camera_name,recording_mode,analytics_json,encrypted_credentials,status,message,created_at,updated_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (job_id,identity['customer_id'],appliance_id,site_id,device_key,name,str(payload.get('recording_mode') or 'motion'),json.dumps(payload.get('analytics') or []),encrypted,'queued','Provisioning request queued for the appliance.',now,now),
            )
        # Curated detail only -- never the raw payload, which may hold
        # username/password. audit() must never see those fields.
        audit(identity,'camera.provisioning_requested','appliance',appliance_id,{'job_id':job_id,'device_key':device_key,'name':name})
        return {'job_id':job_id,'status':'queued','message':'Provisioning request queued for the appliance.'}

    @app.get('/api/customer/camera-provisioning/{job_id}')
    def camera_provisioning_status(request: Request,job_id: str) -> dict:
        identity=customer_owner(request)
        job=row('SELECT id,status,camera_id,message,created_at,updated_at FROM camera_provisioning_requests WHERE id=? AND customer_id=?',(job_id,identity['customer_id']))
        if not job: raise HTTPException(status_code=404,detail='Provisioning job not found.')
        return _maybe_time_out_provisioning_job(job)

    # Appliance-facing provisioning-jobs routes (GET/POST) now live in
    # appliance_cloud.py, authenticated via authenticate_appliance()
    # instead of the removed appliance_agent() bearer-vs-activation-token
    # check -- see appliance_provisioning_jobs()/appliance_submit_provisioning()
    # there. Nothing previously depended on the old -legacy path (no
    # appliance-side caller existed yet), so this is a pure move.

    @app.put('/api/customer/cameras')
    def configure_customer_cameras(request: Request,payload: dict) -> dict:
        identity=customer_owner(request)
        try: require_permission(identity,'camera.self.configure')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        camera_items=payload.get('cameras',[]); now=datetime.now().isoformat()
        with connection() as db:
            for item in camera_items:
                camera_id=str(item.get('id','')); existing=db.execute('SELECT id FROM cameras WHERE id=? AND customer_id=?',(camera_id,identity['customer_id'])).fetchone()
                if existing: db.execute('UPDATE cameras SET name=?,site_id=?,resolution=?,status=? WHERE id=?',(item.get('name','Camera'),item.get('site_id'),item.get('resolution','2mp'),item.get('status','configured'),camera_id))
        audit(identity,'camera.configuration_saved','customer',identity['customer_id'],{'count':len(camera_items)}); return {'message':'Camera setup saved.'}

    @app.post('/api/customer/setup/confirm')
    def confirm_customer_setup(request: Request,payload: dict) -> dict:
        identity=customer_owner(request); appliance_id=str(payload.get('appliance_id','')); appliance=row('SELECT id FROM appliances WHERE id=? AND customer_id=?',(appliance_id,identity['customer_id']))
        if not appliance: raise HTTPException(status_code=404,detail='Appliance not found.')
        with connection() as db:
            db.execute("UPDATE appliances SET activation_status='activated' WHERE id=?",(appliance_id,)); db.execute("UPDATE customer_setup_drafts SET current_step=7,data_json=?,updated_at=? WHERE customer_id=?",(json.dumps(payload),datetime.now().isoformat(),identity['customer_id'])); db.execute("UPDATE customers SET status=CASE WHEN status='pending_installation' THEN 'trial' ELSE status END,trial_status='active' WHERE id=?",(identity['customer_id'],)); db.execute('INSERT INTO service_history(customer_id,event,details,created_at,created_by) VALUES(?,?,?,?,?)',(identity['customer_id'],'Customer setup confirmed','Appliance linked and customer setup wizard completed.',datetime.now().isoformat(),identity['email']))
        audit(identity,'customer.setup_completed','customer',identity['customer_id']); return {'message':'Setup confirmed and saved.','redirect':'/customer-account'}


def render_partner_workspace(request: Request, shell: Callable):
    identity=partner_identity(request)
    if not identity: return RedirectResponse('/partner-login',status_code=303)
    require_partner_access(request)
    partner_id=identity.get('partner_id') or 'anyaicam-primary'; customers=rows('SELECT * FROM customers WHERE partner_id=? ORDER BY created_at DESC',(partner_id,)); account=_read(ACCOUNT_FILE,{})
    customer_rows=[]
    for customer in customers:
        searchable=f'{customer.get("name","")} {customer.get("company","")} {customer.get("email","")} {customer.get("id","")}'.lower()
        site_count=row('SELECT COUNT(*) AS count FROM sites WHERE customer_id=?',(customer['id'],))['count']; plan=row('SELECT camera_quantity FROM plans WHERE customer_id=? ORDER BY created_at DESC LIMIT 1',(customer['id'],)) or {'camera_quantity':0}
        customer_rows.append(f'''<article class="customer-row" data-customer-status="{escape(customer.get('status','active'))}" data-search="{escape(searchable,quote=True)}"><div><strong>{escape(customer.get('name','Unnamed customer'))}</strong><br><small>{escape(customer.get('company',''))} · {site_count} site(s)</small></div><div>{escape(customer.get('email',''))}<br><small>{plan['camera_quantity']} cameras · {escape(customer.get('trial_status') or 'no trial')}</small></div><div><span class="pill">{escape(customer.get('status','active').replace('_',' ').title())}</span></div><div><a class="download" href="/partner/customers/{customer['id']}">Manage</a></div></article>''')
    customer_body=''.join(customer_rows) if customer_rows else '<div class="empty" id="customer-empty">No real customer records yet.</div>'
    filters=''.join(f'<label><input type="radio" name="customer-status" value="{key}" {"checked" if key=="active" else ""}> {label}</label>' for key,label in [('active','Active'),('pending_installation','Pending installation'),('trial','Trial'),('suspended','Suspended'),('cancelled','Cancelled'),('all','All')])
    tabs=[('getting-started','Getting Started'),('partner-details','Partner Details'),('customers','Customers'),('materials','Materials'),('pricing','Pricing'),('adapters','Cloud Adapters')]
    tab_buttons=''.join(f'<button class="portal-tab {"active" if key=="customers" else ""}" data-portal-tab="{key}">{label}</button>' for key,label in tabs)
    admin_link='<a class="ghost-button" href="/customer-portal">Customer Portal</a><a class="ghost-button" href="/partner-applications">Partner applications</a>' if identity['role']=='administrator' else ''
    appliances=account.get('appliances',[])
    adapter_rows=''.join(f'''<article class="customer-row"><div><strong>{escape(item.get('cloud_id','Unassigned'))}</strong><br><small>{escape(item.get('serial_number','Pending serial'))}</small></div><div>{escape(item.get('status','offline').title())}<br><small>{escape(item.get('software_version','Unknown version'))}</small></div><div><span class="pill">{escape(item.get('status','offline').title())}</span></div><div><a class="download" href="/partner/appliance-dashboard">Manage</a></div></article>''' for item in appliances) or '<div class="empty">No appliance orders or assignments yet.</div>'
    content=f'''<header class="topbar"><div><p class="eyebrow">Protected partner workspace · {escape(identity['role'])}</p><h1>Partner portal</h1></div><div class="dialog-actions">{admin_link}<form method="post" action="/partner-logout"><button class="ghost-button">Sign out</button></form></div></header><nav class="portal-tabs" aria-label="Partner portal">{tab_buttons}</nav><section class="portal-workspace">
    <div class="portal-panel" data-portal-panel="getting-started" hidden><h2>Partner onboarding</h2><div class="feature-grid" style="margin-top:20px"><article class="feature-card"><div class="feature-icon">1</div><h2>Agreement status</h2><p>Approval and agreement records are managed by AnyAiCam administration.</p><span class="pill">Approved access</span></article><article class="feature-card"><div class="feature-icon">2</div><h2>Training</h2><p>Complete sales, installation, activation, privacy, and support training.</p><span class="coming">Training checklist</span></article><article class="feature-card"><div class="feature-icon">3</div><h2>Support contacts</h2><p>Technical support, sales operations, activation help, and escalation contacts.</p><a class="download" href="/help">Open support</a></article></div><section class="panel" style="margin-top:18px"><h2>Onboarding checklist</h2><ol><li>Partner agreement approved</li><li>Authorized users confirmed</li><li>Training completed</li><li>Territory and tax information reviewed</li><li>Payout details approved</li><li>First customer installation scheduled</li></ol></section></div>
    <div class="portal-panel" data-portal-panel="partner-details" hidden><h2>Partner details</h2><div class="settings-list" style="margin-top:20px"><div class="setting-link"><div><strong>Company information</strong><div class="health-detail">Legal company name and support information</div></div></div><div class="setting-link"><div><strong>Authorized users</strong><div class="health-detail">Administrators, salespeople, technicians, and approved partners</div></div></div><div class="setting-link"><div><strong>Territory</strong><div class="health-detail">Service and sales territory</div></div></div><div class="setting-link"><div><strong>Tax information</strong><div class="health-detail">Tax and resale certificate status</div></div></div><div class="setting-link"><div><strong>Payout details</strong><div class="health-detail">Protected commission payout configuration</div></div></div><div class="setting-link"><div><strong>Approval status</strong><div class="health-detail">Approved · role: {escape(identity['role'])}</div></div></div></div></div>
    <div class="portal-panel" data-portal-panel="customers"><div class="portal-actions"><h2><span id="customer-count">{len(customers)}</span> customers</h2><button class="action-button" id="add-customer">Add New Customer</button></div><div class="portal-search-row"><input class="portal-search" id="customer-search" placeholder="Search by name, company, email, or customer ID…"><div class="status-filters">{filters}</div></div><div class="customer-list" id="customer-list">{customer_body}</div><div class="empty" id="customer-no-results" hidden>No customers match the selected filter.</div></div>
    <div class="portal-panel" data-portal-panel="materials" hidden><h2>Partner materials</h2><div class="feature-grid" style="margin-top:20px">{''.join(f'<article class="feature-card"><div class="feature-icon">▧</div><h2>{item}</h2><p>Protected partner document library.</p><button class="download" onclick="comingSoon(\'{item}\')">Open</button></article>' for item in ['Brochures','Price sheets','Installation guides','Proposal templates','Logos','Training documents'])}</div></div>
    <div class="portal-panel" data-portal-panel="pricing" hidden><h2>Partner pricing and profitability</h2><div class="feature-grid" style="margin-top:20px"><a class="feature-card" href="/partner-prices" style="color:inherit;text-decoration:none"><h2>Wholesale price sheet</h2><p>Retail, partner prices, volume tiers, and margins.</p></a><a class="feature-card" href="/partner-quotes" style="color:inherit;text-decoration:none"><h2>Quote builder</h2><p>Customer price, partner cost, and first-year profit.</p></a><a class="feature-card" href="/partner-revenue" style="color:inherit;text-decoration:none"><h2>Recurring revenue</h2><p>Monthly recurring profit and commissions.</p></a></div></div>
    <div class="portal-panel" data-portal-panel="adapters" hidden><h2>Cloud adapters and appliances</h2><div class="health-detail">Orders, serial numbers, assignments, shipping, activation, connectivity, version, and health.</div><div class="customer-list">{adapter_rows}</div></div></section>
    <dialog class="partner-dialog" id="customer-dialog" style="width:min(900px,calc(100% - 28px))"><div class="dialog-body"><div class="panel-head"><h2>New customer onboarding</h2><button class="ghost-button" type="button" id="close-customer-dialog">Close</button></div><form class="dialog-form" id="customer-form"><div class="account-grid"><label>Customer name<input id="new-name" required></label><label>Company<input id="new-company"></label><label>Email<input id="new-email" type="email" required></label><label>Phone<input id="new-phone"></label><label>Sites, one per line<textarea id="new-sites" rows="3" required>Primary site</textarea></label><label>Appliance<select id="new-appliance"><option>AnyAiCam mini PC</option><option>Customer-owned computer</option></select></label><label>Camera quantity<input id="new-quantity" type="number" min="1" max="128" value="4"></label><label>Resolution<select id="new-resolution"><option value="2mp">2MP / 1080p</option><option value="4mp">4MP</option><option value="8mp">8MP / 4K</option></select></label><label>Recording mode<select id="new-recording"><option value="motion">Motion</option><option value="continuous">Continuous</option></select></label><label>Retention<select id="new-retention"><option value="2">2 days</option><option value="7">7 days</option><option value="14">14 days</option><option value="30">30 days</option></select></label><label>Status<select id="new-status"><option value="pending_installation">Pending installation</option><option value="trial">Trial</option><option value="active">Active</option><option value="suspended">Suspended</option><option value="cancelled">Cancelled</option></select></label><label>Customer selling price per camera<input id="new-selling" type="number" min="0" step="0.01" placeholder="Defaults to retail"></label></div><fieldset><legend>Analytics per camera</legend><label><input class="new-addon" type="checkbox" value="smart_motion"> Smart Motion</label><label><input class="new-addon" type="checkbox" value="people_counting"> People Counting</label><label><input class="new-addon" type="checkbox" value="lpr"> License Plate Recognition</label><label><input class="new-addon" type="checkbox" value="ppe"> Construction PPE Monitoring</label></fieldset><button class="action-button">Calculate, quote, and create customer</button></form><section class="panel" id="onboarding-result" hidden></section></div></dialog>'''
    scripts='''<script>const portalTabs=document.querySelectorAll('[data-portal-tab]'),portalPanels=document.querySelectorAll('[data-portal-panel]');portalTabs.forEach(tab=>tab.addEventListener('click',()=>{portalTabs.forEach(x=>x.classList.remove('active'));portalPanels.forEach(p=>p.hidden=p.dataset.portalPanel!==tab.dataset.portalTab);tab.classList.add('active')}));document.getElementById('add-customer').onclick=()=>location.href='/partner/onboarding';const search=document.getElementById('customer-search'),rows=[...document.querySelectorAll('[data-customer-status]')],noResults=document.getElementById('customer-no-results');function filterCustomers(){if(!rows.length){noResults.hidden=true;return}const status=document.querySelector('[name=customer-status]:checked').value,q=search.value.trim().toLowerCase();let visible=0;rows.forEach(row=>{const show=(status==='all'||row.dataset.customerStatus===status)&&(!q||row.dataset.search.includes(q));row.hidden=!show;if(show)visible++});noResults.hidden=visible>0;document.getElementById('customer-count').textContent=visible}search.addEventListener('input',filterCustomers);document.querySelectorAll('[name=customer-status]').forEach(x=>x.addEventListener('change',filterCustomers));filterCustomers();</script>'''
    return shell('Partner portal','partner',content,scripts)
