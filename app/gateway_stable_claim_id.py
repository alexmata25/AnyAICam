"""Server-owned installation identifiers, never ownership credentials."""
import secrets
from datetime import datetime, timezone

STABLE_CLAIM_ID_READY = False


class StableClaimConflict(Exception):
    pass


def issue_after_verified_claim(db, *, appliance_id, customer_id, completed_claim_id):
    """Called inside the existing proof-verified claim-completion transaction."""
    if not STABLE_CLAIM_ID_READY: return None
    if db.execute("UPDATE appliances SET gateway_edition=gateway_edition WHERE id=? AND customer_id=? AND activated_via='claim'",(appliance_id,customer_id)).rowcount != 1:
        raise StableClaimConflict('Current appliance owner is unavailable.')
    claims=db.execute("SELECT id,customer_id FROM appliance_claims WHERE appliance_id=? AND status='completed' AND revoked_at IS NULL",(appliance_id,)).fetchall()
    if len(claims)!=1 or claims[0]['id']!=completed_claim_id or claims[0]['customer_id']!=customer_id:
        raise StableClaimConflict('Completed possession-verified claim is unavailable.')
    existing=db.execute('SELECT claim_id FROM gateway_stable_installation_claim_ids WHERE appliance_id=?',(appliance_id,)).fetchone()
    if existing: return existing['claim_id']
    claim_id='AAC-'+secrets.token_hex(16).upper()
    inserted=db.execute('INSERT INTO gateway_stable_installation_claim_ids(claim_id,appliance_id,original_claim_id,created_at) VALUES(?,?,?,?) ON CONFLICT DO NOTHING',
                        (claim_id,appliance_id,completed_claim_id,datetime.now(timezone.utc).isoformat())).rowcount
    if inserted != 1:
        raise StableClaimConflict('Installation identity is conflicting.')
    from partner_db import audit
    audit({'email':'claim-service','role':'system'},'gateway.stable_claim_id_issued','appliance',appliance_id,
          {'customer_id':customer_id,'original_claim_id':completed_claim_id})
    return claim_id


def read_for_owner(db, actor, *, appliance_id):
    """Return only the current owner's ID; the ID does not authorize mutations."""
    if not STABLE_CLAIM_ID_READY: return None
    if not isinstance(actor,dict) or actor.get('role') != 'customer_owner' or not actor.get('customer_id'):
        return None
    customer=actor['customer_id']
    rows=db.execute("SELECT s.claim_id FROM gateway_stable_installation_claim_ids s JOIN appliances a ON a.id=s.appliance_id JOIN appliance_claims c ON c.appliance_id=a.id WHERE a.id=? AND a.customer_id=? AND c.customer_id=? AND c.status='completed' AND c.revoked_at IS NULL",(appliance_id,customer,customer)).fetchall()
    return rows[0]['claim_id'] if len(rows)==1 else None


def request_lost_credential_review(db, actor, *, appliance_id, request_id):
    """Record an owner request only; never reissue credentials or transfer."""
    if not STABLE_CLAIM_ID_READY: raise StableClaimConflict('Recovery is unavailable.')
    if (not isinstance(actor,dict) or actor.get('role')!='customer_owner' or not actor.get('customer_id')
            or not isinstance(actor.get('email'),str) or not actor['email'].strip()
            or not isinstance(request_id,str) or not request_id or request_id!=request_id.strip() or len(request_id)>128):
        raise StableClaimConflict('Authenticated owner and request ID required.')
    customer=actor['customer_id']
    if db.execute('UPDATE appliances SET gateway_edition=gateway_edition WHERE id=? AND customer_id=?',(appliance_id,customer)).rowcount!=1:
        raise StableClaimConflict('Installation unavailable.')
    if not read_for_owner(db,actor,appliance_id=appliance_id):
        raise StableClaimConflict('Current ownership and completed claim are required.')
    old=db.execute('SELECT appliance_id,customer_id,requested_by FROM gateway_claim_recovery_requests WHERE request_id=?',(request_id,)).fetchone()
    if old:
        if tuple(old[key] for key in ('appliance_id','customer_id','requested_by'))==(appliance_id,customer,actor['email']):
            return {'status':'pending_manual_verification','duplicate':True}
        raise StableClaimConflict('Recovery request identity conflicts.')
    inserted=db.execute("INSERT INTO gateway_claim_recovery_requests(request_id,appliance_id,customer_id,requested_by,status,created_at) VALUES(?,?,?,?,'pending_manual_verification',?) ON CONFLICT DO NOTHING",
                        (request_id,appliance_id,customer,actor['email'],datetime.now(timezone.utc).isoformat())).rowcount
    if inserted!=1: raise StableClaimConflict('A recovery request is already pending.')
    from partner_db import audit
    audit(actor,'gateway.lost_credential_recovery_requested','appliance',appliance_id,
          {'customer_id':customer,'request_id':request_id,'status':'pending_manual_verification'})
    return {'status':'pending_manual_verification'}


def register_unmounted_routes(app):
    """Prepare owner lookup and owner-plus-device recovery; never transfer."""
    from fastapi import HTTPException, Request
    from partner_portal import partner_identity
    from partner_db import transaction, audit
    from appliance_cloud import authenticate_appliance
    from token_security import unsign
    import hmac

    def owner_for(request):
        if not STABLE_CLAIM_ID_READY: raise HTTPException(status_code=404,detail='Not found.')
        actor=partner_identity(request)
        if not actor or actor.get('role')!='customer_owner' or not actor.get('customer_id'):
            raise HTTPException(status_code=403,detail='Customer owner required.')
        return actor

    @app.get('/api/v2/customer/appliances/{appliance_id}/gateway-claim-id')
    def read_claim_id(appliance_id: str, request: Request):
        actor=owner_for(request)
        with transaction() as db:
            claim_id=read_for_owner(db,actor,appliance_id=appliance_id)
        if not claim_id: raise HTTPException(status_code=404,detail='Installation not found.')
        return {'claim_id':claim_id}

    @app.post('/api/v2/customer/appliances/{appliance_id}/gateway-claim-id/recover')
    def recover_claim_id(appliance_id: str, request: Request):
        actor=owner_for(request)
        cookie=request.cookies.get('anyaicam_csrf'); token=request.headers.get('x-csrf-token')
        try: valid=isinstance(cookie,str) and isinstance(token,str) and bool(cookie) and hmac.compare_digest(cookie,token) and unsign(cookie)=='csrf'
        except (TypeError,ValueError): valid=False
        if not valid: raise HTTPException(status_code=403,detail='CSRF validation failed.')
        appliance=authenticate_appliance(request)
        if not isinstance(appliance,dict) or appliance.get('id')!=appliance_id or appliance.get('customer_id')!=actor['customer_id']:
            raise HTTPException(status_code=403,detail='Current appliance proof required.')
        with transaction() as db:
            claim_id=read_for_owner(db,actor,appliance_id=appliance_id)
            if not claim_id: raise HTTPException(status_code=404,detail='Installation not found.')
            audit(actor,'gateway.stable_claim_id_recovered','appliance',appliance_id,{'customer_id':actor['customer_id']})
        return {'claim_id':claim_id}

    @app.post('/api/v2/customer/appliances/{appliance_id}/gateway-claim-id/recovery-request')
    def request_recovery(appliance_id: str, payload: dict, request: Request):
        actor=owner_for(request)
        cookie=request.cookies.get('anyaicam_csrf'); token=request.headers.get('x-csrf-token')
        try: valid=isinstance(cookie,str) and isinstance(token,str) and bool(cookie) and hmac.compare_digest(cookie,token) and unsign(cookie)=='csrf'
        except (TypeError,ValueError): valid=False
        if not valid: raise HTTPException(status_code=403,detail='CSRF validation failed.')
        if not isinstance(payload,dict) or set(payload)!={'request_id'}:
            raise HTTPException(status_code=400,detail='Invalid recovery request.')
        try:
            with transaction() as db:
                return request_lost_credential_review(db,actor,appliance_id=appliance_id,request_id=payload['request_id'])
        except StableClaimConflict as error:
            raise HTTPException(status_code=409,detail=str(error)) from error

    @app.get('/api/operations/internal-gateway/claim-recovery')
    def list_recovery_requests(request: Request):
        from gateway_internal_provisioning import internal_test_enabled
        from appliance_identity import has_global_administrator_grant
        if not STABLE_CLAIM_ID_READY or not internal_test_enabled(): raise HTTPException(status_code=404,detail='Not found.')
        actor=partner_identity(request)
        if not actor or actor.get('role')!='administrator' or not actor.get('email'):
            raise HTTPException(status_code=403,detail='Global administrator required.')
        with transaction() as db:
            if not has_global_administrator_grant(db,email=actor['email']):
                raise HTTPException(status_code=403,detail='Global administrator required.')
            records=db.execute("SELECT request_id,appliance_id,customer_id,requested_by,status,created_at FROM gateway_claim_recovery_requests WHERE status='pending_manual_verification' ORDER BY created_at LIMIT 100").fetchall()
            return {'requests':[dict(record) for record in records]}
