"""Prepared customer Cloud Gateway checkout; route is not mounted or enabled."""
import os

CHECKOUT_HANDOFF_VERIFIED = False


def register_routes(app):
    """Register only in an approved environment with the normal CSRF middleware."""
    from fastapi import HTTPException, Request
    from partner_portal import partner_identity

    @app.post('/api/v2/customer/cloud-gateway/checkout')
    def cloud_gateway_checkout(payload: dict, request: Request):
        if os.environ.get('ANYAICAM_CLOUD_GATEWAY_CHECKOUT_ENABLED','').strip().lower() != 'true':
            raise HTTPException(status_code=503,detail='Cloud Gateway checkout is unavailable.')
        identity=partner_identity(request)
        if not identity or identity.get('role') != 'customer_owner' or not identity.get('customer_id'):
            raise HTTPException(status_code=403,detail='Customer owner permission required.')
        import hmac
        from token_security import unsign
        cookie=request.cookies.get('anyaicam_csrf'); token=request.headers.get('x-csrf-token')
        if not cookie or not token or not hmac.compare_digest(cookie,token) or unsign(cookie) != 'csrf':
            raise HTTPException(status_code=403,detail='CSRF validation failed.')
        if not isinstance(payload,dict) or set(payload) != {'appliance_id','camera_ids'}:
            raise HTTPException(status_code=400,detail='Choose an appliance and cameras.')
        appliance_id=payload['appliance_id']; cameras=payload['camera_ids']
        if (not isinstance(appliance_id,str) or not appliance_id or appliance_id != appliance_id.strip() or len(appliance_id)>128
                or not isinstance(cameras,list) or not 1 <= len(cameras) <= 100
                or any(not isinstance(c,str) or not c or c != c.strip() or len(c)>128 for c in cameras)
                or len(set(cameras)) != len(cameras)):
            raise HTTPException(status_code=400,detail='Choose distinct cameras.')
        customer_id=identity['customer_id']
        from partner_db import transaction
        from database_backend import lock_customer_camera_capacity
        from cloud_gateway_camera_entitlements import _camera_mapping
        with transaction() as db:
            if not lock_customer_camera_capacity(db,customer_id):
                raise HTTPException(status_code=403,detail='Customer unavailable.')
            appliance=db.execute('SELECT id,customer_id,gateway_edition FROM appliances WHERE id=? AND customer_id=?',(appliance_id,customer_id)).fetchone()
            if not appliance or appliance['customer_id'] != customer_id or appliance['gateway_edition'] != 'cloud_gateway':
                raise HTTPException(status_code=403,detail='Owned Cloud Gateway appliance required.')
            if not all(_camera_mapping(db,customer_id,appliance_id,camera) for camera in cameras):
                raise HTTPException(status_code=409,detail='Camera mapping or edition pending verification.')
        # A database transaction cannot cover a remote provider call. Keep the
        # hard stop until revocations and unknown-outcome sessions are fenced.
        if not CHECKOUT_HANDOFF_VERIFIED:
            raise HTTPException(status_code=503,detail='Cloud Gateway checkout protection is pending.')
        import gateway_checkout_attempts
        if not gateway_checkout_attempts.ATTEMPTS_SERVICE_READY:
            raise HTTPException(status_code=503,detail='Checkout attempt protection is unavailable.')
        import checkout_guard, main
        from cloud_gateway_subscription_evidence import checkout_fields
        price_id=os.environ.get('ANYAICAM_STRIPE_PRICE_CLOUD_GATEWAY','').strip()
        if not price_id or not main.PUBLIC_BASE_URL:
            raise HTTPException(status_code=503,detail='Cloud Gateway checkout is unavailable.')
        main.require_stripe_price_matches_catalog(price_id,999,'month')
        stripe_customer=checkout_guard.canonical_stripe_customer(customer_id,email=identity.get('email'))
        base=main.PUBLIC_BASE_URL.rstrip('/')
        purchase,fields=checkout_fields(customer_id=customer_id,stripe_customer_id=stripe_customer,price_id=price_id,
                                        quantity=len(cameras),success_url=base+'/subscription-portal?cloud_gateway=success',
                                        cancel_url=base+'/subscription-portal?cloud_gateway=cancelled')
        from gateway_checkout_handoff import create_session
        try:
            session=create_session(identity,customer_id=customer_id,appliance_id=appliance_id,
                edition='cloud_gateway',stripe_customer_id=stripe_customer,price_id=price_id,
                camera_ids=cameras,purchase=purchase,fields=fields)
        except Exception as error:
            raise HTTPException(status_code=503,detail='Checkout requires reconciliation.') from error
        from urllib.parse import urlsplit
        try:
            url=str(session.get('url') or ''); parsed=urlsplit(url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password: raise ValueError()
        except (ValueError,AttributeError):
            raise HTTPException(status_code=502,detail='Checkout could not be started.')
        return {'status':'complete','checkout_url':url}
