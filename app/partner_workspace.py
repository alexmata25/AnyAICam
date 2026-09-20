from cloud_config import settings as cloud_settings
import json
import logging
import secrets
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from partner_portal import partner_identity, require_partner_access, PARTNER_ROLES
from camera_install_state import camera_is_installed, camera_status_label
from pricing_config import calculate_partner_quote, calculate_quote, load_pricing
from appliance_protocol import encrypt_camera_credentials
from partner_db import audit, authorize_appliance_tenant, authorize_customer_tenant, connection, password_hash, require_permission, row, rows, verify_password
from email_service import get_email_service
from provisioning_service import get_provisioning_backend, ProvisioningBackendUnavailable

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


def _dual_mode_identity(request: Request, *, eligible_roles: set[str] = PARTNER_ROLES) -> dict:
    """Accepts a direct Partner Portal session (require_partner_access()'s
    own contract, unchanged: role must be in eligible_roles) OR, when
    there isn't one, a legacy Admin Portal session with a live,
    already-linked partner bridge -- see admin_partner_bridge.
    resolve_partner_identity_with_bridge() and main.py's
    operations_rdm_page() for the same two-step pattern used first for
    /operations/rdm. This lets an authorized administrator reuse the
    existing customer/onboarding workflow (Add New Customer) without a
    second manual Partner Portal login, exactly as the user requested
    -- it never creates a new identity or a new permission, only
    supplies one that a prior, explicit link already proved. Raises 403
    if neither a direct nor a bridged identity applies."""
    identity = partner_identity(request)
    if identity and identity.get('role') in eligible_roles:
        return identity
    if identity:
        raise HTTPException(status_code=403, detail='Your Partner Portal account type cannot use this workflow.')
    from main import current_user  # deferred: main.py imports this module at load time, so importing main.py back at module scope would be circular
    from admin_partner_bridge import resolve_partner_identity_with_bridge
    admin_user = current_user(request)
    bridged = resolve_partner_identity_with_bridge(request, admin_user, eligible_roles=eligible_roles)
    if not bridged:
        raise HTTPException(
            status_code=403,
            detail='Partner Portal access required. Sign in to the Partner Portal, or link an authorized partner '
                   'account from Operations > Remote device management.',
        )
    return bridged


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
        identity=_dual_mode_identity(request)
        try: require_permission(identity,'customer.create')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        draft_id=str(payload.get('id') or secrets.token_hex(6)); existing=row('SELECT id FROM onboarding_drafts WHERE id=? AND actor_email=?',(draft_id,identity['email']))
        with connection() as db:
            if existing: db.execute('UPDATE onboarding_drafts SET current_step=?,data_json=?,updated_at=? WHERE id=?',(int(payload.get('current_step',1)),json.dumps(payload.get('data',{})),datetime.now().isoformat(),draft_id))
            else: db.execute('INSERT INTO onboarding_drafts(id,actor_email,current_step,data_json,status,updated_at) VALUES(?,?,?,?,?,?)',(draft_id,identity['email'],int(payload.get('current_step',1)),json.dumps(payload.get('data',{})),'draft',datetime.now().isoformat()))
        return {'status':'saved','draft_id':draft_id,'message':'Onboarding progress saved.'}

    @app.get('/api/partner/onboarding/drafts/{draft_id}')
    def get_onboarding_draft(request: Request,draft_id: str) -> dict:
        identity=_dual_mode_identity(request); draft=row('SELECT * FROM onboarding_drafts WHERE id=? AND actor_email=?',(draft_id,identity['email']))
        if not draft: raise HTTPException(status_code=404,detail='Onboarding draft not found.')
        draft['data']=json.loads(draft.pop('data_json')); return draft

    @app.post('/api/partner/customers/onboard')
    def onboard_customer(request: Request, payload: dict) -> dict:
        identity=_dual_mode_identity(request)
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
        # partner_users.email is UNIQUE across every role (administrator,
        # partner staff, customer owners/viewers) -- not just customers.
        # Without this check, onboarding an email that already belongs to
        # any other portal account (confirmed live: reusing the Admin
        # account's own email for a test customer) reached the INSERT INTO
        # partner_users below and raised an uncaught sqlite3.IntegrityError,
        # producing a raw 500 with no usable detail instead of a clean,
        # actionable error -- and rolled back everything already inserted
        # in this same transaction (customer, sites, appliance, cameras,
        # plan, quote), leaving nothing behind but also giving the operator
        # no indication why.
        if row('SELECT id FROM partner_users WHERE email=?',(email,)): raise HTTPException(status_code=409,detail='This email is already associated with another account.')
        now=datetime.now().isoformat()
        # HIGH fix (2026-09-14 partner-scoped-administrator follow-up,
        # Codex tenant-isolation re-audit): identity.get('role')=='administrator'
        # is byte-identical for a true platform-global administrator and a
        # company-scoped one (see partner_db.tenant_owns_partner()'s own
        # docstring) -- a bare role check here let a partner-scoped
        # administrator steer partner_id to an arbitrary foreign partner via
        # the payload and onboard a customer directly under it. Only a
        # live-verified GLOBAL administrator grant may steer partner_id;
        # every other identity (including a company-scoped 'administrator')
        # is confined to its own tenant, exactly like every other role.
        from appliance_identity import has_global_administrator_grant
        with connection() as db:
            is_global_admin=has_global_administrator_grant(db,email=identity.get('email',''))
        if is_global_admin and str(payload.get('partner_id') or '').strip():
            partner_id=str(payload['partner_id']).strip()
        else:
            partner_id=identity.get('partner_id') or 'anyaicam-primary'
        customer_id=secrets.token_hex(5); password=_temporary_password(); quote_id=secrets.token_hex(5); invitation_id=secrets.token_hex(5); plan_id=secrets.token_hex(5)
        email_preview=f'''Subject: Welcome to AnyAiCam\n\nHello {payload.get('name','')},\nYour AnyAiCam account is ready.\nLogin: {email}\nTemporary password: {password}\nPlease change your password after signing in.'''
        created_sites=[]; activation_tokens=[]
        with connection() as db:
            db.execute('INSERT INTO customers(id,partner_id,name,company,email,phone,status,trial_status,billing_status,source,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(customer_id,partner_id,payload.get('name',''),payload.get('company',''),email,payload.get('phone',''),status,'eligible','placeholder','real',now,identity['email']))
            for site_data in sites:
                site_id=secrets.token_hex(5); db.execute('INSERT INTO sites(id,customer_id,name,address,site_type,created_at) VALUES(?,?,?,?,?,?)',(site_id,customer_id,site_data.get('name','Site'),site_data.get('address',''),site_data.get('site_type','Customer site'),now))
                # AWS-authoritative onboarding rework, Phase 1: Cloud ID and
                # activation token are no longer minted here with secrets.token_hex()/
                # secrets.token_urlsafe() -- they come from the provisioning backend
                # (AWS in production, MockProvisioningBackend for dev/test; see
                # provisioning_service.py). idempotency_key is scoped to this
                # customer+site pair so a retried provisioning call for the same
                # pair returns the same Cloud ID instead of minting a second one.
                order={
                    'customer_id':customer_id,'site_id':site_id,
                    'customer_name':payload.get('name',''),'company':payload.get('company',''),
                    'email':email,'phone':payload.get('phone',''),'status':status,
                    'site_name':site_data.get('name','Site'),
                    'appliance_type':payload.get('appliance_type','AnyAiCam mini PC'),
                    'camera_count':quote['quantity'],'resolution':quote['resolution'],
                    'recording_mode':quote['recording'],'retention_days':quote['retention_days'],
                    'analytics_addons':quote['addons'],
                    'deployment_mode':payload.get('deployment_mode','local'),
                    'order_reference':quote_id,
                }
                try: provisioning=get_provisioning_backend().provision(order,idempotency_key=f'onboarding:{customer_id}:{site_id}')
                except ProvisioningBackendUnavailable as error: raise HTTPException(status_code=503,detail='Provisioning service is temporarily unavailable; the customer was not created. Try again shortly.') from error
                cloud_id=provisioning['cloud_id']; activation=provisioning['activation_token']; appliance_id=provisioning['appliance_id']
                activation_tokens.append({'site':site_data.get('name','Site'),'cloud_id':cloud_id,'activation_token':activation,'provisioning_qr_payload':provisioning['provisioning_qr_payload']})
                db.execute('INSERT INTO appliances(id,customer_id,site_id,cloud_id,appliance_type,serial_number,software_version,last_check_in,online_status,ip_address,cpu,memory,disk,camera_capacity,activation_token_hash,activation_token_created_at,shipping_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(appliance_id,customer_id,site_id,cloud_id,payload.get('appliance_type','AnyAiCam mini PC'),payload.get('serial_number','Pending'),'Not installed',None,'offline','Not connected',0,0,0,max(16,quote['quantity']),password_hash(activation),now,'not_ordered',now))
                db.execute('INSERT INTO appliance_activation_tokens(id,appliance_id,token_hash,expires_at,created_at,created_by) VALUES(?,?,?,?,?,?)',(secrets.token_hex(6),appliance_id,password_hash(activation),(datetime.now()+timedelta(hours=24)).isoformat(),now,identity['email']))
                created_sites.append({'id':site_id,'name':site_data.get('name','Site'),'appliance_id':appliance_id})
            primary_site=created_sites[0]
            for camera_number in range(1,quote['quantity']+1): db.execute('INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,resolution,status,created_at) VALUES(?,?,?,?,?,?,?,?)',(secrets.token_hex(5),customer_id,primary_site['id'],primary_site['appliance_id'],f'Camera {camera_number}',quote['resolution'],'pending_installation',now))
            db.execute('INSERT INTO plans(id,customer_id,resolution,recording_mode,retention_days,camera_quantity,retail_monthly,partner_monthly,monthly_recurring_profit,annual_total,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(plan_id,customer_id,quote['resolution'],quote['recording'],quote['retention_days'],quote['quantity'],quote['monthly_customer_revenue'],quote['monthly_partner_charge'],quote['monthly_recurring_profit'],quote['annual_total'],'quote',now))
            for analytic in quote['addons']: db.execute('INSERT INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,status,monthly_retail,monthly_partner,created_at) VALUES(?,?,?,?,?,?,?,?)',(secrets.token_hex(5),customer_id,created_sites[0]['id'],analytic,'pending',load_pricing()['addons'][analytic]['price'],None,now))
            db.execute('INSERT INTO quotes(id,customer_id,partner_id,status,selection_json,totals_json,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)',(quote_id,customer_id,partner_id,'estimate',json.dumps(pricing_selection),json.dumps(quote),now,identity['email']))
            owner_user_id=secrets.token_hex(5)
            db.execute('INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,must_change_password) VALUES(?,?,?,?,?,?,?,?,?,1)',(owner_user_id,partner_id,email,payload.get('name','Customer'),'customer_owner',password_hash(password),1,customer_id,now))
            # Without this grant, the new owner's password is correct but
            # the appliance's cloud-delegated login (authenticate_operator()
            # in appliance_identity.py) has no live identity_grants row
            # that resolves to this customer's scope, so it denies every
            # login attempt until someone manually runs the same grant via
            # POST /api/operations/identity-grants -- see
            # docs/blockers-before-universal-release.md's "ONVIF-
            # authorization"-adjacent customer-password blocker entry for
            # the incident that surfaced this exact manual-insert gap.
            from appliance_identity import create_grant as _create_identity_grant
            _create_identity_grant(db,user_id=owner_user_id,role='customer_owner',scope_type='customer',scope_id=customer_id,granted_by=identity['email'],now=now)
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
        token=secrets.token_urlsafe(24); now=datetime.now(); now_text=now.isoformat()
        with connection() as db:
            # HIGH fix (2026-09-14 multi-tenant security remediation,
            # Codex tenant-isolation audit): this route previously only
            # verified that appliance_id existed at all -- any
            # partner_owner/technician could revoke a foreign appliance's
            # real activation tokens and receive a usable replacement
            # bearer token in the response, just by naming its id.
            # Resolved and tenant-verified BEFORE any revoke/insert/
            # update, inside this same connection, so a denied
            # cross-tenant request touches none of the foreign
            # appliance's existing tokens. Same 404 whether the id
            # doesn't exist at all or simply isn't this caller's tenant
            # -- see authorize_appliance_tenant()'s own docstring for why
            # that's deliberate (no cross-tenant existence oracle).
            if not authorize_appliance_tenant(db,identity,appliance_id):
                raise HTTPException(status_code=404,detail='Appliance not found.')
            # The real verification path (POST /api/appliance/activate,
            # appliance_cloud.py) accepts ANY appliance_activation_tokens
            # row for this appliance_id that is still unused, unrevoked,
            # and unexpired -- it never reads appliances.activation_token_
            # hash at all (that column is a stale, unread leftover from
            # before appliance_activation_tokens existed as its own
            # table). This endpoint previously only wrote that unread
            # column, so a "regenerated" token could never actually
            # activate anything, and the original onboarding-time token
            # -- the real, still-valid row -- silently remained usable
            # forever. Revoking every still-usable row for this appliance
            # before inserting the new one is what makes "invalidate the
            # previous token" actually true.
            db.execute("UPDATE appliance_activation_tokens SET revoked_at=? WHERE appliance_id=? AND used_at IS NULL AND revoked_at IS NULL",(now_text,appliance_id))
            db.execute('INSERT INTO appliance_activation_tokens(id,appliance_id,token_hash,expires_at,created_at,created_by) VALUES(?,?,?,?,?,?)',(secrets.token_hex(6),appliance_id,password_hash(token),(now+timedelta(hours=24)).isoformat(),now_text,identity['email']))
            # Kept in sync for display purposes only -- see the comment above.
            db.execute('UPDATE appliances SET activation_token_hash=?,activation_token_created_at=? WHERE id=?',(password_hash(token),now_text,appliance_id))
        audit(identity,'appliance.activation_token_generated','appliance',appliance_id); return {'activation_token':token,'message':'New one-time activation token generated. The previous token no longer works.'}

    @app.post('/api/partner/appliances/{appliance_id}/{action}')
    def appliance_action(request: Request,appliance_id: str,action: str) -> dict:
        identity=require_partner_access(request)
        try: require_permission(identity,'appliance.action')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        if action not in {'restart','update'}: raise HTTPException(status_code=400,detail='Unsupported appliance action.')
        # Directly-equivalent finding, same 2026-09-14 remediation pass:
        # this route (a placeholder -- no real hardware action is wired
        # up yet) had no tenant-ownership check of its own, letting any
        # partner_owner/technician reference a foreign appliance_id in
        # their own audit trail and receive a false "recorded" response
        # for a tenant they don't own. Fixed with the same primitive used
        # for the real RDM command-queue route above, before this real
        # action wires up hardware execution.
        with connection() as db:
            if not authorize_appliance_tenant(db,identity,appliance_id):
                raise HTTPException(status_code=404,detail='Appliance not found.')
        audit(identity,f'appliance.{action}_requested','appliance',appliance_id); return {'status':'placeholder','message':f'Appliance {action} request recorded; hardware execution is not connected yet.'}

    @app.post('/api/partner/customers/{customer_id}/notes')
    def add_customer_note(request: Request,customer_id: str,payload: dict) -> dict:
        identity=require_partner_access(request)
        try: require_permission(identity,'customer.edit')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        # HIGH fix (2026-09-14 partner-scoped-administrator follow-up,
        # Codex tenant-isolation re-audit): identity.get('role')!='administrator'
        # was a bare role-name shortcut -- byte-identical for a true
        # platform-global administrator and a company-scoped one -- that let
        # a partner-scoped administrator add notes to any foreign partner's
        # customer just by naming its id. Resolved and tenant-verified
        # BEFORE the insert, inside this same connection, via the same
        # authorize_customer_tenant() primitive the rest of this module's
        # 2026-09-14 remediation already established, so a denial (404 --
        # never confirms whether the id exists at all) creates no note row.
        with connection() as db:
            if not authorize_customer_tenant(db,identity,customer_id):
                raise HTTPException(status_code=404,detail='Customer not found.')
            note=str(payload.get('note','')).strip()
            if not note: raise HTTPException(status_code=400,detail='Note is required.')
            db.execute('INSERT INTO customer_notes(customer_id,note,created_at,created_by) VALUES(?,?,?,?)',(customer_id,note,datetime.now().isoformat(),identity['email']))
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
                if role in ('customer_owner','customer_viewer'):
                    # CRITICAL fix (2026-09-14 multi-tenant security
                    # remediation, Codex tenant-isolation audit): this
                    # route previously trusted a caller-supplied
                    # customer_id completely -- any user holding
                    # user.invite (partner_owner OR customer_owner) could
                    # mint a real customer_owner/customer_viewer identity
                    # grant for ANY customer_id, including one owned by a
                    # different partner entirely, by simply naming its
                    # id. Resolved and tenant-verified BEFORE the first
                    # INSERT, inside this same connection, so a denial
                    # (404 -- never confirms whether the id exists at all)
                    # creates no user, no grant, no invitation row: see
                    # authorize_customer_tenant()'s own docstring.
                    if not customer_id or not authorize_customer_tenant(db,identity,str(customer_id)):
                        raise HTTPException(status_code=404,detail='Customer not found.')
                db.execute('INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,must_change_password) VALUES(?,?,?,?,?,?,?,?,?,1)',(user_id,identity.get('partner_id') or 'anyaicam-primary',email,payload.get('name',''),role,password_hash(password),1,customer_id,now))
                if role in ('customer_owner','customer_viewer'):
                    # Same gap, same fix as the main onboarding flow above:
                    # an invited customer_owner/customer_viewer needs a live
                    # identity_grants row before the appliance's cloud-
                    # delegated login will ever recognize them -- see that
                    # call's own comment for the full incident this closes.
                    from appliance_identity import create_grant as _create_identity_grant
                    _create_identity_grant(db,user_id=user_id,role=role,scope_type='customer',scope_id=customer_id,granted_by=identity['email'],now=now)
                db.execute('INSERT INTO invitations(id,email,role,customer_id,status,temporary_password_hash,email_preview,expires_at,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)',(invitation_id,email,role,customer_id,'preview',password_hash(password),preview,None,now,identity['email']))
        except HTTPException:
            raise
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
        with connection() as db:
            # HIGH fix (2026-09-14 multi-tenant security remediation,
            # Codex tenant-isolation audit): this route authorized only
            # the permission itself, never that customer_id belonged to
            # the caller's own tenant -- the same gap add_customer_note()
            # above already closes for its own nearby route. Resolved and
            # tenant-verified BEFORE the insert, inside this same
            # connection, so a denial creates no plan row.
            if not authorize_customer_tenant(db,identity,customer_id):
                raise HTTPException(status_code=404,detail='Customer not found.')
            db.execute('INSERT INTO plans(id,customer_id,resolution,recording_mode,retention_days,camera_quantity,retail_monthly,partner_monthly,monthly_recurring_profit,annual_total,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(plan_id,customer_id,quote['resolution'],quote['recording'],quote['retention_days'],quote['quantity'],quote['monthly_customer_revenue'],quote['monthly_partner_charge'],quote['monthly_recurring_profit'],quote['annual_total'],'pending_confirmation',now))
        audit(identity,'plan.changed','plan',plan_id,{'customer_id':customer_id}); return {'message':'Plan change estimate saved.','quote':quote}

    @app.get('/partner/onboarding',response_class=HTMLResponse)
    def onboarding_page(request: Request):
        try:
            identity=_dual_mode_identity(request)
        except HTTPException:
            return RedirectResponse('/partner-login',status_code=303)
        try: require_permission(identity,'customer.create')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        content='''<header class="topbar"><div><p class="eyebrow">Resumable customer onboarding</p><h1>Add New Customer</h1></div><a class="ghost-button" href="/partner">Back to Partner Portal</a></header><div class="mock-banner">Progress is saved after each step. Partner pricing remains confidential.</div><section class="panel"><p class="health-detail" id="onboarding-outer-step">AnyAiCam customer setup &middot; Step <strong>1</strong> of 7 (Partner portion)</p><div class="workspace-tabs" id="wizard-tabs" data-outer-steps="1,2,3,3,3"><button class="workspace-tab active">1 Customer</button><button class="workspace-tab">2 Sites</button><button class="workspace-tab">3 What you're buying</button><button class="workspace-tab">4 Pricing</button><button class="workspace-tab">5 Review &amp; send to AWS</button></div><form id="onboarding-wizard" class="rule-form"><div class="wizard-step" data-step="1"><h2>Customer and company</h2><label>Customer owner name<input id="w-name" required></label><label>Company<input id="w-company"></label><label>Email<input id="w-email" type="email" required></label><label>Phone<input id="w-phone"></label><label>Status<select id="w-status"><option value="pending_installation">Pending installation</option><option value="trial">Trial</option><option value="active">Active</option></select></label></div><div class="wizard-step" data-step="2" hidden><h2>Sites</h2><label>Site names, one per line<textarea id="w-sites" rows="6" required>Primary site</textarea></label><p class="health-detail">Each line creates a separate site and appliance assignment.</p></div><div class="wizard-step" data-step="3" hidden><h2>Appliance and cameras</h2><label>Computer type<select id="w-appliance"><option>AnyAiCam mini PC</option><option>Customer-owned computer</option></select></label><label>Deployment<select id="w-deployment"><option value="local">Local</option><option value="hybrid">Hybrid</option><option value="cloud">Cloud</option></select></label><label>Camera quantity<input id="w-quantity" type="number" min="1" max="128" value="4"></label><label>Resolution<select id="w-resolution"><option value="2mp">2MP / 1080p</option><option value="4mp">4MP</option><option value="8mp">8MP / 4K</option></select></label><label>Recording<select id="w-recording"><option value="motion">Motion</option><option value="continuous">Continuous</option></select></label><label>Retention<select id="w-retention"><option value="2">2 days</option><option value="7">7 days</option><option value="14">14 days</option><option value="30">30 days</option></select></label><fieldset><legend>Analytics</legend><label><input class="w-addon" type="checkbox" value="smart_motion"> Smart Motion</label><label><input class="w-addon" type="checkbox" value="people_counting"> People Counting</label><label><input class="w-addon" type="checkbox" value="lpr"> LPR</label><label><input class="w-addon" type="checkbox" value="ppe"> PPE Monitoring</label></fieldset></div><div class="wizard-step" data-step="4" hidden><h2>Customer price</h2><label>Approved customer selling price per camera<input id="w-selling" type="number" min="0" step="0.01" placeholder="Defaults to retail price"></label><button class="ghost-button" id="calculate-onboarding" type="button">Calculate retail, partner price, and margin</button><div id="wizard-pricing" class="panel"></div></div><div class="wizard-step" data-step="5" hidden><h2>Review and create</h2><div id="wizard-review" class="panel"></div><button class="action-button" type="submit">Generate quote and create customer</button></div><div class="dialog-actions"><button class="ghost-button" id="wizard-back" type="button" hidden>Back</button><button class="action-button" id="wizard-next" type="button">Save and continue</button></div></form><section class="panel" id="wizard-result" hidden></section></section>'''
        scripts='''<script>let step=1,draftId=new URLSearchParams(location.search).get('draft');const form=document.getElementById('onboarding-wizard'),steps=[...document.querySelectorAll('.wizard-step')],tabs=[...document.querySelectorAll('#wizard-tabs .workspace-tab')];function data(){const selling=document.getElementById('w-selling').value;return{name:document.getElementById('w-name').value,company:document.getElementById('w-company').value,email:document.getElementById('w-email').value,phone:document.getElementById('w-phone').value,status:document.getElementById('w-status').value,sites:document.getElementById('w-sites').value.split('\\n').map(name=>({name:name.trim()})).filter(x=>x.name),appliance_type:document.getElementById('w-appliance').value,deployment_mode:document.getElementById('w-deployment').value,pricing:{resolution:document.getElementById('w-resolution').value,recording:document.getElementById('w-recording').value,retention:Number(document.getElementById('w-retention').value),quantity:Number(document.getElementById('w-quantity').value),addons:[...document.querySelectorAll('.w-addon:checked')].map(x=>x.value),...(selling?{selling_price_per_camera:Number(selling)}:{})}}}function show(){steps.forEach(x=>x.hidden=Number(x.dataset.step)!==step);tabs.forEach((x,i)=>x.classList.toggle('active',i===step-1));document.getElementById('wizard-back').hidden=step===1;document.getElementById('wizard-next').hidden=step===5;document.getElementById('onboarding-outer-step').innerHTML=`AnyAiCam customer setup &middot; Step <strong>${document.getElementById('wizard-tabs').dataset.outerSteps.split(',')[step-1]}</strong> of 7 (Partner portion)`;if(step===5)document.getElementById('wizard-review').innerHTML=`<strong>${data().name}</strong><p>${data().sites.length} site(s) · ${data().pricing.quantity} cameras · ${data().pricing.resolution.toUpperCase()} · ${data().pricing.retention} days</p>`}async function save(){const response=await fetch('/api/partner/onboarding/drafts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:draftId,current_step:step,data:data()})}),result=await response.json();draftId=result.draft_id;history.replaceState(null,'',`?draft=${draftId}`)}document.getElementById('wizard-next').onclick=async()=>{if(step===1&&(!data().name||!data().email))return showToast('Customer name and email are required.');await save();step++;show()};document.getElementById('wizard-back').onclick=()=>{step--;show()};async function calculate(){const response=await fetch('/api/partner/calculate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data().pricing)}),r=await response.json(),box=document.getElementById('wizard-pricing');box.innerHTML=response.ok?`<div class="health-row"><span>Retail monthly</span><strong>$${r.monthly_customer_revenue.toFixed(2)}</strong></div><div class="health-row"><span>Partner monthly</span><strong>$${r.monthly_partner_charge.toFixed(2)}</strong></div><div class="health-row"><span>Monthly recurring profit</span><strong>$${r.monthly_recurring_profit.toFixed(2)}</strong></div><div class="health-row"><span>First-year profit</span><strong>$${r.first_year_profit.toFixed(2)}</strong></div>`:`<div class="mock-banner">${r.detail}</div>`}document.getElementById('calculate-onboarding').onclick=calculate;form.addEventListener('submit',async e=>{e.preventDefault();await save();const payload={...data(),draft_id:draftId},response=await fetch('/api/partner/customers/onboard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),r=await response.json(),box=document.getElementById('wizard-result');box.hidden=false;if(!response.ok){box.innerHTML=`<div class="mock-banner">${r.detail}</div>`;return}const awsRows=r.activation_tokens.map(t=>`<div class="health-row"><span>${t.site}</span><strong>${t.cloud_id}</strong></div>`).join('');box.innerHTML=`<h2>Step 5 of 7 &middot; AWS provisioning result</h2><p class="health-detail">Cloud ID, activation status, and QR handoff below are issued by AWS/the provisioning backend, not generated locally.</p><div class="panel">${awsRows}</div><p class="health-detail">Give the customer their Cloud ID and activation token (or the QR image) so they can complete steps 6-7 themselves after signing in -- do not sign in as them.</p><details><summary>Activation tokens (one-time display -- hand off, do not store elsewhere)</summary>${r.activation_tokens.map(t=>`<div class="health-row"><span>${t.cloud_id}</span><strong>${t.activation_token}</strong></div>`).join('')}</details><h2>Customer created</h2><p><strong>Login:</strong> ${r.login}<br><strong>Temporary password:</strong> ${r.temporary_password}</p><pre style="white-space:pre-wrap">${r.invitation_email_preview}</pre><h2>Installation checklist</h2><ol>${r.checklist.map(x=>`<li>${x}</li>`).join('')}</ol><a class="action-button" href="/partner/customers/${r.customer.id}">Open customer record</a>`});show();</script>'''
        return shell('Customer onboarding','partner',content,scripts)

    @app.get('/partner/customers/{customer_id}',response_class=HTMLResponse)
    def customer_detail(request: Request,customer_id: str):
        identity=_dual_mode_identity(request)
        try: require_permission(identity,'customer.view')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        # HIGH fix (2026-09-14 partner-scoped-administrator follow-up,
        # Codex tenant-isolation re-audit): this route's original fix
        # (2026-09-14 multi-tenant security remediation) still compared
        # identity.get('role')!='administrator' directly -- byte-identical
        # for a true platform-global administrator and a company-scoped
        # one, so a partner-scoped administrator could still open another
        # partner's customer record just by knowing or guessing its id.
        # Replaced with authorize_customer_tenant(), the same primitive
        # this module's other routes already use, which only bypasses
        # tenant ownership for a live-verified GLOBAL administrator grant.
        # 404 (not 403) so this never confirms or denies whether an id
        # exists to a caller who isn't authorized to see it.
        with connection() as db:
            customer=authorize_customer_tenant(db,identity,customer_id)
        if not customer: raise HTTPException(status_code=404,detail='Customer not found.')
        sites=rows('SELECT * FROM sites WHERE customer_id=?',(customer_id,)); appliances=rows('SELECT * FROM appliances WHERE customer_id=?',(customer_id,)); cameras=rows('SELECT * FROM cameras WHERE customer_id=?',(customer_id,)); plans=rows('SELECT * FROM plans WHERE customer_id=? ORDER BY created_at DESC',(customer_id,)); analytics=rows('SELECT * FROM analytics_subscriptions WHERE customer_id=?',(customer_id,)); history=rows('SELECT * FROM service_history WHERE customer_id=? ORDER BY created_at DESC',(customer_id,)); notes=rows('SELECT * FROM customer_notes WHERE customer_id=? ORDER BY created_at DESC',(customer_id,))
        site_cards=''.join(f'<article class="feature-card"><h2>{escape(x["name"])}</h2><p>{escape(x.get("address") or "No address entered")}</p></article>' for x in sites) or '<div class="empty">No sites.</div>'
        appliance_rows=''.join(f'<tr><td>{escape(x["cloud_id"])}</td><td>{escape(x.get("serial_number") or "Pending")}</td><td>{escape(x.get("online_status") or "offline")}</td><td>{escape(x.get("software_version") or "Not installed")}</td><td>{escape(x.get("ip_address") or "Not connected")}</td><td>{x.get("cpu",0)} / {x.get("memory",0)} / {x.get("disk",0)}</td><td><button class="download appliance-action" data-id="{x["id"]}" data-action="restart">Restart</button> · <button class="download appliance-action" data-id="{x["id"]}" data-action="update">Update</button> · <button class="download regen-token-button" data-id="{x["id"]}">Regenerate activation token</button></td></tr>' for x in appliances) or '<tr><td colspan="7">No appliances.</td></tr>'
        current=plans[0] if plans else {}; history_html=''.join(f'<div class="activity-row"><div><strong>{escape(x["event"])}</strong><div class="health-detail">{escape(x.get("details") or "")}</div></div><span class="activity-time">{escape(x["created_at"][:19])}</span></div>' for x in history) or '<div class="empty">No service history.</div>'; notes_html=''.join(f'<div class="activity-row"><div>{escape(x["note"])}</div><span class="activity-time">{escape(x["created_at"][:19])}</span></div>' for x in notes) or '<div class="empty">No notes.</div>'
        content=f'''<header class="topbar"><div><p class="eyebrow">Real customer record</p><h1>{escape(customer['name'])}</h1><div class="health-detail">{escape(customer.get('company') or '')} · {escape(customer.get('status') or 'active')}</div></div><a class="ghost-button" href="/partner">Back to customers</a></header><section class="summary"><div class="stat"><span class="stat-label">Sites</span><span class="stat-value">{len(sites)}</span></div><div class="stat"><span class="stat-label">Cameras</span><span class="stat-value">{len(cameras)} / {current.get('camera_quantity') or 0}</span></div><div class="stat"><span class="stat-label">Trial / billing</span><span class="stat-value">{escape(customer.get('trial_status') or '—')} / {escape(customer.get('billing_status') or '—')}</span></div></section><h2>Sites</h2><div class="feature-grid" style="margin:14px 0 24px">{site_cards}</div><section class="panel" style="overflow:auto"><h2>Appliances</h2><table class="data-table"><thead><tr><th>Cloud ID</th><th>Serial</th><th>Status</th><th>Version</th><th>IP</th><th>CPU / Memory / Disk</th><th>Actions</th></tr></thead><tbody>{appliance_rows}</tbody></table><div id="new-token-display"></div></section><section class="account-grid" style="margin-top:18px"><div class="panel"><h2>Current plan</h2><p>{escape(str(current.get('resolution') or '—').upper())} · {escape(str(current.get('recording_mode') or '—'))} · {current.get('retention_days') or '—'} days</p><p>Retail ${current.get('retail_monthly') or 0:,.2f} · Partner ${current.get('partner_monthly') or 0:,.2f}</p><form id="plan-quantity-form" class="rule-form"><label>Camera entitlement (site has more cameras than the plan expects? update it here)<input id="plan-quantity" type="number" min="1" max="128" value="{current.get('camera_quantity') or 1}" required></label><button class="action-button">Update camera entitlement</button></form><p id="plan-quantity-message" class="health-detail"></p></div><div class="panel"><h2>Analytics</h2><p>{', '.join(escape(x['analytic_key'].replace('_',' ').title()) for x in analytics) or 'None selected'}</p></div><div class="panel"><h2>Service history</h2>{history_html}</div><div class="panel"><h2>Notes</h2>{notes_html}<form id="note-form" class="rule-form"><label>Add note<textarea id="customer-note" required></textarea></label><button class="action-button">Save note</button></form></div></section>'''
        scripts=f'''<script>document.querySelectorAll('.appliance-action').forEach(button=>button.onclick=async()=>{{const response=await fetch(`/api/partner/appliances/${{button.dataset.id}}/${{button.dataset.action}}`,{{method:'POST'}}),r=await response.json();showToast(r.message)}});document.querySelectorAll('.regen-token-button').forEach(button=>button.onclick=async()=>{{const response=await fetch(`/api/partner/appliances/${{button.dataset.id}}/activation-token`,{{method:'POST'}}),r=await response.json();if(!response.ok)return showToast(r.detail);document.getElementById('new-token-display').innerHTML=`<div class="mock-banner">New one-time activation token for this appliance (shown once -- the previous token no longer works; enter this into anyaicam-setup now, do not store it elsewhere):<br><strong style="user-select:all">${{r.activation_token}}</strong></div>`}});document.getElementById('note-form').addEventListener('submit',async e=>{{e.preventDefault();const response=await fetch('/api/partner/customers/{customer_id}/notes',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{note:document.getElementById('customer-note').value}})}}),r=await response.json();showToast(r.message);setTimeout(()=>location.reload(),500)}});document.getElementById('plan-quantity-form').addEventListener('submit',async e=>{{e.preventDefault();const response=await fetch('/api/partner/customers/{customer_id}/plan',{{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{resolution:{json.dumps(current.get('resolution') or '2mp')},recording:{json.dumps(current.get('recording_mode') or 'motion')},retention:{current.get('retention_days') or 2},quantity:Number(document.getElementById('plan-quantity').value)}})}}),r=await response.json(),box=document.getElementById('plan-quantity-message');if(!response.ok){{box.textContent=r.detail;return}}box.textContent='Camera entitlement updated to '+r.quote.quantity+'.';setTimeout(()=>location.reload(),800)}})</script>'''
        return shell('Customer detail','partner',content,scripts)

    @app.get('/customer-account',response_class=HTMLResponse)
    def customer_account(request: Request,appliance_id: str=''):
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

        # Multi-appliance isolation fix (2026-09-12): this page used to query
        # cameras `WHERE customer_id=?` alone, exactly the same defect class
        # 0a92bea already fixed for GET /api/customer/cameras and the setup
        # wizard's own Step 5 table -- this call site was missed in that
        # audit. Confirmed live on anyaicam-staging: a customer with two
        # appliances saw both appliances' cameras rendered as one
        # undifferentiated list, with colliding camera_number-derived labels
        # ("Camera 1" from one appliance indistinguishable from "Camera 1"
        # from the other). An explicit appliance_id (mirroring the same
        # optional, backward-compatible, own-tenant-verified parameter GET
        # /api/customer/cameras already accepts) scopes the whole page to
        # that appliance alone. This page has no appliance-selector control
        # of its own yet (no dropdown, no persisted "current appliance" --
        # see the module-level note below), so when appliance_id is omitted
        # -- every real browser request today -- cameras from more than one
        # appliance are never merged into one indistinguishable list; they
        # are grouped and labeled by their own appliance instead (see
        # `distinct_appliance_ids` below). The shared account-wide Stripe
        # entitlement (licensed_slots) is unaffected either way.
        if appliance_id:
            owned_appliance=row('SELECT id FROM appliances WHERE id=? AND customer_id=?',(appliance_id,customer['id']))
            if not owned_appliance:
                raise HTTPException(status_code=404,detail='Appliance not found.')

        # "Installed/configured" reflects real appliance-reported state
        # (appliance_camera_status, written by the appliance's own live
        # heartbeat -- POST /api/appliance/cameras), not only whether the
        # customer manually completed /customer/setup's "Save camera
        # setup" step (the only thing that ever sets cameras.status to
        # 'configured'). See camera_install_state.py's own module
        # docstring for the full trace: an installer-provisioned
        # customer, whose appliance and cameras were set up by a
        # partner/technician rather than the customer's own self-service
        # wizard, has real, online, recording cameras that must show as
        # installed without ever touching that wizard.
        all_customer_cameras=rows(
            'SELECT c.id,c.name,c.camera_number,c.status,c.device_key,c.appliance_id,'
            'a.cloud_id AS appliance_cloud_id,'
            'MAX(COALESCE(acs.online,0)) AS appliance_online,'
            'MAX(COALESCE(acs.recording,0)) AS appliance_recording '
            'FROM cameras c LEFT JOIN appliance_camera_status acs ON acs.camera_id=c.id '
            'LEFT JOIN appliances a ON a.id=c.appliance_id '
            'WHERE c.customer_id=?'+(' AND c.appliance_id=?' if appliance_id else '')+
            ' GROUP BY c.id,c.name,c.camera_number,c.status,c.device_key,c.appliance_id,a.cloud_id ORDER BY c.camera_number',
            (customer['id'],appliance_id) if appliance_id else (customer['id'],)
        )
        for camera in all_customer_cameras:
            camera['has_recording']=bool(row('SELECT 1 FROM recordings WHERE camera_id=?',(camera['id'],)))
            camera['installed']=camera_is_installed(
                camera_status=camera['status'],
                device_key=camera['device_key'],
                appliance_reported_online=bool(camera['appliance_online']),
                appliance_reported_recording=bool(camera['appliance_recording']),
                has_cloud_recording=camera['has_recording'],
            )
        cameras=[camera for camera in all_customer_cameras if camera['installed']]

        # Confirmed live: a purchased-but-undiscovered onboarding
        # placeholder (device_key=NULL, status forced to 'configured' by
        # Step 5's bulk save -- see camera_install_state.py's own module
        # docstring) rendered identically to a real camera here: "Camera
        # 1 / Configured / Live view", for an appliance that has never
        # checked in. Licensed capacity now comes from the same Stripe-
        # verified source used everywhere else (customer_entitlements.
        # total_camera_slots()), never from however many placeholder rows
        # onboarding happened to pre-create -- so a Local 1-8 purchase
        # correctly shows 8 slots, not however many placeholders exist.
        from customer_entitlements import total_camera_slots
        licensed_slots=total_camera_slots(customer['id'])
        configured_count=len(cameras)

        # An activated appliance is necessary but not sufficient: a
        # customer can have a linked, activated appliance and still
        # have zero real cameras and zero purchased slots -- that
        # customer belongs in Setup too. Once they've purchased at least
        # one camera slot (or already have a real, discovered camera),
        # they land on this dashboard instead, which now shows their
        # licensed-but-undiscovered slots honestly rather than hiding
        # them or redirecting away.
        if identity['role']=='customer_owner' and not (activated and (cameras or licensed_slots>0)):
            return RedirectResponse('/customer/setup',status_code=303)

        def _camera_card(camera: dict) -> str:
            return f'''<article class="feature-card">
                <div class="feature-icon">▣</div>
                <h2>{escape(camera.get("name") or f"Camera {camera.get('camera_number') or ''}")}</h2>
                <p>{escape(camera_status_label(
                    camera_status=camera['status'],
                    device_key=camera['device_key'],
                    appliance_reported_online=bool(camera['appliance_online']),
                    appliance_reported_recording=bool(camera['appliance_recording']),
                    has_cloud_recording=camera['has_recording'],
                ))}</p>
                <a class="action-button" href="/customer/cameras/{escape(camera['id'],quote=True)}/live">Live view</a>
            </article>'''

        # Multi-appliance isolation fix (2026-09-12), continued: with no
        # appliance_id given, `cameras` above can legitimately span more
        # than one appliance. Rather than rendering them as one
        # undifferentiated grid (the actual reported defect -- two
        # appliances' "Camera 1" looked identical and uncountable as
        # belonging to different appliances), each appliance's cameras are
        # grouped under their own full-width heading (their cloud_id) --
        # still one page, nothing hidden or merged, no camera silently
        # attributed to the wrong appliance. When appliance_id was passed,
        # or the account only has one appliance, this is a single group and
        # renders exactly as before (no heading, no visual change).
        distinct_appliance_ids=list(dict.fromkeys(c['appliance_id'] for c in cameras)) if not appliance_id else []
        if len(distinct_appliance_ids)>1:
            real_camera_cards=''.join(
                f'<div class="camera-appliance-group-heading" style="grid-column:1/-1;font-weight:600;margin-top:14px">'
                f'{escape(next((c["appliance_cloud_id"] for c in cameras if c["appliance_id"]==aid and c.get("appliance_cloud_id")),aid) or "Appliance")}</div>'
                +''.join(_camera_card(c) for c in cameras if c['appliance_id']==aid)
                for aid in distinct_appliance_ids
            )
        else:
            real_camera_cards=''.join(_camera_card(camera) for camera in cameras)
        # Every licensed slot beyond the real, discovered cameras is
        # shown honestly as unused/not-yet-discovered -- no Live view, no
        # Playback, no implication it's online or configured -- while the
        # slot itself is preserved for future discovery (nothing here
        # creates, deletes, or claims a camera row).
        placeholder_slot_cards=''.join(
            f'''<article class="feature-card feature-card-placeholder">
                <div class="feature-icon">▢</div>
                <h2>Camera slot {n}</h2>
                <p class="health-detail">Waiting for appliance discovery</p>
            </article>'''
            for n in range(configured_count+1,max(licensed_slots,configured_count)+1)
        )
        camera_cards=(real_camera_cards+placeholder_slot_cards) or '<div class="empty">No camera slots are available on this account yet.</div>'

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
                    <div class="health-detail">{configured_count} of {licensed_slots} camera{'s' if licensed_slots!=1 else ''} configured &middot; Live video is delivered through the AnyAiCam cloud relay.</div>
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
        # Provisioning audit fix: Step 6 used to show plans.* (a legacy,
        # partner-quoted per-camera/resolution/retention estimate that
        # predates the Local/Hybrid camera-slot architecture, is never
        # touched by a real Stripe purchase, and for this exact customer
        # was left over from an admin-side onboarding placeholder quote --
        # see customer_entitlements.py's own module docstring, audit
        # finding #2, for why that table can never be trusted as "what
        # this customer is billed"). LICENSING/BILLING (what Stripe
        # actually verified: hardware, camera-slot tier + licensed
        # capacity, analytics add-ons) is now shown from the same
        # authoritative sources the rest of this app already uses for
        # billing -- never re-derived here. plans.* is kept for what it's
        # still legitimately used for (recording_retention_sweep.py's real
        # retention-days enforcement) and shown separately, as RECORDING
        # CONFIGURATION, never as a dollar amount -- Stripe is the only
        # source of what this customer is actually charged.
        from customer_entitlements import get_entitlements_for_customer, total_camera_slots, PLAN_TIERS
        from hardware_orders import get_orders_for_customer
        from analytics_entitlements import get_active_analytics_for_customer, ANALYTICS_CATALOG
        entitlements=get_entitlements_for_customer(identity['customer_id'])
        camera_entitlement=next((e for e in entitlements if e['product'] in ('camera_slots_local','camera_slots_hybrid') and e['status']=='active'),None)
        licensed_slots=total_camera_slots(identity['customer_id'])
        if camera_entitlement:
            plan_type='hybrid' if camera_entitlement['product']=='camera_slots_hybrid' else 'local'
            tier_label=next((t[1] for t in PLAN_TIERS if t[0]==plan_type and t[4]==camera_entitlement['camera_slot_quantity']),f'{licensed_slots} cameras')
            camera_plan_summary=f'{plan_type.title()} {tier_label} &middot; {licensed_slots} licensed camera slots'
        else:
            camera_plan_summary='No camera-slot plan purchased yet'
        # Provisioning Phase 8: the tier list backing the "Buy camera
        # capacity" action below -- only tiers with a real, configured
        # Stripe Price ID are offered (same fail-closed discipline as
        # customer_entitlements._load_price_tier_map()), so this page
        # can never offer a tier create_camera_slot_checkout() would
        # then reject with PRICE_ID_REQUIRED.
        import os as _os
        camera_tier_options=[{'plan_type':t[0],'tier_label':t[1],'camera_slot_maximum':t[4],'monthly_retail_usd':t[5]} for t in PLAN_TIERS if _os.environ.get(t[6],'').strip()]
        hardware_orders_list=[o for o in get_orders_for_customer(identity['customer_id']) if o['status']=='paid']
        hardware_summary=', '.join(f"{escape(o['product_name'])} (paid)" for o in hardware_orders_list) or 'No hardware purchased yet'
        analytics_labels={key:label for key,label,_env in ANALYTICS_CATALOG}
        active_analytics=get_active_analytics_for_customer(identity['customer_id'])
        analytics_summary=', '.join(escape(analytics_labels.get(key,key)) for key in active_analytics) or 'None purchased'
        # Confirmed live on Samsung: refreshing this page always reset the
        # wizard to Step 1 and forgot the selected appliance, even though
        # POST /api/customer/setup/progress faithfully saves current_step
        # and appliance_id on every "Save and continue" click -- nothing
        # server-side ever read customer_setup_drafts back on page load;
        # the initial JS state (setupStep=1, and no <option selected> on
        # the appliance picker) was hardcoded regardless of saved
        # progress. Rehydrated here the same way appliance_json already
        # is: read once server-side, injected as the script's initial
        # state, no change to how progress is saved.
        draft=row('SELECT * FROM customer_setup_drafts WHERE customer_id=?',(identity['customer_id'],))
        try: draft_data=json.loads(draft['data_json']) if draft else {}
        except (TypeError,ValueError): draft_data={}
        initial_step=max(1,min(7,int(draft['current_step']))) if draft else 1
        initial_appliance_id=str(draft_data.get('appliance_id') or '')
        if not any(a['id']==initial_appliance_id for a in appliances): initial_appliance_id=appliances[0]['id'] if appliances else ''
        appliance_options=''.join(f'<option value="{a["id"]}" {"selected" if a["id"]==initial_appliance_id else ""}>{escape(a["cloud_id"])} · {escape(a.get("online_status") or "offline")}</option>' for a in appliances)
        # Multi-appliance isolation fix (2026-09-12): `cameras` above is
        # fetched by customer_id alone (shared with the Step 6 review
        # panel, which is legitimately account-wide). The Step 5 table
        # below is NOT account-wide -- it is specifically "this
        # appliance's cameras" -- so it must be scoped to whichever
        # appliance is initially selected, or a customer with more than
        # one appliance sees a different appliance's camera list (and,
        # before this fix, could save name/site edits onto it). Confirmed
        # live on anyaicam-staging: a customer whose account had an old
        # historical appliance (5 placeholder camera rows, non-null but
        # synthetic device_keys from admin-side onboarding seed data) and
        # a newly-claimed Ryzen appliance (zero real cameras) saw "5 of 8
        # cameras configured" while Ryzen selected -- the 5 stale rows
        # were never actually associated with Ryzen at all. Client-side
        # re-selection is handled separately by refreshCameraTable()
        # below, called on the appliance dropdown's own onchange.
        cameras=[c for c in cameras if c.get('appliance_id')==initial_appliance_id]
        # Provisioning audit fix: a camera row with no device_key was never
        # discovered on any appliance -- it's a purchased-slot placeholder
        # created at onboarding time (see partner_workspace.onboard_
        # customer()'s own per-quantity INSERT loop), not a physical
        # device. Shown identically to a real, provisioned camera before
        # this fix (same table, same "pending"-shaped status text), which
        # is exactly what let an unconfigured/offline appliance look like
        # it had already discovered a camera. A real, provisioned camera
        # always has a device_key (appliance_cloud.appliance_submit_
        # provisioning() is the only code path that ever sets one) --
        # that single column is what distinguishes the two here, same
        # signal /api/customer/cameras already uses for its own configured
        # count.
        camera_rows=''.join(f'<tr><td>{escape(c["name"])}</td><td><input class="setup-camera-name" data-id="{c["id"]}" value="{escape(c["name"],quote=True)}"></td><td><select class="setup-camera-site">'+''.join(f'<option value="{s["id"]}" {"selected" if s["id"]==c["site_id"] else ""}>{escape(s["name"])}</option>' for s in sites)+'</select></td><td>'+(f'<span class="pill">Licensed slot &middot; not yet discovered</span>' if not c.get('device_key') else escape(c.get('status') or 'pending'))+'</td></tr>' for c in cameras) or '<tr><td colspan="4">No cameras discovered or preconfigured yet.</td></tr>'
        # Self-service Cloud ID provisioning bridge (2026-09-12): precomputed
        # as its own variable, not inlined into the f-string below, purely to
        # avoid nesting a second triple-quoted string inside content's own
        # triple-quoted f-string. Intentionally absent (not merely hidden)
        # whenever this customer already has an appliance -- see the
        # Step 2 HTML comment just below for the full rationale.
        provision_first_appliance_panel='' if appliances else '<div id="provision-first-appliance" class="panel" style="margin-bottom:14px"><h3 style="margin-top:0">Provision your first appliance</h3><p class="health-detail">Uses your account\'s own purchased hardware order and camera-slot entitlement -- no separate purchase happens here.</p><label>Site name<input id="provision-site-name" placeholder="Main location" value="Primary site"></label><label>Site address (optional)<input id="provision-site-address" placeholder="123 Main St"></label><button class="action-button" id="provision-appliance-button">Provision appliance</button><p id="provision-message" class="health-detail"></p><div id="provision-result" hidden><p class="health-detail"><strong>Cloud ID and activation token generated below have been filled in for you.</strong> To activate the physical appliance itself, run <code>anyaicam-setup</code> on it and paste the QR value shown here when prompted (or type the Cloud ID and token manually) -- this token is shown only once.</p><label>Provisioning QR value (paste into anyaicam-setup)<input id="provision-qr-value" readonly></label></div></div>'
        content=f'''<header class="topbar"><div><p class="eyebrow">First-time customer onboarding</p><h1>Welcome, {escape(customer['name'])}</h1></div><form method="post" action="/partner-logout"><button class="ghost-button">Sign out</button></form></header><p class="health-detail" id="customer-setup-outer-step">AnyAiCam customer setup &middot; Step <strong>6</strong> of 7 (Customer portion)</p><section class="panel"><div class="workspace-tabs" id="customer-setup-tabs" style="grid-template-columns:repeat(7,minmax(120px,1fr));overflow:auto"><button class="workspace-tab active">1 Welcome</button><button class="workspace-tab">2 Add appliance</button><button class="workspace-tab">3 Status</button><button class="workspace-tab">4 Discover</button><button class="workspace-tab">5 Cameras</button><button class="workspace-tab">6 Review</button><button class="workspace-tab">7 Confirm</button></div>
        <div class="customer-setup-step" data-step="1"><h2>Welcome to AnyAiCam</h2><p>This setup links your appliance, requests camera discovery from that appliance, and saves your camera and subscription settings.</p><div class="mock-banner">The browser does not scan the local network. Camera discovery runs on the assigned appliance.</div></div>
        <div class="customer-setup-step" data-step="2" hidden><h2>Add appliance</h2>
        <!-- Self-service Cloud ID provisioning bridge (2026-09-12): a
        customer who purchased hardware + a camera-slot plan through the
        storefront and was approved via /customer-registration-requests
        (never through the partner-run "Customer onboarding" wizard)
        reached this step with no site and no appliance -- Cloud ID +
        activation token below had nothing to link against. This panel
        is the missing first step for exactly that customer: it creates
        their first site and provisions exactly one Cloud-ID appliance
        through the same authoritative provisioning_service.py backend
        the admin-run wizard already uses, sized from their own real
        camera-slot entitlement (never hardcoded). It is intentionally
        absent -- not merely hidden -- whenever this customer already has
        an appliance (admin-onboarded, or already self-provisioned), so
        it never offers to provision a second one. Cloud ID + activation
        token remain the canonical identity either way: this panel only
        auto-fills the same two fields below and never bypasses them. -->
        {provision_first_appliance_panel}
        <!-- Confirmed live: browsers/password managers were autofilling the
        signed-in customer's account EMAIL into the bare Cloud ID text input,
        because it sits immediately before a type="password" field with no
        <form> wrapper and no autocomplete hints -- indistinguishable from a
        login username/password pair to Chrome/Firefox/1Password/LastPass
        autofill heuristics. Customer email is the customer's identity;
        Cloud ID is the installation's identity, and the two must never be
        conflated (see provisioning_service.py). The decoy username/password
        pair below is the standard cross-browser mitigation: it gives
        autofill a real target to fill instead of the visible fields, off
        screen and never read by any code here. autocomplete="off" plus the
        password-manager-specific ignore attributes are defense in depth on
        top of that, not a replacement for it -- autocomplete="off" alone is
        not honored by every browser on username/password-shaped fields. -->
        <input type="text" name="username" autocomplete="username" tabindex="-1" aria-hidden="true" style="position:absolute;width:1px;height:1px;left:-9999px;opacity:0">
        <input type="password" name="password" autocomplete="current-password" tabindex="-1" aria-hidden="true" style="position:absolute;width:1px;height:1px;left:-9999px;opacity:0">
        <label>Cloud ID<input id="customer-cloud-id" name="cloud-id-not-email" placeholder="AIC-XXXXXXXX" autocomplete="off" data-lpignore="true" data-form-type="other"></label><label>Activation token<input id="customer-activation-token" type="password" name="activation-token-not-account-password" autocomplete="new-password" data-lpignore="true" data-form-type="other"></label><label>Or scan a provisioning QR image<input id="customer-qr-file" type="file" accept="image/*"></label><button class="action-button" id="link-customer-appliance">Link appliance</button><p id="link-message" class="health-detail"></p></div>
        <div class="customer-setup-step" data-step="3" hidden><h2>Appliance status</h2><select id="customer-appliance">{appliance_options}</select><div id="appliance-status" class="panel" style="margin-top:14px"></div></div>
        <div class="customer-setup-step" data-step="4" hidden><h2>Discover cameras</h2><p>The selected appliance performs discovery. This page only submits the job and displays its progress.</p><button class="action-button" id="start-camera-scan">Request appliance scan</button><div class="storage-bar"><span id="scan-progress" style="width:0%"></span></div><p id="scan-message" class="health-detail"></p><div id="scan-results"></div></div>
        <div class="customer-setup-step" data-step="5" hidden><h2>Camera setup</h2><p class="health-detail" id="camera-progress"></p><div style="overflow:auto"><table class="data-table"><thead><tr><th>Camera</th><th>Rename</th><th>Location/site</th><th>Status</th></tr></thead><tbody>{camera_rows}</tbody></table></div><p class="health-detail">Recording, retention, analytics, and notifications use the selected customer plan. Per-camera overrides can be added after activation.</p><button class="ghost-button" id="save-camera-setup">Save camera setup</button></div>
        <div class="customer-setup-step" data-step="6" hidden><h2>Review your account</h2>
        <div id="customer-subscription-review" class="panel"><h3 style="margin-top:0">Licensing &amp; billing &middot; from Stripe</h3><p>Hardware: {hardware_summary}</p><p>Camera plan: {camera_plan_summary}</p><p>Analytics: {analytics_summary}</p><p class="health-detail">This reflects what Stripe has verified for your account. Billing itself is managed entirely through Stripe, not this page.</p><div id="camera-plan-purchase"{' hidden' if camera_entitlement else ''}><label>Camera-capacity plan<select id="camera-tier-select">{''.join(f'<option value="{t["plan_type"]}|{t["tier_label"]}">{t["plan_type"].title()} {t["tier_label"]} cameras &middot; ${t["monthly_retail_usd"]}/mo</option>' for t in camera_tier_options)}</select></label><button class="action-button" id="buy-camera-plan">Buy camera capacity</button><p id="camera-plan-message" class="health-detail"></p></div></div>
        <div class="panel" style="margin-top:14px"><h3 style="margin-top:0">Local recording configuration</h3><p>{escape(str(plan.get('resolution','—')).upper())} &middot; {escape(str(plan.get('recording_mode','—')))} &middot; retention up to {plan.get('retention_days','—')} days</p><p class="health-detail">Recording length depends on your appliance's available storage, not a separate charge -- this is a configuration setting, not a purchased product.</p></div></div>
        <div class="customer-setup-step" data-step="7" hidden><h2>Confirm and save</h2><p>Confirm the appliance, camera assignments, recording plan, retention, analytics, and notification preferences.</p><button class="action-button" id="confirm-customer-setup">Confirm and open dashboard</button></div>
        <div class="dialog-actions"><button class="ghost-button" id="customer-setup-back" hidden>Back</button><button class="action-button" id="customer-setup-next">Save and continue</button></div></section>'''
        appliance_json=json.dumps(appliances).replace('</','<\\/')
        scripts=f'''<script>let setupStep={initial_step},scanJob=null;const setupSteps=[...document.querySelectorAll('.customer-setup-step')],setupTabs=[...document.querySelectorAll('#customer-setup-tabs .workspace-tab')],appliances={appliance_json};function selectedAppliance(){{return document.getElementById('customer-appliance').value}}function escapeHtml(v){{return String(v==null?'':v).replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c])}}function showSetup(){{setupSteps.forEach(x=>x.hidden=Number(x.dataset.step)!==setupStep);setupTabs.forEach((x,i)=>x.classList.toggle('active',i===setupStep-1));document.getElementById('customer-setup-back').hidden=setupStep===1;document.getElementById('customer-setup-next').hidden=setupStep===7;document.getElementById('customer-setup-outer-step').innerHTML=`AnyAiCam customer setup &middot; Step <strong>${{setupStep<=3?6:7}}</strong> of 7 (Customer portion)`;if(setupStep===5){{refreshCameraProgress()}}if(setupStep===3){{const a=appliances.find(x=>x.id===selectedAppliance())||{{}};document.getElementById('appliance-status').innerHTML=`<div class="health-row"><span>Cloud ID</span><strong>${{a.cloud_id||'—'}}</strong></div><div class="health-row"><span>Software</span><strong>${{a.software_version||'Not installed'}}</strong></div><div class="health-row"><span>Status</span><strong>${{a.online_status||'offline'}}</strong></div><div class="health-row"><span>Last check-in</span><strong>${{a.last_check_in||'Never'}}</strong></div><div class="health-row"><span>Assigned site</span><strong>${{a.site_id||'—'}}</strong></div>`}}}}async function refreshCameraProgress(){{const response=await fetch(`/api/customer/cameras?appliance_id=${{encodeURIComponent(selectedAppliance())}}`),r=await response.json();document.getElementById('camera-progress').textContent=`${{r.configured_camera_count}} of ${{r.expected_camera_count||'?'}} cameras configured`;}}
        async function loadLatestScan(){{const applianceId=selectedAppliance();document.getElementById('scan-message').textContent='';document.getElementById('scan-progress').style.width='0%';document.getElementById('scan-results').innerHTML='';scanJob=null;if(!applianceId)return;const response=await fetch(`/api/customer/appliances/${{applianceId}}/scans/latest`),r=await response.json();if(r.job_id){{scanJob=r.job_id;pollScan()}}}}
        async function saveProgress(){{await fetch('/api/customer/setup/progress',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{current_step:setupStep,data:{{appliance_id:selectedAppliance(),scan_job:scanJob}}}})}})}}document.getElementById('customer-setup-next').onclick=async()=>{{await saveProgress();setupStep++;showSetup()}};document.getElementById('customer-setup-back').onclick=()=>{{setupStep--;showSetup()}};document.getElementById('customer-appliance').onchange=async()=>{{await saveProgress();location.reload()}};const provisionApplianceButton=document.getElementById('provision-appliance-button');if(provisionApplianceButton)provisionApplianceButton.onclick=async()=>{{provisionApplianceButton.disabled=true;provisionApplianceButton.textContent='Provisioning…';const messageEl=document.getElementById('provision-message');messageEl.textContent='';messageEl.style.color='';let response,r;try{{response=await fetch('/api/customer/appliances/provision',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{site_name:document.getElementById('provision-site-name').value,site_address:document.getElementById('provision-site-address').value}})}});r=await response.json()}}catch(error){{provisionApplianceButton.disabled=false;provisionApplianceButton.textContent='Provision appliance';messageEl.style.color='#b91c1c';messageEl.textContent='Could not reach the server. Check the connection and try again.';return}}if(!response.ok){{provisionApplianceButton.disabled=false;provisionApplianceButton.textContent='Provision appliance';messageEl.style.color='#b91c1c';messageEl.textContent=r.detail||`Could not provision an appliance (error ${{response.status}}).`;return}}document.getElementById('customer-cloud-id').value=r.cloud_id||'';if(r.activation_token)document.getElementById('customer-activation-token').value=r.activation_token;const resultBox=document.getElementById('provision-result');if(resultBox){{resultBox.hidden=false;const qrField=document.getElementById('provision-qr-value');if(qrField)qrField.value=r.provisioning_qr_payload||''}}provisionApplianceButton.textContent='Provisioned';if(r.status==='already_provisioned'){{messageEl.textContent='An appliance was already provisioned for this account.';location.reload();return}}messageEl.textContent=`Provisioned Cloud ID ${{r.cloud_id}} for ${{r.camera_capacity||'?'}} camera slot(s). Cloud ID and activation token are filled in below.`;if(confirm('Appliance provisioned. Copy the Cloud ID and activation token (or the QR value) above now if you still need them for anyaicam-setup, then click OK to continue -- the appliance list will refresh.'))location.reload()}};document.getElementById('link-customer-appliance').onclick=async()=>{{const response=await fetch('/api/customer/appliances/link',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{cloud_id:document.getElementById('customer-cloud-id').value,activation_token:document.getElementById('customer-activation-token').value}})}}),r=await response.json();document.getElementById('link-message').textContent=response.ok?r.message:r.detail;if(response.ok)location.reload()}};document.getElementById('customer-qr-file').onchange=async event=>{{if(!('BarcodeDetector'in window))return showToast('QR image scanning is unavailable in this browser. Enter the Cloud ID and token manually.');const bitmap=await createImageBitmap(event.target.files[0]),codes=await new BarcodeDetector({{formats:['qr_code']}}).detect(bitmap);if(!codes.length)return showToast('No QR code found.');const parts=codes[0].rawValue.split('|');document.getElementById('customer-cloud-id').value=parts[0]||'';document.getElementById('customer-activation-token').value=parts[1]||'';showToast('QR provisioning details loaded.')}};document.getElementById('start-camera-scan').onclick=async()=>{{const scanButton=document.getElementById('start-camera-scan');scanButton.disabled=true;scanButton.textContent='Discovery in progress…';const response=await fetch(`/api/customer/appliances/${{selectedAppliance()}}/scan`,{{method:'POST'}}),r=await response.json();scanJob=r.job_id;document.getElementById('scan-message').textContent=r.message;document.getElementById('scan-progress').style.width=`${{r.progress}}%`;if(scanJob)setTimeout(pollScan,1200)}};async function pollScan(){{const response=await fetch(`/api/customer/camera-scans/${{scanJob}}`),r=await response.json();const scanButton=document.getElementById('start-camera-scan'),active=['queued','running'].includes(r.status);scanButton.disabled=active;scanButton.textContent=active?'Discovery in progress…':'Request appliance scan';document.getElementById('scan-message').textContent=r.message;document.getElementById('scan-progress').style.width=`${{r.progress}}%`;document.getElementById('scan-results').innerHTML=(r.results||[]).map(x=>{{const address=x.ip_address||x.ip||x.onvif_endpoint||'';const label=x.name||[x.manufacturer,x.model].filter(Boolean).join(' ')||'Discovered device';return `<div class="health-row" data-device-key="${{x.device_key||''}}" data-onvif="${{x.onvif_endpoint||''}}" data-ip="${{x.ip_address||x.ip||''}}" data-manufacturer="${{x.manufacturer||''}}" data-model="${{x.model||''}}"><span><strong>${{escapeHtml(label)}}</strong> &middot; ${{escapeHtml(x.manufacturer||'Unknown manufacturer')}} ${{escapeHtml(x.model||'')}} &middot; ${{escapeHtml(address||'no address reported')}}<br><span class="health-detail">Device key: <code>${{escapeHtml(x.device_key||'none')}}</code></span></span><button type="button" class="ghost-button provision-camera-button">Add this camera</button></div>`}}).join('')||'<div class="empty">No cameras found yet.</div>';if(active)setTimeout(pollScan,1500)}}function showProvisioningError(message){{const el=document.getElementById('scan-message');el.textContent=message;el.style.color='#b91c1c';showToast(message)}}
async function pollProvisioning(jobId,button){{const response=await fetch(`/api/customer/camera-provisioning/${{jobId}}`);let r;try{{r=await response.json()}}catch(error){{button.disabled=false;button.textContent='Add this camera';return showProvisioningError('The camera could not be added: no response from the server. Check the connection and try again.')}}if(!response.ok){{button.disabled=false;button.textContent='Add this camera';return showProvisioningError(r.detail||`The camera could not be added (error ${{response.status}}). Try again or contact support.`)}}if(r.status==='provisioned'){{document.getElementById('scan-message').style.color='';showToast('Camera added.');button.textContent='Added';button.disabled=true;refreshCameraProgress();return}}if(r.status==='failed'){{button.disabled=false;button.textContent='Add this camera';return showProvisioningError(r.message||'The camera could not be added. Try again.')}}setTimeout(()=>pollProvisioning(jobId,button),1500)}}
        document.getElementById('scan-results').addEventListener('click',async event=>{{const button=event.target.closest('.provision-camera-button');if(!button)return;const row=button.closest('[data-device-key]'),deviceKey=row.dataset.deviceKey;if(!deviceKey)return showToast('This device has no stable identifier and cannot be added yet.');const name=prompt('Name this camera (you can rename it later):','Camera')||'Camera';const username=prompt('Camera ONVIF/RTSP username (leave blank if none):','')||'';const password=username?(prompt('Camera ONVIF/RTSP password:','')||''):'';button.disabled=true;button.textContent='Adding…';let response,r;try{{response=await fetch('/api/customer/cameras/provision',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{appliance_id:selectedAppliance(),device_key:deviceKey,name,onvif_endpoint:row.dataset.onvif,ip_address:row.dataset.ip,manufacturer:row.dataset.manufacturer,model:row.dataset.model,username,password}})}});r=await response.json()}}catch(error){{button.disabled=false;button.textContent='Add this camera';return showProvisioningError('The camera could not be added: no response from the server. Check the connection and try again.')}}if(!response.ok){{button.disabled=false;button.textContent='Add this camera';return showProvisioningError(r.detail||`The camera could not be added (error ${{response.status}}). Try again or contact support.`)}}document.getElementById('scan-message').style.color='';document.getElementById('scan-message').textContent=r.message||'Provisioning request queued for the appliance.';pollProvisioning(r.job_id,button);}});
        document.getElementById('save-camera-setup').onclick=async()=>{{const cameras=[...document.querySelectorAll('.setup-camera-name')].map((input,index)=>({{id:input.dataset.id,name:input.value,site_id:document.querySelectorAll('.setup-camera-site')[index].value,resolution:'{escape(str(plan.get('resolution','2mp')))}',status:'configured'}})),response=await fetch('/api/customer/cameras',{{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{appliance_id:selectedAppliance(),cameras}})}}),r=await response.json();showToast(r.message)}};document.getElementById('confirm-customer-setup').onclick=async()=>{{const response=await fetch('/api/customer/setup/confirm',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{appliance_id:selectedAppliance()}})}}),r=await response.json();if(response.ok)location.href=r.redirect;else showToast(r.detail)}};
        const buyCameraPlanButton=document.getElementById('buy-camera-plan');if(buyCameraPlanButton)buyCameraPlanButton.onclick=async()=>{{const [plan_type,tier_label]=document.getElementById('camera-tier-select').value.split('|');buyCameraPlanButton.disabled=true;buyCameraPlanButton.textContent='Redirecting to Stripe…';const messageEl=document.getElementById('camera-plan-message');messageEl.textContent='';let response,r;try{{response=await fetch('/api/customer/camera-slots/checkout',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{plan_type,tier_label}})}});r=await response.json()}}catch(error){{buyCameraPlanButton.disabled=false;buyCameraPlanButton.textContent='Buy camera capacity';messageEl.textContent='Could not reach the server. Check the connection and try again.';return}}if(!response.ok){{buyCameraPlanButton.disabled=false;buyCameraPlanButton.textContent='Buy camera capacity';messageEl.textContent=r.detail||`Could not start checkout (error ${{response.status}}).`;return}}location.href=r.checkout_url}};
        showSetup();loadLatestScan();</script>'''
        return shell('Customer setup','users',content,scripts)

    @app.get('/api/customer/setup/status')
    def customer_setup_status(request: Request) -> dict:
        identity=customer_owner(request); customer_id=identity['customer_id']; appliances=rows('SELECT id,cloud_id,serial_number,software_version,last_check_in,online_status,ip_address,site_id,activation_status FROM appliances WHERE customer_id=?',(customer_id,))
        # AWS-authoritative onboarding rework, Phase 1: refresh each
        # appliance's cloud-reported fields from the provisioning backend --
        # the local `appliances` row is a cache the app can still read from
        # (and falls back to) if the backend is temporarily unreachable, not
        # the source of truth for online/offline, software version, or
        # check-in time.
        backend=get_provisioning_backend()
        for appliance in appliances:
            try: status=backend.get_status(appliance['cloud_id'])
            except ProvisioningBackendUnavailable: continue
            if not status: continue
            appliance['online_status']=status['online_status']; appliance['software_version']=status['software_version']; appliance['last_check_in']=status['last_check_in']; appliance['entitlement']=status['entitlement']
            with connection() as db: db.execute('UPDATE appliances SET online_status=?,software_version=?,last_check_in=? WHERE id=?',(status['online_status'],status['software_version'],status['last_check_in'],appliance['id']))
        draft=row('SELECT * FROM customer_setup_drafts WHERE customer_id=?',(customer_id,)); return {'customer_id':customer_id,'appliances':appliances,'draft':draft}

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
        cloud_id=str(payload.get('cloud_id','')).strip().upper(); token=str(payload.get('activation_token','')).strip()
        # AWS-authoritative onboarding rework, Phase 1: the provisioning
        # backend (AWS in production, MockProvisioningBackend for dev/test) is
        # the source of truth for whether this cloud_id/token pair is valid --
        # not the local activation_token_hash column, which is now only a
        # local cache of what the backend already confirmed at provision time.
        try: verification=get_provisioning_backend().verify_link(cloud_id,token)
        except ProvisioningBackendUnavailable as error: raise HTTPException(status_code=503,detail='Provisioning service is temporarily unavailable; try again shortly.') from error
        if not verification: raise HTTPException(status_code=403,detail='Activation token is invalid.')
        appliance=row('SELECT * FROM appliances WHERE cloud_id=? AND customer_id=?',(cloud_id,identity['customer_id']))
        if not appliance: raise HTTPException(status_code=404,detail='Cloud ID was not found on this customer account.')
        # Confirmed live (2026-09-13): the physical appliance's own POST
        # /api/appliance/activate call already sets activation_status=
        # 'activated', and every real heartbeat since (appliance_cloud.
        # heartbeat()) keeps online_status/software_version/last_check_in
        # current and real. get_provisioning_backend().verify_link()'s own
        # response above is a permanently-stale snapshot captured once at
        # provision() time (see MockProvisioningBackend.provision(): those
        # fields are written there and never updated again) -- letting it
        # overwrite an already-activated appliance's live fields would
        # silently regress a genuinely healthy, heartbeating appliance back
        # to looking offline/"Not installed" in the DB. Token identity was
        # already proven by verify_link() above regardless of this branch,
        # so this only skips the stale-field overwrite, never the security
        # check.
        if appliance.get('activation_status')!='activated':
            with connection() as db: db.execute("UPDATE appliances SET activation_status='linked',online_status=?,software_version=?,last_check_in=? WHERE id=?",(verification.get('online_status','offline'),verification.get('software_version',appliance.get('software_version')),verification.get('last_check_in'),appliance['id']))
        audit(identity,'appliance.linked','appliance',appliance['id'])
        # Provisioning Phase 2: surface the customer's current
        # customer_entitlements-derived camera-slot count on claim --
        # informational only, deliberately NOT a gate on claiming the
        # installation itself. An installation with zero purchased slots
        # can still be claimed (it will just show 0 of 0 configurable
        # cameras until a purchase grants slots); real slot enforcement
        # belongs at camera-provisioning time (PUT /api/customer/cameras),
        # the same pattern this codebase already uses for per-camera
        # analytics entitlements (see customer_analytics_panel.py's
        # assign_entitlement()). This keeps "claim an installation" and
        # "use a camera slot" as the two distinct concepts the
        # provisioning spec describes, rather than conflating them.
        from customer_entitlements import total_camera_slots
        # Provisioning Phase 8 follow-up: this -- the customer's own
        # browser-facing "claim my appliance" action -- is the approved
        # Getting Started trigger, not hardware-payment success (see
        # purchase_notifications.send_getting_started_email()'s own
        # docstring). Fires at most once per customer regardless of how
        # many appliances they ever link, via that function's own
        # per-customer idempotency key -- never gated on or triggered by
        # this being the customer's FIRST appliance. Best-effort: a
        # notification failure must never block a successful link, which
        # has already fully succeeded by this point.
        try:
            from purchase_notifications import send_getting_started_email
            send_getting_started_email(identity['customer_id'])
        except Exception:
            logging.getLogger('anyaicam.partner_workspace').exception(
                'Failed to send Getting Started email after appliance link for customer %s', identity['customer_id']
            )
        return {'message':'Appliance linked to customer account.','appliance_id':appliance['id'],'camera_slots_purchased':total_camera_slots(identity['customer_id'])}

    @app.post('/api/customer/appliances/provision')
    def provision_customer_appliance(request: Request,payload: dict) -> dict:
        """Self-service bridge for the gap this session's real customer-
        journey audit found: a customer who purchased hardware + a
        camera-slot plan through the storefront and was approved via
        /customer-registration-requests (never through the partner-run
        "Customer onboarding" wizard) reaches Customer Setup Step 2 with
        no site, no appliance, and nothing for Cloud ID + activation
        token to link against -- POST /api/customer/appliances/link's
        own precondition (an appliances row already scoped to this
        customer_id) was simply never created for them.

        Deliberately reuses the exact same authoritative mechanism
        partner_workspace.onboard_customer() already uses for the
        admin-run channel -- get_provisioning_backend().provision(), the
        one seam allowed to mint a Cloud ID/activation token (see
        provisioning_service.py's own docstring) -- rather than inventing
        a second provisioning backend or a second notion of appliance
        identity. This endpoint only supplies what a self-service
        customer has that an admin-run onboarding call already had: their
        own site details, and their own real, already-purchased
        entitlement to size camera capacity from. Everything downstream
        (Cloud ID format, activation-token hashing/expiry/single-use
        redemption via the unmodified POST /api/appliance/activate, the
        provisioning_qr_payload shape, first_enroll()/coordinated_
        reenroll()) is completely unchanged -- this is a new front door
        onto the existing house, not a new house.

        Cloud ID remains canonical; the separate claim-code flow
        (appliance_claims.py, /customer/claim-appliance,
        anyaicam-setup --claim) is untouched by this endpoint and stays
        available as its own additive bootstrap option -- see this
        session's own architecture-decision checkpoint entry."""
        identity=customer_owner(request)
        try: require_permission(identity,'appliance.self.link')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        customer_id=identity['customer_id']

        # Idempotent short-circuit: nothing to do if this customer already
        # has an appliance, from this action or any other (admin
        # onboarding, a prior successful call here). Never re-provision,
        # never re-expose a token that may already have been consumed.
        existing=row('SELECT * FROM appliances WHERE customer_id=? ORDER BY created_at ASC LIMIT 1',(customer_id,))
        if existing:
            return {'status':'already_provisioned','message':'An appliance has already been provisioned for this account.',
                    'appliance_id':existing['id'],'cloud_id':existing['cloud_id'],'site_id':existing['site_id']}

        from hardware_orders import get_orders_for_customer
        if not any(o['status']=='paid' for o in get_orders_for_customer(customer_id)):
            raise HTTPException(status_code=403,detail='No paid hardware order found for this account. Purchase hardware before provisioning an appliance.')

        # total_camera_slots() is the one authoritative source for camera
        # capacity anywhere in this codebase (customer_entitlements.py's
        # own docstring: "never a hard-coded constant") -- never a
        # payload-supplied quantity, never a partner-typed plans.*
        # column. A customer with an 8-slot Local entitlement gets
        # exactly 8 camera placeholders; a different customer's own
        # purchase drives their own number identically.
        from customer_entitlements import total_camera_slots
        camera_count=total_camera_slots(customer_id)
        if camera_count<1:
            raise HTTPException(status_code=403,detail='No active camera-slot entitlement found for this account. Purchase a camera plan before provisioning an appliance.')

        customer=row('SELECT * FROM customers WHERE id=?',(customer_id,))
        if not customer: raise HTTPException(status_code=404,detail='Customer account not found.')

        site_name=str(payload.get('site_name') or '').strip() or 'Primary site'
        site_address=str(payload.get('site_address') or '').strip()
        candidate_site_id=secrets.token_hex(5)
        now=datetime.now().isoformat()
        order={
            'customer_id':customer_id,'site_id':candidate_site_id,
            'customer_name':customer.get('name',''),'company':customer.get('company',''),
            'email':customer.get('email',''),'phone':customer.get('phone',''),'status':customer.get('status','active'),
            'site_name':site_name,
            'appliance_type':'AnyAiCam mini PC',
            'camera_count':camera_count,'resolution':'2mp','recording_mode':'motion','retention_days':30,
            'analytics_addons':[],'deployment_mode':'local','order_reference':f'self-service:{customer_id}',
        }
        # idempotency_key is scoped to this customer alone (never the
        # candidate_site_id above, which is only ever used on a genuine
        # first call) -- exactly so a retry after any partial failure
        # below calls provision() again and gets back the SAME cloud_id/
        # appliance_id/site_id/token already on record, never a second
        # Cloud ID for one customer's one self-service appliance.
        try: provisioning=get_provisioning_backend().provision(order,idempotency_key=f'self-service-provision:{customer_id}')
        except ProvisioningBackendUnavailable as error: raise HTTPException(status_code=503,detail='Provisioning service is temporarily unavailable. Try again shortly.') from error

        cloud_id=provisioning['cloud_id']; activation_token=provisioning['activation_token']; appliance_id=provisioning['appliance_id']; site_id=provisioning['site_id']

        # Every INSERT below is guarded by a fresh existence check
        # immediately before it, inside the same transaction -- not
        # because SQLite itself needs it, but because a retry that
        # reached provision() again (the branch above already returned
        # the SAME ids from the backend's own idempotency_index) must
        # never attempt a duplicate-primary-key insert for a row a prior,
        # partially-failed attempt already committed.
        with connection() as db:
            if not row('SELECT id FROM sites WHERE id=?',(site_id,)):
                db.execute('INSERT INTO sites(id,customer_id,name,address,site_type,created_at) VALUES(?,?,?,?,?,?)',(site_id,customer_id,site_name,site_address,'Customer site',now))
            if not row('SELECT id FROM appliances WHERE id=?',(appliance_id,)):
                db.execute('INSERT INTO appliances(id,customer_id,site_id,cloud_id,appliance_type,serial_number,software_version,last_check_in,online_status,ip_address,cpu,memory,disk,camera_capacity,activation_token_hash,activation_token_created_at,shipping_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(appliance_id,customer_id,site_id,cloud_id,'AnyAiCam mini PC','Pending','Not installed',None,'offline','Not connected',0,0,0,max(16,camera_count),password_hash(activation_token),now,'not_ordered',now))
            if not row('SELECT id FROM appliance_activation_tokens WHERE appliance_id=? AND used_at IS NULL AND revoked_at IS NULL',(appliance_id,)):
                db.execute('INSERT INTO appliance_activation_tokens(id,appliance_id,token_hash,expires_at,created_at,created_by) VALUES(?,?,?,?,?,?)',(secrets.token_hex(6),appliance_id,password_hash(activation_token),(datetime.now()+timedelta(hours=24)).isoformat(),now,identity.get('email','')))
            if (row('SELECT COUNT(*) AS n FROM cameras WHERE appliance_id=?',(appliance_id,)) or {'n':0})['n']==0:
                for camera_number in range(1,camera_count+1):
                    db.execute('INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,resolution,status,created_at) VALUES(?,?,?,?,?,?,?,?)',(secrets.token_hex(5),customer_id,site_id,appliance_id,f'Camera {camera_number}','2mp','pending_installation',now))
            db.execute('INSERT INTO service_history(customer_id,event,details,created_at,created_by) VALUES(?,?,?,?,?)',(customer_id,'Self-service appliance provisioned',f'Cloud ID {cloud_id} provisioned via self-service Customer Setup for {camera_count} camera slot(s).',now,identity.get('email','')))
        audit(identity,'appliance.self_provisioned','appliance',appliance_id)
        return {
            'status':'provisioned','appliance_id':appliance_id,'cloud_id':cloud_id,'site_id':site_id,
            'activation_token':activation_token,'provisioning_qr_payload':provisioning['provisioning_qr_payload'],
            'camera_capacity':camera_count,
            'message':'Appliance provisioned. Store this activation token securely -- enter it (or paste the QR value below) on the physical appliance during anyaicam-setup; it will not be shown again after you leave this page.',
        }

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
        # Idempotency guard (confirmed-live, 2026-09-13): without this, every
        # click created a brand-new row, and the appliance-side agent
        # (poll_discovery(), appliance-agent/anyaicam_agent/service.py)
        # claims and works through every non-terminal job for one appliance
        # strictly sequentially -- one real scan() at a time, ~24s each in
        # practice. A customer clicking the button repeatedly (confirmed:
        # 6 times in 49 seconds on a real account) built an unbounded
        # backlog that took minutes to drain, and the browser only ever
        # tracks the newest job, which sits at the back of that queue.
        # Every request still completed correctly -- this was a latency/
        # pileup defect, not a broken chain -- closed at the source by
        # returning the existing non-terminal job instead of creating
        # another. _maybe_time_out_scan_job() is run on it first so a
        # genuinely abandoned job (past its own timeout) still correctly
        # frees up a new scan rather than blocking one forever.
        placeholders=','.join('?' for _ in CAMERA_SCAN_TERMINAL_STATES)
        existing=row(f"SELECT * FROM camera_scan_jobs WHERE appliance_id=? AND status NOT IN ({placeholders}) ORDER BY created_at DESC LIMIT 1",(appliance_id,*CAMERA_SCAN_TERMINAL_STATES))
        if existing:
            existing=_maybe_time_out_scan_job(existing)
            if existing['status'] not in CAMERA_SCAN_TERMINAL_STATES:
                return {'job_id':existing['id'],'status':existing['status'],'progress':existing['progress'],'message':existing['message']}
        job_id=secrets.token_hex(6); now=datetime.now().isoformat(); online=appliance.get('online_status')=='online'; status='queued' if online else 'waiting_for_appliance'; message='Discovery request queued for the appliance.' if online else 'Appliance is offline. Discovery will begin after it checks in.'
        with connection() as db: db.execute('INSERT INTO camera_scan_jobs(id,customer_id,appliance_id,status,progress,results_json,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',(job_id,identity['customer_id'],appliance_id,status,0,'[]',message,now,now))
        audit(identity,'camera.discovery_requested','appliance',appliance_id,{'job_id':job_id}); return {'job_id':job_id,'status':status,'progress':0,'message':message}

    @app.get('/api/customer/appliances/{appliance_id}/scans/latest')
    def customer_latest_camera_scan(request: Request,appliance_id: str) -> dict:
        # Confirmed live on Samsung: a completed scan job (5 discovered
        # candidates) never reappeared after leaving and reopening
        # /customer/setup -- the Discover step's scan-results div is only
        # ever populated by pollScan(), itself only ever started from the
        # "Request appliance scan" button's own click handler. There was
        # no code path that looked up an existing job for the appliance on
        # page load, and customer_setup_drafts.data_json's own saved
        # scan_job was confirmed unreliable too (null even after a real
        # job completed, since the client only ever writes whatever its
        # in-memory scanJob variable happens to hold at the moment "Save
        # and continue" is clicked). This looks up the appliance's own
        # latest job directly from camera_scan_jobs instead -- the actual
        # source of truth -- so the customer-setup page (and any other
        # caller) can always rehydrate discovery state on load without
        # creating a new scan job.
        identity=customer_owner(request)
        appliance=row('SELECT id FROM appliances WHERE id=? AND customer_id=?',(appliance_id,identity['customer_id']))
        if not appliance: raise HTTPException(status_code=404,detail='Appliance not found.')
        job=row('SELECT id FROM camera_scan_jobs WHERE appliance_id=? AND customer_id=? ORDER BY created_at DESC LIMIT 1',(appliance_id,identity['customer_id']))
        return {'job_id':job['id'] if job else None}

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
        # Provisioning Phase 7: enforce the purchased camera-slot maximum
        # (customer_entitlements.total_camera_slots() -- the one function
        # anything claim/refresh/provisioning-shaped must call, never a
        # hard-coded constant, per that module's own docstring). Re-
        # provisioning an ALREADY-KNOWN device_key (the customer's own
        # existing camera reconnecting, being rediscovered on a new scan,
        # or moved to a different appliance/site) never consumes a new
        # slot and is never blocked here -- only a genuinely new device_
        # key counts toward the limit. Queued-but-not-yet-confirmed
        # requests count too, so a burst of simultaneous "Add this
        # camera" clicks can't race past the limit before any of them
        # reach appliance_submit_provisioning()'s own second gate.
        from customer_entitlements import total_camera_slots
        already_known_device = row('SELECT id FROM cameras WHERE customer_id=? AND device_key=?',(identity['customer_id'],device_key))
        if not already_known_device:
            slot_limit=total_camera_slots(identity['customer_id'])
            configured=row('SELECT COUNT(*) AS n FROM cameras WHERE customer_id=? AND device_key IS NOT NULL',(identity['customer_id'],))['n']
            pending=row("SELECT COUNT(*) AS n FROM camera_provisioning_requests WHERE customer_id=? AND status='queued'",(identity['customer_id'],))['n']
            if configured+pending>=slot_limit:
                raise HTTPException(status_code=403,detail=f'Camera limit reached: this account is licensed for {slot_limit} camera(s). Upgrade your plan or remove a camera before adding another.')
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

    @app.get('/api/customer/cameras')
    def list_customer_cameras(request: Request, appliance_id: str='') -> dict:
        identity=customer_owner(request)
        # Multi-appliance isolation fix (2026-09-12): appliance_id is
        # optional (backward compatible with any account-wide caller),
        # but when the setup wizard passes the currently selected
        # appliance -- its only real caller -- this must return ONLY
        # that appliance's own cameras, never another appliance's, and
        # a customer must never be able to pass another customer's
        # appliance_id to see or count its cameras. Confirmed live on
        # anyaicam-staging: a customer with two appliances (an old
        # historical one carrying 5 seeded placeholder cameras, and a
        # newly-claimed Ryzen with zero real cameras) saw "5 of 8
        # cameras configured" while Ryzen was selected here, purely
        # because this query never filtered by appliance at all.
        if appliance_id:
            appliance=row('SELECT id FROM appliances WHERE id=? AND customer_id=?',(appliance_id,identity['customer_id']))
            if not appliance: raise HTTPException(status_code=404,detail='Appliance not found.')
            cameras=rows("SELECT id,name,site_id,appliance_id,resolution,status,device_key,ip_address,manufacturer,model,camera_number,created_at FROM cameras WHERE customer_id=? AND appliance_id=? AND status!='removed' ORDER BY created_at",(identity['customer_id'],appliance_id))
        else:
            cameras=rows("SELECT id,name,site_id,appliance_id,resolution,status,device_key,ip_address,manufacturer,model,camera_number,created_at FROM cameras WHERE customer_id=? AND status!='removed' ORDER BY created_at",(identity['customer_id'],))
        for camera in cameras: camera['credentials_configured']=bool(row('SELECT 1 FROM camera_credentials WHERE camera_id=?',(camera['id'],)))
        # Provisioning audit fix: this used to read plans.camera_quantity --
        # a legacy, partner-quoted, per-camera-subscription estimate that
        # predates the Local/Hybrid camera-slot architecture and is never
        # touched by a real Stripe purchase (see customer_entitlements.py's
        # own module docstring, audit finding #2). A customer who bought
        # "Local 1-8" through Stripe TEST checkout was shown "0 of 1
        # cameras configured" here -- the real, Stripe-verified capacity
        # (8) was never consulted. total_camera_slots() is the one
        # function anything camera-count-shaped must call, per that
        # module's own docstring; this is exactly that shape.
        from customer_entitlements import total_camera_slots
        expected_slots=total_camera_slots(identity['customer_id'])
        # Confirmed live: counting by status=='configured' alone let an
        # onboarding placeholder (device_key=NULL, never a real discovered
        # device) count as "configured" the instant its name was saved
        # through Step 5's "Save camera setup" -- the same click that
        # correctly marks a REAL provisioned camera configured, with no way
        # to tell the two apart from status alone. A real, provisioned
        # camera always has a device_key (see appliance_cloud.py's
        # appliance_submit_provisioning(), the only code path that ever
        # creates a cameras row from a confirmed device) and a real
        # camera_number (camera_mapping.py's assign_camera_number(),
        # called from that same path); a placeholder has neither. Counting
        # by device_key here -- not status -- so a customer's progress
        # reflects real, working cameras, never a renamed placeholder.
        configured=len([c for c in cameras if c.get('device_key')])
        return {'cameras':cameras,'expected_camera_count':expected_slots,'configured_camera_count':configured,'onboarding_complete':expected_slots>0 and configured>=expected_slots}

    # NOTE: a second, identically-pathed `provision_customer_camera`
    # handler used to be registered here. Starlette matches routes in
    # registration order, so it was permanently shadowed by
    # request_camera_provisioning() above and could never be reached by
    # real HTTP traffic -- confirmed dead code, not a second feature.
    # Its synchronous device_key dedup / camera_number assignment /
    # credential-storage logic is already done correctly by the live
    # async path this shadowed route duplicated: appliance_submit_
    # provisioning() in appliance_cloud.py, reached via request_camera_
    # provisioning()'s queued job once the appliance confirms it. Removed
    # rather than fixed-in-place so exactly one canonical implementation
    # of this route exists -- see test_exactly_one_camera_provision_route_is_registered().

    @app.delete('/api/customer/cameras/{camera_id}')
    def remove_customer_camera(request: Request,camera_id: str) -> dict:
        # RDM camera-quota requirement: removing an active camera must
        # free its licensed slot (device_key IS NOT NULL is exactly what
        # both provisioning-enforcement gates and list_customer_cameras()'s
        # own configured_camera_count above count) WITHOUT destroying
        # historical data -- recordings, detection_events, and
        # customer_clip_jobs all carry a real FOREIGN KEY(camera_id)
        # REFERENCES cameras(id) (db_migrations.py). This used to run
        # `DELETE FROM cameras WHERE id=?`, which would either orphan
        # every one of those historical rows' camera_id reference or
        # violate the FK outright -- exactly the "camera removal must
        # not destroy historical recordings/events/audit history"
        # requirement this route now honors. The camera row itself
        # (id/customer_id/site_id/appliance_id/name) is kept permanently
        # so a historical recording/event still resolves a real camera
        # name -- only device_key, camera_number, and credentials (all
        # genuinely disposable: a future replacement camera gets its own
        # fresh device_key/credentials, never these reused ones) are
        # cleared, and status becomes 'removed' so a future discovery/
        # provisioning pass never silently recycles this exact row for a
        # DIFFERENT physical camera (which would misattribute this
        # camera's real history onto an unrelated device -- see
        # appliance_submit_provisioning()'s own placeholder-slot query,
        # scoped to status='pending_installation' and therefore already
        # correctly skipping a 'removed' row).
        #
        # This is a genuinely separate operation from retention/data
        # deletion: an explicit "delete this camera's history" action
        # (if ever added) is its own, distinct, destructive operation on
        # recordings/detection_events directly -- never implied by
        # removing the camera from active service.
        identity=customer_owner(request)
        try: require_permission(identity,'camera.self.configure')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        camera=row('SELECT id, camera_number, appliance_id FROM cameras WHERE id=? AND customer_id=?',(camera_id,identity['customer_id']))
        if not camera: raise HTTPException(status_code=404,detail='Camera not found.')
        with connection() as db:
            if camera.get('camera_number') is not None and camera.get('appliance_id'):
                try:
                    from camera_mapping import assign_camera_number
                    assign_camera_number(db,camera_id,None,appliance_id=camera['appliance_id'],customer_id=identity['customer_id'])
                except (LookupError, ValueError):
                    pass
            db.execute('DELETE FROM camera_credentials WHERE camera_id=?',(camera_id,))
            db.execute("UPDATE cameras SET device_key=NULL,camera_number=NULL,status='removed' WHERE id=?",(camera_id,))
        audit(identity,'camera.removed','camera',camera_id); return {'message':'Camera removed. Historical recordings and events are preserved; the freed slot is available for a new camera.'}

    @app.put('/api/customer/cameras')
    def configure_customer_cameras(request: Request,payload: dict) -> dict:
        identity=customer_owner(request)
        try: require_permission(identity,'camera.self.configure')
        except PermissionError as error: raise HTTPException(status_code=403,detail=str(error)) from error
        # Multi-appliance isolation fix (2026-09-12): appliance_id is
        # optional (a legacy/direct caller with no appliance context
        # keeps working exactly as before), but the setup wizard --
        # its only real caller -- now always sends the appliance the
        # customer had selected, and when present each camera_id is
        # verified to actually belong to THAT appliance before being
        # touched, not just to the customer. Defense in depth on top of
        # the Step 5 table render fix (customer_first_setup()) already
        # only listing that appliance's own cameras: even a replayed or
        # hand-crafted request can no longer rename/reassign a different
        # appliance's camera merely because both belong to the same
        # customer.
        appliance_id=str(payload.get('appliance_id') or '')
        camera_items=payload.get('cameras',[]); now=datetime.now().isoformat()
        with connection() as db:
            for item in camera_items:
                camera_id=str(item.get('id',''))
                if appliance_id:
                    existing=db.execute('SELECT id FROM cameras WHERE id=? AND customer_id=? AND appliance_id=?',(camera_id,identity['customer_id'],appliance_id)).fetchone()
                else:
                    existing=db.execute('SELECT id FROM cameras WHERE id=? AND customer_id=?',(camera_id,identity['customer_id'])).fetchone()
                if existing: db.execute('UPDATE cameras SET name=?,site_id=?,resolution=?,status=? WHERE id=?',(item.get('name','Camera'),item.get('site_id'),item.get('resolution','2mp'),item.get('status','configured'),camera_id))
        audit(identity,'camera.configuration_saved','customer',identity['customer_id'],{'count':len(camera_items),'appliance_id':appliance_id or None}); return {'message':'Camera setup saved.'}

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
    partner_id=identity.get('partner_id') or 'anyaicam-primary'
    # HIGH fix (2026-09-14 partner-scoped-administrator follow-up, Codex
    # tenant-isolation re-audit): this listing previously used a bare
    # identity.get('role')=='administrator' shortcut to decide whether to
    # drop the partner_id filter entirely -- byte-identical for a true
    # platform-global administrator and a company-scoped one (see partner_
    # db.tenant_owns_partner()'s own docstring), so a partner-scoped
    # administrator could enumerate every other partner's real customer
    # records here, server-side, regardless of any UI filtering. Only a
    # live-verified GLOBAL administrator grant (appliance_identity.
    # has_global_administrator_grant(), the same primitive the rest of
    # this module's 2026-09-14 remediation already established) may see
    # customers across every partner; every other identity -- including a
    # company-scoped 'administrator' -- keeps the exact same partner_id-
    # scoped query as every other role.
    from appliance_identity import has_global_administrator_grant
    with connection() as db:
        is_global = has_global_administrator_grant(db,email=identity.get('email',''))
    if is_global:
        customers=rows('SELECT * FROM customers ORDER BY created_at DESC')
    else:
        customers=rows('SELECT * FROM customers WHERE partner_id=? ORDER BY created_at DESC',(partner_id,))
    account=_read(ACCOUNT_FILE,{})
    customer_rows=[]
    for customer in customers:
        searchable=f'{customer.get("name") or ""} {customer.get("company") or ""} {customer.get("email") or ""} {customer.get("id") or ""} {customer.get("partner_id") or ""}'.lower()
        site_count=row('SELECT COUNT(*) AS count FROM sites WHERE customer_id=?',(customer['id'],))['count']; plan=row('SELECT camera_quantity FROM plans WHERE customer_id=? ORDER BY created_at DESC LIMIT 1',(customer['id'],)) or {'camera_quantity':0}
        partner_label=f' · partner {escape(customer.get("partner_id",""))}' if is_global else ''
        customer_rows.append(f'''<article class="customer-row" data-customer-status="{escape(customer.get('status') or 'active')}" data-search="{escape(searchable,quote=True)}"><div><strong>{escape(customer.get('name') or 'Unnamed customer')}</strong><br><small>{escape(customer.get('company') or '')} · {site_count} site(s){partner_label}</small></div><div>{escape(customer.get('email') or '')}<br><small>{plan['camera_quantity']} cameras · {escape(customer.get('trial_status') or 'no trial')}</small></div><div><span class="pill">{escape((customer.get('status') or 'active').replace('_',' ').title())}</span></div><div><a class="download" href="/partner/customers/{customer['id']}">Manage</a></div></article>''')
    customer_body=''.join(customer_rows) if customer_rows else '<div class="empty" id="customer-empty">No real customer records yet.</div>'
    filters=''.join(f'<label><input type="radio" name="customer-status" value="{key}" {"checked" if key=="active" else ""}> {label}</label>' for key,label in [('active','Active'),('pending_installation','Pending installation'),('trial','Trial'),('suspended','Suspended'),('cancelled','Cancelled'),('all','All')])
    tabs=[('getting-started','Getting Started'),('partner-details','Partner Details'),('customers','Customers'),('materials','Materials'),('pricing','Pricing'),('adapters','Cloud Adapters')]
    tab_buttons=''.join(f'<button class="portal-tab {"active" if key=="customers" else ""}" data-portal-tab="{key}">{label}</button>' for key,label in tabs)
    # Reuses is_global (computed above) rather than a bare role check, for
    # the same reason: these links lead to genuinely global tools (every
    # partner's applications), so a company-scoped 'administrator' should
    # see the same partner-scoped workspace every other role sees.
    admin_link='<a class="ghost-button" href="/customer-portal">Customer Portal</a><a class="ghost-button" href="/partner-applications">Partner applications</a>' if is_global else ''
    # Reads the same SQL `appliances` table the real onboarding path
    # (onboard_customer(), the provisioning backend, /api/customer/setup/status)
    # writes to -- not the separate account_management.json-backed Appliance
    # model in business_portal.py, which only the older /setup wizard ever
    # populates and which a real Wizard A customer never appears in.
    if is_global:
        appliances=rows('SELECT a.* FROM appliances a ORDER BY a.created_at DESC')
    else:
        appliances=rows('SELECT a.* FROM appliances a JOIN customers c ON c.id=a.customer_id WHERE c.partner_id=? ORDER BY a.created_at DESC',(partner_id,))
    adapter_rows=''.join(f'''<article class="customer-row"><div><strong>{escape(item.get('cloud_id') or 'Unassigned')}</strong><br><small>{escape(item.get('serial_number') or 'Pending serial')}</small></div><div>{escape((item.get('online_status') or 'offline').title())}<br><small>{escape(item.get('software_version') or 'Unknown version')}</small></div><div><span class="pill">{escape((item.get('online_status') or 'offline').title())}</span></div><div><a class="download" href="/partner/appliance-dashboard">Manage</a></div></article>''' for item in appliances) or '<div class="empty">No appliance orders or assignments yet.</div>'
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
