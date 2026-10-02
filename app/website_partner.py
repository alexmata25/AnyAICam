import secrets
from datetime import datetime,timedelta
from pathlib import Path

from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse,HTMLResponse,RedirectResponse
from urllib.parse import urlsplit,urlunsplit

from cloud_config import settings
from email_service import get_email_service
from partner_db import audit,connection,password_hash,rows
from partner_portal import PARTNER_ROLES,destination_for_role,partner_identity,require_partner_access

PAGE=Path(__file__).with_name('partner.html')
CUSTOMER_LOGIN_PAGE=Path(__file__).with_name('customer-login.html')
VALID_APPLICATION_STATUSES={'pending','approved','rejected','more_information_required'}


def _clean(value,limit=500): return str(value or '').strip()[:limit]


LOGIN_PAGE_HEADERS={'Cache-Control':'no-cache'}

# Public partner applications (see PUBLIC_PATH_PREFIXES in main.py): bounded
# per client address, same limiter the public password-reset request uses.
from appliance_protocol import RateLimiter
_application_ip_limiter=RateLimiter(limit=10,window_seconds=3600)


def _portal_destination(login_url,path):
    portal=urlsplit(login_url); destination=urlsplit(path)
    return urlunsplit((portal.scheme,portal.netloc,destination.path,destination.query,''))


def _require_global_admin(request: Request) -> dict:
    """Partner portal pass (2026-09-26): partner applications (applicant contact
    details, approval) are a platform-wide admin tool. The bare 'administrator'
    role label is also carried by partner-scoped company admins, so reach
    requires a live global administrator grant, like /partner-pricing-admin."""
    identity=require_partner_access(request,{'administrator'})
    from appliance_identity import has_global_administrator_grant
    with connection() as db:
        if not has_global_administrator_grant(db,email=identity.get('email','')):
            raise HTTPException(status_code=403,detail='Platform administrator access is required.')
    return identity


def register_website_partner_routes(app: FastAPI,shell) -> None:
    @app.get('/partner.html')
    # Sign-in pages carry the login script itself: without Cache-Control a
    # browser may heuristically reuse an old copy after a deploy (seen in the
    # 2026-09-26 admin pass -- a stale partner.html missed the MFA step).
    def public_partner_page(): return FileResponse(PAGE,media_type='text/html',headers=LOGIN_PAGE_HEADERS)

    @app.get('/customer-login.html')
    def public_customer_login_page():
        return FileResponse(CUSTOMER_LOGIN_PAGE,media_type='text/html',headers=LOGIN_PAGE_HEADERS)

    @app.get('/api/website/partner-session')
    def website_session(request: Request):
        # Confirmed live on Samsung: the "Administration" nav link sent an
        # administrator to https://portal.anyaicam.com/partner?tab=
        # customers, which doesn't resolve at all from an edge appliance
        # -- settings.partner_login_url/customer_login_url resolve
        # through Settings.partner_login_url's environment-keyed lookup,
        # which is exactly right for cloud (one real public domain) but
        # wrong for edge (no fixed address; reached via whatever LAN/
        # Tailscale host the browser is actually using -- same reasoning
        # as password_reset_request()'s own edge_production fix in
        # cloud_features.py). For edge_production, every link here is
        # instead built from this exact request's own Host header.
        if settings.edge_production:
            scheme=request.headers.get('x-forwarded-proto',request.url.scheme)
            host=request.headers.get('host') or request.url.netloc
            local_base=f'{scheme}://{host}'
            public_navigation={'partner_url':local_base+'/partner.html','customer_url':local_base+'/customer-login.html'}
        else:
            local_base=None
            public_navigation={'partner_url':settings.partner_login_url,'customer_url':settings.customer_login_url}
        identity=partner_identity(request)
        if not identity: return {'authenticated':False,**public_navigation}
        experience='partner' if identity.get('role') in PARTNER_ROLES else 'customer'
        label='Administration' if identity['role']=='administrator' else ('Partner Portal' if experience=='partner' else 'My Cameras')
        # role_destination('administrator') is a fixed '/partner?tab=
        # customers' regardless of grant scope -- correct for a partner-
        # scoped (company-level) administrator, but not for a true
        # scope_type='global' administrator, who actually lands on
        # /admin-portal (see cloud_administrator_bridge()'s own docstring
        # in main.py and portal_login_submit()'s real login-redirect
        # logic). This mirrors that same distinction here so the nav
        # link matches where signing in would actually send them,
        # without touching login/session establishment itself.
        destination_path=destination_for_role(identity['role'])
        if identity['role']=='administrator':
            from appliance_identity import has_global_administrator_grant
            with connection() as db:
                if has_global_administrator_grant(db,email=identity['email']):
                    destination_path='/admin-portal'
        login_url=settings.partner_login_url if experience=='partner' else settings.customer_login_url
        portal_url=(local_base+destination_path) if settings.edge_production else _portal_destination(login_url,destination_path)
        return {'authenticated':True,'experience':experience,'role':identity['role'],'portal_url':portal_url,'navigation_label':label,**public_navigation}

    @app.post('/api/partner-applications',status_code=201)
    def submit_application(payload: dict,request: Request):
        if not _application_ip_limiter.allow(request.client.host if request.client else 'unknown'): raise HTTPException(status_code=429,detail='Too many applications from this address. Please try again later.')
        required={field:_clean(payload.get(field),200) for field in ('company_name','contact_name','email','phone','service_area','company_type')}
        if any(not value for value in required.values()): raise HTTPException(status_code=400,detail='Complete all required company and contact fields.')
        if '@' not in required['email']: raise HTTPException(status_code=400,detail='Enter a valid email address.')
        try: estimated=max(0,min(100000,int(payload.get('estimated_installations') or 0)))
        except (TypeError,ValueError): raise HTTPException(status_code=400,detail='Estimated installations must be a number.')
        application_id=secrets.token_hex(8); now=datetime.now().isoformat()
        with connection() as db:
            existing=db.execute("SELECT id FROM partner_applications WHERE lower(email)=? AND status IN ('pending','more_information_required')",(required['email'].lower(),)).fetchone()
            if existing: raise HTTPException(status_code=409,detail='An application for this email is already under review.')
            db.execute('''INSERT INTO partner_applications(id,company_name,contact_name,email,phone,website,service_area,license_information,company_type,estimated_installations,notes,status,submitted_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(application_id,required['company_name'],required['contact_name'],required['email'].lower(),required['phone'],_clean(payload.get('website'),300),required['service_area'],_clean(payload.get('license_information'),500),required['company_type'],estimated,_clean(payload.get('notes'),2000),'pending',now))
        audit({'email':required['email'].lower(),'role':'applicant'},'partner_application.submitted','partner_application',application_id,{'company_name':required['company_name'],'ip':request.client.host if request.client else None})
        return {'message':'Application received. An administrator will review it before portal access is granted.','application_id':application_id,'status':'pending'}

    @app.get('/api/admin/partner-applications')
    def list_applications(request: Request,status: str='pending'):
        _require_global_admin(request)
        if status=='all': return rows('SELECT * FROM partner_applications ORDER BY submitted_at DESC')
        if status not in VALID_APPLICATION_STATUSES: raise HTTPException(status_code=400,detail='Unknown application status.')
        return rows('SELECT * FROM partner_applications WHERE status=? ORDER BY submitted_at DESC',(status,))

    @app.get('/partner-applications',response_class=HTMLResponse)
    def applications_page(request: Request):
        identity=partner_identity(request)
        if not identity: return RedirectResponse('/partner.html',status_code=303)
        _require_global_admin(request)
        content='''<header class="topbar"><div><p class="eyebrow">Administrator only</p><h1>Partner applications</h1></div><a class="ghost-button" href="/partner">Back to Portal</a></header><section class="panel"><label>Status <select id="application-filter"><option value="pending">Pending</option><option value="more_information_required">More information required</option><option value="approved">Approved</option><option value="rejected">Rejected</option><option value="all">All</option></select></label><div id="application-list" class="activity-list"></div></section>'''
        scripts='''<script>const list=document.getElementById('application-list'),filter=document.getElementById('application-filter');async function load(){const response=await fetch('/api/admin/partner-applications?status='+filter.value),items=await response.json();list.innerHTML=items.length?items.map(item=>`<article class="panel"><div class="health-row"><div><strong>${escapeHtml(item.company_name)}</strong><div class="health-detail">${escapeHtml(item.contact_name)} · ${escapeHtml(item.email)} · ${escapeHtml(item.service_area)}</div></div><span class="status-pill">${escapeHtml(item.status.replaceAll('_',' '))}</span></div><p>${escapeHtml(item.notes||'No notes')}</p>${item.status==='pending'||item.status==='more_information_required'?`<div class="dialog-actions"><button class="action-button review" data-id="${item.id}" data-status="approved">Approve</button><button class="ghost-button review" data-id="${item.id}" data-status="more_information_required">Request information</button><button class="ghost-button review" data-id="${item.id}" data-status="rejected">Reject</button></div>`:''}</article>`).join(''):'<div class="empty">No applications in this status.</div>';document.querySelectorAll('.review').forEach(button=>button.onclick=()=>review(button))}async function review(button){const response=await fetch('/api/admin/partner-applications/'+button.dataset.id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:button.dataset.status})}),result=await response.json();if(!response.ok)return showToast(result.detail);if(result.temporary_password)alert('Invitation preview\n\n'+result.email_preview);showToast(result.message);load()}filter.onchange=load;load()</script>'''
        return shell('Partner applications','partner',content,scripts)

    @app.put('/api/admin/partner-applications/{application_id}')
    def review_application(application_id: str,payload: dict,request: Request):
        identity=_require_global_admin(request); status=_clean(payload.get('status'),40)
        if status not in VALID_APPLICATION_STATUSES-{'pending'}: raise HTTPException(status_code=400,detail='Choose approved, rejected, or more information required.')
        with connection() as db:
            application=db.execute('SELECT * FROM partner_applications WHERE id=?',(application_id,)).fetchone()
            if not application: raise HTTPException(status_code=404,detail='Partner application not found.')
            now=datetime.now().isoformat(); db.execute('UPDATE partner_applications SET status=?,reviewed_at=?,reviewed_by=? WHERE id=?',(status,now,identity['email'],application_id))
            result={'message':f'Application marked {status.replace("_"," ")}.'}
            if status=='approved':
                existing=db.execute('SELECT id FROM partner_users WHERE lower(email)=?',(application['email'].lower(),)).fetchone()
                if existing: raise HTTPException(status_code=409,detail='A portal user already exists for this email.')
                partner_id=secrets.token_hex(7); user_id=secrets.token_hex(7); invitation_id=secrets.token_hex(7)
                import account_invitations
                db.execute('INSERT INTO partners(id,name,approval_status,territory,source,created_at) VALUES(?,?,?,?,?,?)',(partner_id,application['company_name'],'approved',application['service_area'],'real',now))
                db.execute('''INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at,account_status,must_change_password)
                    VALUES(?,?,?,?,?,?,1,?,'active',0)''',(user_id,partner_id,application['email'].lower(),application['contact_name'],'partner_owner',account_invitations.unusable_password_hash(),now))
                preview=(f'Partner portal: {settings.invitation_url}\nSet your password with the personal link in this email '
                         f'(single use, valid {account_invitations.INVITE_TTL_DAYS} days), then accept the Partner Terms when you first sign in.')
                raw_invite,expiry=account_invitations.issue(db,invitation_id=invitation_id,email=application['email'].lower(),role='partner_owner',customer_id=None,
                                                            user_id=user_id,created_by=identity['email'],now=datetime.fromisoformat(now),preview=preview)
                invite_link=account_invitations.link(account_invitations.public_base(request),raw_invite)
                result.update({'message':'Partner approved and invitation created.','invitation_id':invitation_id,'email_preview':preview,'invitation_expires_at':expiry})
        audit(identity,'partner_application.'+status,'partner_application',application_id)
        if status=='approved':
            delivery=get_email_service().send('invitation',application['email'],'Your AnyAiCam Partner Portal invitation',preview+'\n\nSet your password: '+invite_link,metadata={'application_id':application_id,'invitation_id':invitation_id})
            if str((delivery or {}).get('status') or '')!='sent':
                result['invitation_link']=invite_link  # one-time manual handoff: the email did not go out
            audit(identity,'partner.approved','partner',partner_id,{'application_id':application_id}); audit(identity,'user.invited','partner_user',user_id,{'role':'partner_owner'})
        return result
