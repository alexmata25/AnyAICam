"""Prepared internal test review workflow; never activates or refunds."""
from datetime import datetime, timezone


class ReviewConflict(Exception):
    pass


def record_review_action(db, actor, *, attempt_id, request_id, action):
    """Append an idempotent operator disposition in the caller's transaction."""
    from gateway_internal_provisioning import internal_test_enabled
    from appliance_identity import has_global_administrator_grant
    if (not internal_test_enabled() or not isinstance(actor,dict) or actor.get('role')!='administrator'
            or not isinstance(actor.get('email'),str) or not actor['email']
            or not has_global_administrator_grant(db,email=actor['email'])):
        raise ReviewConflict('Current global administrator required.')
    if (action not in {'investigate','provider_followup','refund_review'}
            or any(not isinstance(value,str) or not value or value != value.strip() or len(value)>128 for value in (attempt_id,request_id))):
        raise ReviewConflict('Invalid review action.')
    existing=db.execute('SELECT attempt_id,actor_email,action FROM gateway_checkout_review_actions WHERE request_id=?',(request_id,)).fetchone()
    if existing:
        if (existing['attempt_id'],existing['actor_email'],existing['action']) == (attempt_id,actor['email'],action):
            return {'status':'already_recorded'}
        raise ReviewConflict('Review request conflicts.')
    locked=db.execute("UPDATE gateway_checkout_attempts SET status=status WHERE id=? AND status IN ('creating','review','paid_review','late_paid_review')",(attempt_id,)).rowcount
    if locked != 1: raise ReviewConflict('Attempt does not require review.')
    inserted=db.execute('INSERT INTO gateway_checkout_review_actions(request_id,attempt_id,actor_email,action,created_at) VALUES(?,?,?,?,?) ON CONFLICT(request_id) DO NOTHING',
                        (request_id,attempt_id,actor['email'],action,datetime.now(timezone.utc).isoformat())).rowcount
    if inserted != 1: raise ReviewConflict('Review request changed; retry.')
    from partner_db import audit
    audit(actor,'gateway.checkout_review_action','gateway_checkout_attempt',attempt_id,{'request_id':request_id,'action':action})
    return {'status':'recorded'}


def resolve_verified_expired(db, actor, *, attempt_id, request_id, session):
    """Close only a canonically verified expired, unpaid test session."""
    from gateway_internal_provisioning import internal_test_enabled
    from appliance_identity import has_global_administrator_grant
    if (not internal_test_enabled() or not isinstance(actor,dict) or actor.get('role')!='administrator'
            or not actor.get('email') or not has_global_administrator_grant(db,email=actor['email'])):
        raise ReviewConflict('Current global administrator required.')
    if any(not isinstance(value,str) or not value or value!=value.strip() or len(value)>128 for value in (attempt_id,request_id)):
        raise ReviewConflict('Invalid review request.')
    old=db.execute('SELECT attempt_id,actor_email,action FROM gateway_checkout_review_actions WHERE request_id=?',(request_id,)).fetchone()
    if old:
        if (old['attempt_id'],old['actor_email'],old['action'])==(attempt_id,actor['email'],'resolved_expired'):
            return {'status':'already_recorded'}
        raise ReviewConflict('Review request conflicts.')
    locked=db.execute("UPDATE gateway_checkout_attempts SET status=status WHERE id=? AND status IN ('creating','open','review')",(attempt_id,)).rowcount
    if locked!=1: raise ReviewConflict('Attempt is paid, closed or unavailable.')
    attempt=db.execute('SELECT session_id,stripe_customer_id,payment_event_id FROM gateway_checkout_attempts WHERE id=?',(attempt_id,)).fetchone()
    meta=session.get('metadata') if isinstance(session,dict) else None
    if (not attempt or not attempt['session_id'] or attempt['payment_event_id'] or not isinstance(session,dict)
            or session.get('id')!=attempt['session_id'] or session.get('customer')!=attempt['stripe_customer_id']
            or session.get('livemode') is not False or session.get('mode')!='subscription'
            or session.get('status')!='expired' or session.get('payment_status')!='unpaid'
            or not isinstance(meta,dict) or meta.get('anyaicam_gateway_attempt')!=attempt_id):
        raise ReviewConflict('Canonical expired unpaid session is not verified.')
    now=datetime.now(timezone.utc).isoformat()
    db.execute("UPDATE gateway_checkout_attempts SET status='expired',updated_at=? WHERE id=?",(now,attempt_id))
    db.execute('INSERT INTO gateway_checkout_review_actions(request_id,attempt_id,actor_email,action,created_at) VALUES(?,?,?,?,?)',
               (request_id,attempt_id,actor['email'],'resolved_expired',now))
    from partner_db import audit
    audit(actor,'gateway.checkout_expired_reconciled','gateway_checkout_attempt',attempt_id,{'request_id':request_id,'session_id':attempt['session_id']})
    return {'status':'expired'}


def link_verified_session(db, actor, *, attempt_id, request_id, session, items):
    """Attach only a canonical session already anchored in checkout_pending.

    Linking quarantines the attempt and grants nothing; a verified webhook
    remains responsible for recording any paid event.
    """
    from gateway_internal_provisioning import internal_test_enabled
    from appliance_identity import has_global_administrator_grant
    if (not internal_test_enabled() or not isinstance(actor,dict) or actor.get('role')!='administrator'
            or not actor.get('email') or not has_global_administrator_grant(db,email=actor['email'])):
        raise ReviewConflict('Current global administrator required.')
    if any(not isinstance(value,str) or not value or value!=value.strip() or len(value)>128 for value in (attempt_id,request_id)):
        raise ReviewConflict('Invalid review request.')
    old=db.execute('SELECT attempt_id,actor_email,action FROM gateway_checkout_review_actions WHERE request_id=?',(request_id,)).fetchone()
    if old:
        if (old['attempt_id'],old['actor_email'],old['action'])==(attempt_id,actor['email'],'linked_session'):
            return {'status':'already_recorded'}
        raise ReviewConflict('Review request conflicts.')
    locked=db.execute("UPDATE gateway_checkout_attempts SET status=status WHERE id=? AND status IN ('creating','review') AND payment_event_id IS NULL",(attempt_id,)).rowcount
    if locked!=1: raise ReviewConflict('Attempt is paid, closed or unavailable.')
    attempt=db.execute('SELECT * FROM gateway_checkout_attempts WHERE id=?',(attempt_id,)).fetchone()
    purchase='cloud_gateway' if attempt['edition']=='cloud_gateway' else 'local_gateway_remote:'+attempt['appliance_id']
    metadata=session.get('metadata') if isinstance(session,dict) else None
    session_id=session.get('id') if isinstance(session,dict) else None
    data=items.get('data') if isinstance(items,dict) else None
    if (not isinstance(session_id,str) or not session_id or (attempt['session_id'] and attempt['session_id']!=session_id)
            or session.get('livemode') is not False or session.get('mode')!='subscription'
            or session.get('customer')!=attempt['stripe_customer_id'] or session.get('status') not in ('open','complete','expired')
            or session.get('payment_status') not in ('paid','unpaid','no_payment_required')
            or not isinstance(metadata,dict) or metadata.get('anyaicam_gateway_attempt')!=attempt_id
            or not isinstance(data,list) or len(data)!=1 or items.get('has_more') is not False
            or not isinstance(data[0],dict) or type(data[0].get('quantity')) is not int or data[0]['quantity']!=attempt['quantity']
            or not isinstance(data[0].get('price'),dict) or data[0]['price'].get('id')!=attempt['stripe_price_id']):
        raise ReviewConflict('Canonical session does not match the attempt.')
    anchor=db.execute("UPDATE checkout_pending SET status=status WHERE customer_id=? AND purchase=? AND session_id=? AND status IN ('open','paid')",(attempt['customer_id'],purchase,session_id)).rowcount
    if anchor!=1: raise ReviewConflict('Server-owned checkout session anchor unavailable.')
    now=datetime.now(timezone.utc).isoformat()
    db.execute("UPDATE gateway_checkout_attempts SET status='review',authorization_invalidated=1,session_id=?,updated_at=? WHERE id=?",(session_id,now,attempt_id))
    db.execute('INSERT INTO gateway_checkout_review_actions(request_id,attempt_id,actor_email,action,created_at) VALUES(?,?,?,?,?)',
               (request_id,attempt_id,actor['email'],'linked_session',now))
    from partner_db import audit
    audit(actor,'gateway.checkout_session_linked_for_review','gateway_checkout_attempt',attempt_id,{'request_id':request_id,'session_id':session_id})
    return {'status':'review'}


def register_unmounted_review_routes(app):
    from fastapi import HTTPException, Request
    from partner_portal import partner_identity
    from partner_db import transaction
    from appliance_identity import has_global_administrator_grant
    from gateway_internal_provisioning import internal_test_enabled

    @app.get('/api/operations/internal-gateway/checkout-review')
    def list_checkout_review(request: Request):
        if not internal_test_enabled(): raise HTTPException(status_code=404,detail='Not found.')
        actor=partner_identity(request)
        if not actor or actor.get('role')!='administrator' or not actor.get('email'):
            raise HTTPException(status_code=403,detail='Global administrator required.')
        with transaction() as db:
            if not has_global_administrator_grant(db,email=actor['email']):
                raise HTTPException(status_code=403,detail='Global administrator required.')
            rows=db.execute("SELECT id,customer_id,appliance_id,edition,status,session_id,payment_event_id,stripe_invoice_id,reversal_state,refund_event_id,updated_at FROM gateway_checkout_attempts WHERE status IN ('creating','review','paid_review','late_paid_review') ORDER BY updated_at LIMIT 100").fetchall()
            return {'attempts':[dict(item) for item in rows]}

    @app.post('/api/operations/internal-gateway/checkout-review/{attempt_id}/actions')
    def append_checkout_review(attempt_id: str, payload: dict, request: Request):
        import hmac
        from token_security import unsign
        if not internal_test_enabled(): raise HTTPException(status_code=404,detail='Not found.')
        actor=partner_identity(request)
        if not actor or actor.get('role')!='administrator' or not actor.get('email'):
            raise HTTPException(status_code=403,detail='Global administrator required.')
        cookie=request.cookies.get('anyaicam_csrf'); token=request.headers.get('x-csrf-token')
        try: valid=isinstance(cookie,str) and isinstance(token,str) and bool(cookie) and hmac.compare_digest(cookie,token) and unsign(cookie)=='csrf'
        except (TypeError,ValueError): valid=False
        if not valid: raise HTTPException(status_code=403,detail='CSRF validation failed.')
        if not isinstance(payload,dict) or set(payload)!={'request_id','action'}:
            raise HTTPException(status_code=400,detail='Invalid review action.')
        try:
            with transaction() as db:
                if not has_global_administrator_grant(db,email=actor['email']):
                    raise HTTPException(status_code=403,detail='Global administrator required.')
                return record_review_action(db,actor,attempt_id=attempt_id,request_id=payload['request_id'],action=payload['action'])
        except ReviewConflict as error:
            raise HTTPException(status_code=409,detail=str(error)) from error

    @app.post('/api/operations/internal-gateway/checkout-review/{attempt_id}/resolve-expired')
    def resolve_expired(attempt_id: str, payload: dict, request: Request):
        import hmac
        from token_security import unsign
        import stripe_mode, stripe_state
        from urllib.parse import quote
        if not internal_test_enabled(): raise HTTPException(status_code=404,detail='Not found.')
        actor=partner_identity(request)
        if not actor or actor.get('role')!='administrator' or not actor.get('email'):
            raise HTTPException(status_code=403,detail='Global administrator required.')
        cookie=request.cookies.get('anyaicam_csrf'); token=request.headers.get('x-csrf-token')
        try: valid=isinstance(cookie,str) and isinstance(token,str) and bool(cookie) and hmac.compare_digest(cookie,token) and unsign(cookie)=='csrf'
        except (TypeError,ValueError): valid=False
        if not valid: raise HTTPException(status_code=403,detail='CSRF validation failed.')
        if not isinstance(payload,dict) or set(payload)!={'request_id'}:
            raise HTTPException(status_code=400,detail='Invalid review request.')
        if stripe_mode.server_key_is_test() is not True:
            raise HTTPException(status_code=503,detail='Test Stripe configuration required.')
        try:
            with transaction() as db:
                if not has_global_administrator_grant(db,email=actor['email']):
                    raise HTTPException(status_code=403,detail='Global administrator required.')
                record=db.execute('SELECT session_id FROM gateway_checkout_attempts WHERE id=?',(attempt_id,)).fetchone()
                if not record or not record['session_id']: raise ReviewConflict('Session outcome remains unknown.')
                try:
                    session=stripe_state.stripe_get('/v1/checkout/sessions/'+quote(record['session_id'],safe=''),'the Gateway checkout session')
                except Exception as error:
                    raise HTTPException(status_code=503,detail='Provider verification is unavailable.') from error
                return resolve_verified_expired(db,actor,attempt_id=attempt_id,request_id=payload['request_id'],session=session)
        except ReviewConflict as error:
            raise HTTPException(status_code=409,detail=str(error)) from error

    @app.post('/api/operations/internal-gateway/checkout-review/{attempt_id}/link-session')
    def link_session(attempt_id: str, payload: dict, request: Request):
        import hmac
        from token_security import unsign
        import stripe_mode, stripe_state
        from urllib.parse import quote
        if not internal_test_enabled(): raise HTTPException(status_code=404,detail='Not found.')
        actor=partner_identity(request)
        if not actor or actor.get('role')!='administrator' or not actor.get('email'):
            raise HTTPException(status_code=403,detail='Global administrator required.')
        cookie=request.cookies.get('anyaicam_csrf'); token=request.headers.get('x-csrf-token')
        try: valid=isinstance(cookie,str) and isinstance(token,str) and bool(cookie) and hmac.compare_digest(cookie,token) and unsign(cookie)=='csrf'
        except (TypeError,ValueError): valid=False
        if not valid: raise HTTPException(status_code=403,detail='CSRF validation failed.')
        if not isinstance(payload,dict) or set(payload)!={'request_id'}:
            raise HTTPException(status_code=400,detail='Invalid review request.')
        if stripe_mode.server_key_is_test() is not True:
            raise HTTPException(status_code=503,detail='Test Stripe configuration required.')
        try:
            with transaction() as db:
                if not has_global_administrator_grant(db,email=actor['email']):
                    raise HTTPException(status_code=403,detail='Global administrator required.')
                attempt=db.execute('SELECT customer_id,appliance_id,edition FROM gateway_checkout_attempts WHERE id=?',(attempt_id,)).fetchone()
                if not attempt: raise ReviewConflict('Attempt not found.')
                purchase='cloud_gateway' if attempt['edition']=='cloud_gateway' else 'local_gateway_remote:'+attempt['appliance_id']
                pending=db.execute('SELECT session_id FROM checkout_pending WHERE customer_id=? AND purchase=?',(attempt['customer_id'],purchase)).fetchone()
                if not pending or not isinstance(pending['session_id'],str) or not pending['session_id']:
                    raise ReviewConflict('Server-owned session identity is still unknown.')
                path='/v1/checkout/sessions/'+quote(pending['session_id'],safe='')
                try:
                    session=stripe_state.stripe_get(path,'the Gateway checkout session')
                    items=stripe_state.stripe_get(path+'/line_items','the Gateway checkout items')
                except Exception as error:
                    raise HTTPException(status_code=503,detail='Provider verification is unavailable.') from error
                return link_verified_session(db,actor,attempt_id=attempt_id,request_id=payload['request_id'],session=session,items=items)
        except ReviewConflict as error:
            raise HTTPException(status_code=409,detail=str(error)) from error
