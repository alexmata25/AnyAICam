import hashlib
import hmac
import json
import os
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime,timedelta
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from pricing_config import calculate_partner_quote, load_pricing, public_pricing, save_pricing
from partner_db import authenticate_detailed, audit, allowed, connection, password_hash
from cloud_config import settings
from cloud_security import clear_login_failures,login_blocked,record_login_failure
from customer_policy import role_destination

SESSION_COOKIE = 'anyaicam_partner_session'
SESSION_SECRETS = ([os.getenv('ANYAICAM_PORTAL_SECRET')] if os.getenv('ANYAICAM_PORTAL_SECRET') else settings.app_secrets) or [secrets.token_hex(32)]
ADMIN_EMAIL = os.getenv('ANYAICAM_ADMIN_EMAIL', '').strip().lower()
ADMIN_PASSWORD = os.getenv('ANYAICAM_ADMIN_PASSWORD', '')
try:
    PORTAL_ACCOUNTS = json.loads(os.getenv('ANYAICAM_PARTNER_ACCOUNTS', '{}'))
except json.JSONDecodeError:
    PORTAL_ACCOUNTS = {}
QUOTES_FILE = Path('/app/recordings/partner_quotes.json')
PARTNER_ROLES = {'administrator', 'partner_owner', 'salesperson', 'technician'}
AUTH_ROLES = PARTNER_ROLES | {'customer_owner', 'customer_viewer'}
CUSTOMER_LOGIN_ROLES = {'customer_owner','customer_viewer','administrator'}


def destination_for_role(role: str) -> str:
    return role_destination(role)


def _token(email: str, role: str, partner_id=None, customer_id=None,session_id=None) -> str:
    payload = json.dumps({'email': email, 'role': role, 'partner_id': partner_id, 'customer_id': customer_id, 'session_id':session_id, 'expires': int(time.time()) + 28800}, separators=(',', ':')).encode()
    encoded = urlsafe_b64encode(payload).decode().rstrip('=')
    signature = hmac.new(SESSION_SECRETS[0].encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + '.' + signature


def _identity(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE, '')
    try:
        encoded, signature = token.rsplit('.', 1)
        if not any(hmac.compare_digest(signature,hmac.new(secret.encode(),encoded.encode(),hashlib.sha256).hexdigest()) for secret in SESSION_SECRETS): return None
        payload = json.loads(urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        if payload['expires'] < time.time() or payload['role'] not in AUTH_ROLES: return None
        if payload.get('session_id'):
            with connection() as db: session=db.execute('SELECT revoked_at,expires_at FROM user_sessions WHERE id=?',(payload['session_id'],)).fetchone()
            if not session or session['revoked_at'] or session['expires_at']<datetime.now().isoformat(): return None
        return payload
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


def establish_partner_session(destination: str, *, request: Request, email: str, role: str, user: dict | None) -> RedirectResponse:
    """Writes the user_sessions row and sets the signed partner cookie
    for a freshly-authenticated Partner Portal identity, then redirects
    to destination -- the exact same two steps partner_login_submit()
    (POST /api/partner-login) already performed inline. Factored out so
    a second entry point (main.py's POST /api/portal-login, the blue
    Portal login page's Administrator/Partner/Technician selector) can
    establish an identical session without duplicating this
    security-sensitive cookie-signing logic."""
    session_id = secrets.token_hex(16)
    now = datetime.now()
    expiry = now + timedelta(hours=8)
    with connection() as db:
        db.execute(
            'INSERT INTO user_sessions(id,user_id,email,role,device_name,session_type,created_at,last_seen_at,expires_at,ip_address,user_agent) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (
                session_id, user.get('id') if user else None, email, role, 'Web browser', 'cookie',
                now.isoformat(), now.isoformat(), expiry.isoformat(),
                request.client.host if request.client else None, request.headers.get('user-agent', '')[:500],
            ),
        )
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        _token(email, role, user.get('partner_id') if user else None, user.get('customer_id') if user else None, session_id),
        httponly=True, samesite='strict', secure=settings.secure_cookies, max_age=28800, domain=settings.cookie_domain or None,
    )
    return response


def _require(request: Request, roles=PARTNER_ROLES) -> dict:
    identity = _identity(request)
    if not identity or identity['role'] not in roles:
        raise HTTPException(status_code=403, detail='Partner authorization required.')
    return identity


def partner_identity(request: Request) -> dict | None:
    return _identity(request)


def require_partner_access(request: Request, roles=PARTNER_ROLES) -> dict:
    return _require(request, roles)


def _read_quotes() -> list:
    try: return json.loads(QUOTES_FILE.read_text(encoding='utf-8')) if QUOTES_FILE.exists() else []
    except (OSError, json.JSONDecodeError): return []


def _save_quotes(quotes: list) -> None:
    QUOTES_FILE.parent.mkdir(parents=True, exist_ok=True); temp=QUOTES_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps(quotes, indent=2), encoding='utf-8'); temp.replace(QUOTES_FILE)


def register_partner_routes(app: FastAPI, shell: Callable) -> None:
    @app.get('/api/public-pricing')
    def public_prices() -> dict:
        return public_pricing()

    @app.get('/partner-login', response_class=HTMLResponse)
    def partner_login(request: Request) -> str:
        current=_identity(request)
        if current: return RedirectResponse(destination_for_role(current['role']), status_code=303)
        configured = bool(ADMIN_EMAIL and ADMIN_PASSWORD)
        note = 'Partner access is configured.' if configured else 'Set ANYAICAM_ADMIN_EMAIL and ANYAICAM_ADMIN_PASSWORD in the private server environment to activate protected access.'
        content=f'''<header class="topbar"><div><p class="eyebrow">Protected portal</p><h1>Partner sign in</h1></div></header><section class="panel" style="max-width:520px;margin:auto"><p class="health-detail">{note}</p><form class="rule-form" id="partner-login-form"><label>Email<input id="partner-email" type="email" required></label><label>Password<input id="partner-password" type="password" required></label><button class="action-button">Sign in</button><a class="download" href="/forgot-password">Forgot password?</a></form></section>'''
        scripts='''<script>document.getElementById('partner-login-form').addEventListener('submit',async e=>{e.preventDefault();const response=await fetch('/api/partner-login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.getElementById('partner-email').value,password:document.getElementById('partner-password').value})});if(response.ok){
    if(response.redirected){
        location.href=response.url;
    }else{
        location.reload();
    }
}else{
    showToast('Invalid or unconfigured partner credentials.');
}})</script>'''
        return shell('Partner login','partner-login',content,scripts)

    @app.post('/api/partner-login')
    def partner_login_submit(payload: dict,request: Request):
        email=str(payload.get('email','')).strip().lower(); password=str(payload.get('password',''))
        if login_blocked(email):
            audit({'email':email,'role':'anonymous'},'login.blocked','partner_user','',{'reason':'rate_limit'})
            raise HTTPException(status_code=429,detail='Account is temporarily locked after repeated sign-in failures. Try again later or reset your password.')
        user,reason=authenticate_detailed(email,password); role=user['role'] if user else None
        account = PORTAL_ACCOUNTS.get(email, {})
        if not role and account.get('approved') is True and account.get('role') in AUTH_ROLES and hmac.compare_digest(password, str(account.get('password',''))):
            role = account['role']
        if role and payload.get('partner_only') and role not in PARTNER_ROLES:
            audit(user or {'email':email,'role':role},'login.denied','partner_portal','',{'reason':'customer_account'})
            raise HTTPException(status_code=403,detail='Customer accounts cannot enter the Partner Portal. Use the customer sign-in page.')
        if role and payload.get('customer_only') and role not in CUSTOMER_LOGIN_ROLES:
            audit(user or {'email':email,'role':role},'login.denied','customer_portal','',{'reason':'partner_account'})
            raise HTTPException(status_code=403,detail='This account is not authorized for the Customer Portal. Use Partner Login.')
        if not role:
            record_login_failure(email)
            audit({'email':email,'role':'anonymous'},'login.failed','partner_user','',{'reason':reason})
            messages={'pending':'This account is awaiting approval.','suspended':'This account is suspended. Contact an administrator.','revoked':'This account has been revoked. Contact an administrator.','invitation_expired':'This invitation has expired. Ask an administrator for a new invitation.','invalid':'The email or password is incorrect.'}
            raise HTTPException(status_code=403, detail=messages.get(reason,'The email or password is incorrect.'))
        clear_login_failures(email)
        audit(user or {'email':email,'role':role},'login.succeeded','partner_user',user.get('id','') if user else '')
        destination='/change-password' if user and user.get('must_change_password') else ('/customer-portal' if payload.get('customer_only') and role=='administrator' else destination_for_role(role))
        return establish_partner_session(destination, request=request, email=email, role=role, user=user)

    @app.post('/partner-logout')
    def partner_logout(request: Request):
        identity=_identity(request)
        if identity and identity.get('session_id'):
            with connection() as db: db.execute('UPDATE user_sessions SET revoked_at=? WHERE id=?',(datetime.now().isoformat(),identity['session_id']))
        response=RedirectResponse('/partner.html',status_code=303); response.delete_cookie(SESSION_COOKIE,domain=settings.cookie_domain or None); return response

    @app.get('/change-password',response_class=HTMLResponse)
    def first_password_change(request: Request):
        identity=_identity(request)
        if not identity: return RedirectResponse('/partner.html',status_code=303)
        terms='<label><input id="accept-terms" type="checkbox" required> I accept the current AnyAiCam Partner Terms</label>' if identity['role'] in PARTNER_ROLES else '<input id="accept-terms" type="hidden" value="true">'
        content=f'''<header class="topbar"><div><p class="eyebrow">Account activation</p><h1>Create your permanent password</h1></div></header><section class="panel" style="max-width:560px;margin:auto"><form id="activation-password" class="rule-form"><label>New password<input id="new-password" type="password" minlength="12" required></label>{terms}<button class="action-button">Activate account</button></form></section>'''
        scripts='''<script>document.getElementById('activation-password').addEventListener('submit',async e=>{e.preventDefault();const response=await fetch('/api/partner/activate-account',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('new-password').value,accept_terms:document.getElementById('accept-terms').checked})}),result=await response.json();if(!response.ok)return showToast(result.detail);showToast(result.message);setTimeout(()=>location.href=result.destination,500)})</script>'''
        return shell('Activate partner account','partner-login',content,scripts)

    @app.post('/api/partner/activate-account')
    def activate_partner_account(request: Request,payload: dict):
        identity=_identity(request)
        if not identity: raise HTTPException(status_code=401,detail='Sign in is required.')
        password=str(payload.get('password',''))
        if len(password)<12: raise HTTPException(status_code=400,detail='Use at least 12 characters.')
        if identity['role'] in PARTNER_ROLES and not payload.get('accept_terms'): raise HTTPException(status_code=400,detail='Partner Terms must be accepted before activation.')
        now=datetime.now().isoformat(); user_id=''
        with connection() as db:
            user=db.execute('SELECT id FROM partner_users WHERE lower(email)=?',(identity['email'].lower(),)).fetchone()
            if not user: raise HTTPException(status_code=404,detail='Partner user was not found.')
            user_id=user['id']; db.execute('UPDATE partner_users SET password_hash=?,must_change_password=0,terms_accepted_at=? WHERE id=?',(password_hash(password),now,user_id))
            db.execute("UPDATE invitations SET status='accepted' WHERE lower(email)=? AND status='pending'",(identity['email'].lower(),))
            if identity['role'] in PARTNER_ROLES: db.execute('INSERT INTO partner_terms_acceptances(id,user_id,terms_version,accepted_at,ip_address) VALUES(?,?,?,?,?)',(secrets.token_hex(8),user_id,'2026-08-01',now,request.client.host if request.client else None))
        if identity['role'] in PARTNER_ROLES: audit(identity,'partner_terms.accepted','partner_user',user_id,{'version':'2026-08-01'})
        audit(identity,'password.changed','partner_user',user_id)
        return {'message':'Your partner account is active.','destination':destination_for_role(identity['role'])}

    @app.get('/api/partner-pricing')
    def partner_prices_api(request: Request) -> dict:
        identity=_require(request)
        if not allowed(identity,'pricing.view'): raise HTTPException(status_code=403,detail='Pricing permission required.')
        return load_pricing()

    @app.post('/api/partner/calculate')
    def partner_calculate(request: Request, payload: dict) -> dict:
        identity=_require(request)
        if not allowed(identity,'quote.create'): raise HTTPException(status_code=403,detail='Quote permission required.')
        try:
            result = calculate_partner_quote(payload)
            if payload.get('save_quote'):
                quotes = _read_quotes(); quotes.append({'id': secrets.token_hex(5), 'customer': payload.get('customer',''), 'site': payload.get('site',''), 'created_at': int(time.time()), **result}); _save_quotes(quotes)
            return result
        except ValueError as error: raise HTTPException(status_code=409, detail=str(error)) from error

    @app.put('/api/admin/partner-pricing')
    def admin_update(request: Request, payload: dict) -> dict:
        identity=_require(request, {'administrator'}); config=load_pricing(); partner=config['partner']
        for field in ('pricing_mode','percentage_discount','map_enabled'):
            if field in payload: partner[field]=payload[field]
        if 'trial_days' in payload: config['trial_days']=max(0,min(365,int(payload['trial_days'])))
        if 'annual_discount_percent' in payload: config['annual_discount_percent']=max(0,min(100,float(payload['annual_discount_percent'])))
        for key, values in payload.get('plan_terms',{}).items():
            if key in partner['plan_terms']: partner['plan_terms'][key].update(values)
        for key, values in payload.get('addon_terms',{}).items():
            if key in partner['addon_terms']:
                partner['addon_terms'][key].update(values)
                if values.get('retail_monthly_price') is not None: config['addons'][key]['price']=float(values['retail_monthly_price'])
        for key, value in payload.get('retail_prices',{}).items():
            resolution,recording,retention=key.split('.'); config['plans'][resolution][recording][retention]=float(value); partner['plan_terms'][key]['retail_monthly_price']=float(value)
            if key in config.get('conflicts',{}): config['conflicts'][key].update({'confirmed':True,'confirmed_value':float(value)})
        for index, values in enumerate(payload.get('volume_tiers',[])):
            if index < len(partner['volume_tiers']): partner['volume_tiers'][index]['discount_percent']=max(0,min(100,float(values.get('discount_percent',0))))
        for name in ('hardware','installation'):
            if name in payload: partner[name].update(payload[name])
        if partner.get('pricing_mode') == 'fixed':
            for terms in list(partner['plan_terms'].values()) + list(partner['addon_terms'].values()):
                retail=terms.get('retail_monthly_price'); wholesale=terms.get('partner_monthly_price')
                if retail is not None and wholesale is not None and float(wholesale) > float(retail):
                    raise HTTPException(status_code=400, detail='A fixed partner price cannot exceed its retail price.')
        save_pricing(config); audit(identity,'pricing.changed','pricing','central-config',{'pricing_mode':partner.get('pricing_mode')}); return {'status':'complete','message':'Confidential partner pricing saved.'}

    @app.get('/partner-prices', response_class=HTMLResponse)
    def partner_sheet(request: Request):
        identity=_identity(request)
        if not identity: return RedirectResponse('/partner-login',status_code=303)
        _require(request)
        if not allowed(identity,'pricing.view'): raise HTTPException(status_code=403,detail='Pricing permission required.')
        config=load_pricing(); rows=[]
        for key,term in config['partner']['plan_terms'].items():
            retail=term.get('retail_monthly_price'); wholesale=term.get('partner_monthly_price'); cost=term.get('partner_cost'); suggested=term.get('suggested_retail_price'); profit=(suggested-wholesale) if suggested is not None and wholesale is not None else None; margin=(profit/suggested*100) if profit is not None and suggested else None
            rows.append(f'<tr><td>{key}</td><td>{"—" if retail is None else f"${retail:.2f}"}</td><td>{"Not configured" if wholesale is None else f"${wholesale:.2f}"}</td><td>{"Not configured" if cost is None else f"${cost:.2f}"}</td><td>{"—" if suggested is None else f"${suggested:.2f}"}</td><td>{"—" if profit is None else f"${profit:.2f}"}</td><td>{"—" if margin is None else f"{margin:.1f}%"}</td></tr>')
        content=f'''<header class="topbar"><div><p class="eyebrow">Confidential · {identity['role']}</p><h1>Partner price sheet</h1></div><form method="post" action="/partner-logout"><button class="ghost-button">Sign out</button></form></header><div class="mock-banner">Confidential partner information. Do not share this page with retail customers.</div><section class="panel" style="overflow:auto"><table class="data-table"><thead><tr><th>Plan</th><th>Retail</th><th>Partner price</th><th>Partner cost</th><th>Suggested retail</th><th>Profit/camera</th><th>Margin</th></tr></thead><tbody>{''.join(rows)}</tbody></table></section>'''
        return shell('Partner prices','partner-prices',content)

    @app.get('/partner-quotes', response_class=HTMLResponse)
    def partner_quotes(request: Request):
        identity=_identity(request)
        if not identity: return RedirectResponse('/partner-login',status_code=303)
        _require(request)
        if not allowed(identity,'quote.create'): raise HTTPException(status_code=403,detail='Quote permission required.')
        content='''<header class="topbar"><div><p class="eyebrow">Protected partner tools</p><h1>Partner quote builder</h1></div></header><div class="mock-banner">Retail customers receive only selling prices. Partner costs and profits remain confidential.</div><section class="health-grid"><form class="panel rule-form" id="pq"><label>Customer<input id="pq-customer" required></label><label>Site<input id="pq-site" required></label><label>Resolution<select id="pq-resolution"><option value="2mp">2MP / 1080p</option><option value="4mp">4MP</option><option value="8mp">8MP / 4K</option></select></label><label>Recording<select id="pq-recording"><option value="motion">Motion</option><option value="continuous">Continuous</option></select></label><label>Retention<select id="pq-retention"><option>2</option><option>7</option><option>14</option><option>30</option></select></label><label>Cameras<input id="pq-quantity" type="number" min="1" max="128" value="4"></label><label>Customer selling price per camera<input id="pq-sell" type="number" min="0" step="0.01" placeholder="Defaults to retail"></label><label>Installation selling price<input id="pq-install" type="number" min="0" step="0.01" value="0"></label><label>Hardware selling price<input id="pq-hardware" type="number" min="0" step="0.01" value="0"></label><fieldset><legend>Analytics</legend><label><input class="pq-addon" type="checkbox" value="smart_motion"> Smart Motion</label><label><input class="pq-addon" type="checkbox" value="people_counting"> People Counting</label><label><input class="pq-addon" type="checkbox" value="lpr"> LPR</label><label><input class="pq-addon" type="checkbox" value="ppe"> PPE</label></fieldset><button class="action-button">Build confidential quote</button></form><section class="panel" id="pq-summary"><h2>Partner profitability</h2></section></section>'''
        scripts='''<script>document.getElementById('pq').addEventListener('submit',async e=>{e.preventDefault();const payload={save_quote:true,customer:document.getElementById('pq-customer').value,site:document.getElementById('pq-site').value,resolution:document.getElementById('pq-resolution').value,recording:document.getElementById('pq-recording').value,retention:Number(document.getElementById('pq-retention').value),quantity:Number(document.getElementById('pq-quantity').value),addons:[...document.querySelectorAll('.pq-addon:checked')].map(x=>x.value),installation_sell_price:Number(document.getElementById('pq-install').value),hardware_sell_price:Number(document.getElementById('pq-hardware').value)};const sell=document.getElementById('pq-sell').value;if(sell)payload.selling_price_per_camera=Number(sell);const response=await fetch('/api/partner/calculate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),r=await response.json(),box=document.getElementById('pq-summary');if(!response.ok){box.innerHTML=`<div class="mock-banner">${r.detail}</div>`;return}box.innerHTML=`<h2>Confidential profitability</h2><div class="health-row"><span>Customer monthly price</span><strong>$${r.monthly_customer_revenue.toFixed(2)}</strong></div><div class="health-row"><span>Monthly recurring profit</span><strong>$${r.monthly_recurring_profit.toFixed(2)}</strong></div><div class="health-row"><span>Installation profit</span><strong>$${r.installation_profit.toFixed(2)}</strong></div><div class="health-row"><span>Hardware profit</span><strong>$${r.hardware_profit.toFixed(2)}</strong></div><div class="health-row"><span>Total first-year profit</span><strong>$${r.first_year_profit.toFixed(2)}</strong></div><div class="health-row"><span>Partner margin</span><strong>${r.partner_margin_percent.toFixed(1)}%</strong></div><p class="health-detail">Confidential quote saved to partner revenue.</p>`})</script>'''
        return shell('Partner quotes','partner-quotes',content,scripts)

    @app.get('/partner-revenue', response_class=HTMLResponse)
    def partner_revenue(request: Request):
        identity=_identity(request)
        if not identity: return RedirectResponse('/partner-login',status_code=303)
        _require(request)
        if not allowed(identity,'pricing.view'): raise HTTPException(status_code=403,detail='Pricing permission required.')
        quotes=_read_quotes(); monthly=sum(float(q.get('monthly_recurring_profit',0)) for q in quotes); first_year=sum(float(q.get('first_year_profit',0)) for q in quotes)
        content=f'''<header class="topbar"><div><p class="eyebrow">Protected partner tools</p><h1>Commissions and recurring revenue</h1></div></header><section class="summary"><div class="stat"><span class="stat-label">Active estimates</span><span class="stat-value">{len(quotes)}</span></div><div class="stat"><span class="stat-label">Estimated monthly recurring profit</span><span class="stat-value">${monthly:,.2f}</span></div><div class="stat"><span class="stat-label">Estimated first-year profit</span><span class="stat-value">${first_year:,.2f}</span></div></section><div class="empty">Revenue appears after partner quotes are saved and approved.</div>'''
        return shell('Partner revenue','partner-revenue',content)

    @app.get('/partner-pricing-admin', response_class=HTMLResponse)
    def partner_admin(request: Request):
        identity=_identity(request)
        if not identity: return RedirectResponse('/partner-login',status_code=303)
        if identity['role']!='administrator': raise HTTPException(status_code=403,detail='Administrator role required.')
        config=load_pricing(); p=config['partner']; tiers=''.join(f'<label>{t["label"]} discount %<input class="tier" data-index="{i}" type="number" min="0" max="100" step="0.01" value="{t["discount_percent"]}"></label>' for i,t in enumerate(p['volume_tiers']))
        term_inputs=''.join(f'<div class="panel"><strong>{key}</strong><label>Retail monthly price<input class="retail-term" data-key="{key}" type="number" min="0" step="0.01" value="{term.get("retail_monthly_price") if term.get("retail_monthly_price") is not None else ""}" required></label><label>Partner monthly price<input class="partner-term" data-key="{key}" data-field="partner_monthly_price" type="number" min="0" step="0.01" value="{term.get("partner_monthly_price") if term.get("partner_monthly_price") is not None else ""}"></label><label>Partner cost<input class="partner-term" data-key="{key}" data-field="partner_cost" type="number" min="0" step="0.01" value="{term.get("partner_cost") if term.get("partner_cost") is not None else ""}"></label><label>Suggested retail<input class="partner-term" data-key="{key}" data-field="suggested_retail_price" type="number" min="0" step="0.01" value="{term.get("suggested_retail_price") if term.get("suggested_retail_price") is not None else ""}"></label><label>Minimum advertised price<input class="partner-term" data-key="{key}" data-field="minimum_advertised_price" type="number" min="0" step="0.01" value="{term.get("minimum_advertised_price") if term.get("minimum_advertised_price") is not None else ""}"></label><label><input class="partner-check" data-key="{key}" data-field="map_enabled" type="checkbox" {"checked" if term.get("map_enabled") else ""}> Enforce MAP</label></div>' for key,term in p['plan_terms'].items())
        addon_inputs=''.join(f'<div class="panel"><strong>{config["addons"][key]["label"]}</strong><label>Retail price<input class="addon-term" data-key="{key}" data-field="retail_monthly_price" type="number" step="0.01" value="{term["retail_monthly_price"]}"></label><label>Partner price<input class="addon-term" data-key="{key}" data-field="partner_monthly_price" type="number" step="0.01" value="{term.get("partner_monthly_price") if term.get("partner_monthly_price") is not None else ""}"></label><label>Partner cost<input class="addon-term" data-key="{key}" data-field="partner_cost" type="number" step="0.01" value="{term.get("partner_cost") if term.get("partner_cost") is not None else ""}"></label><label>Suggested retail<input class="addon-term" data-key="{key}" data-field="suggested_retail_price" type="number" step="0.01" value="{term.get("suggested_retail_price") if term.get("suggested_retail_price") is not None else ""}"></label><label>MAP<input class="addon-term" data-key="{key}" data-field="minimum_advertised_price" type="number" step="0.01" value="{term.get("minimum_advertised_price") if term.get("minimum_advertised_price") is not None else ""}"></label><label><input class="addon-check" data-key="{key}" data-field="map_enabled" type="checkbox" {"checked" if term.get("map_enabled") else ""}> Enforce MAP</label></div>' for key,term in p['addon_terms'].items())
        content=f'''<header class="topbar"><div><p class="eyebrow">Administrator only</p><h1>Retail and partner pricing controls</h1></div></header><form id="partner-admin" class="rule-form"><section class="panel"><label>Partner pricing method<select id="partner-mode"><option value="fixed">Fixed lower price</option><option value="percentage">Percentage discount from retail</option><option value="volume">Volume-based price</option></select></label><label>Partner discount %<input id="partner-discount" type="number" min="0" max="100" step="0.01" value="{p['percentage_discount']}"></label><label>Trial days<input id="pa-trial" type="number" value="{config['trial_days']}"></label><label>Annual discount %<input id="pa-annual" type="number" value="{config['annual_discount_percent']}"></label><div class="account-grid"><label>Hardware retail<input id="hardware-retail" type="number" step="0.01" value="{p['hardware']['retail_price']}"></label><label>Hardware partner price<input id="hardware-partner" type="number" step="0.01" value="{p['hardware'].get('partner_price') or ''}"></label><label>Hardware cost<input id="hardware-cost" type="number" step="0.01" value="{p['hardware'].get('partner_cost') or ''}"></label><label>Installation retail<input id="installation-retail" type="number" step="0.01" value="{p['installation']['retail_price']}"></label><label>Installation partner price<input id="installation-partner" type="number" step="0.01" value="{p['installation'].get('partner_price') or ''}"></label><label>Installation cost<input id="installation-cost" type="number" step="0.01" value="{p['installation'].get('partner_cost') or ''}"></label></div><h2>Volume tiers</h2>{tiers}</section><h2>Recording plans</h2><div class="account-grid">{term_inputs}</div><h2>Analytics add-ons</h2><div class="account-grid">{addon_inputs}</div><button class="action-button">Save protected pricing</button></form>'''
        scripts='''<script>document.getElementById('partner-mode').value='''+json.dumps(p['pricing_mode'])+''';document.getElementById('partner-admin').addEventListener('submit',async e=>{e.preventDefault();const n=id=>{const v=document.getElementById(id).value;return v===''?null:Number(v)},payload={pricing_mode:document.getElementById('partner-mode').value,percentage_discount:Number(document.getElementById('partner-discount').value),trial_days:Number(document.getElementById('pa-trial').value),annual_discount_percent:Number(document.getElementById('pa-annual').value),plan_terms:{},addon_terms:{},retail_prices:{},volume_tiers:[],hardware:{retail_price:n('hardware-retail'),partner_price:n('hardware-partner'),partner_cost:n('hardware-cost')},installation:{retail_price:n('installation-retail'),partner_price:n('installation-partner'),partner_cost:n('installation-cost')}};document.querySelectorAll('.retail-term').forEach(i=>payload.retail_prices[i.dataset.key]=Number(i.value));document.querySelectorAll('.partner-term').forEach(i=>{payload.plan_terms[i.dataset.key]??={};payload.plan_terms[i.dataset.key][i.dataset.field]=i.value===''?null:Number(i.value)});document.querySelectorAll('.partner-check').forEach(i=>{payload.plan_terms[i.dataset.key]??={};payload.plan_terms[i.dataset.key][i.dataset.field]=i.checked});document.querySelectorAll('.addon-term').forEach(i=>{payload.addon_terms[i.dataset.key]??={};payload.addon_terms[i.dataset.key][i.dataset.field]=i.value===''?null:Number(i.value)});document.querySelectorAll('.addon-check').forEach(i=>{payload.addon_terms[i.dataset.key]??={};payload.addon_terms[i.dataset.key][i.dataset.field]=i.checked});document.querySelectorAll('.tier').forEach(i=>payload.volume_tiers[Number(i.dataset.index)]={discount_percent:Number(i.value)});const response=await fetch('/api/admin/partner-pricing',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),result=await response.json();showToast(result.message)})</script>'''
        return shell('Partner pricing administration','partner-pricing-admin',content,scripts)
