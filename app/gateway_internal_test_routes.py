"""Prepared routes for internal test provisioning. This module is not mounted."""
import hmac


def register_unmounted_test_routes(app):
    from fastapi import HTTPException, Request
    from partner_portal import partner_identity
    from token_security import unsign
    import gateway_internal_provisioning as service

    def actor_for(request):
        if not service.internal_test_enabled():
            raise HTTPException(status_code=404, detail='Not found.')
        identity = partner_identity(request)
        if not identity or identity.get('role') != 'administrator' or not identity.get('email'):
            raise HTTPException(status_code=403, detail='Global administrator required.')
        cookie = request.cookies.get('anyaicam_csrf')
        token = request.headers.get('x-csrf-token')
        try:
            valid = (isinstance(cookie, str) and isinstance(token, str) and bool(cookie)
                     and hmac.compare_digest(cookie, token) and unsign(cookie) == 'csrf')
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise HTTPException(status_code=403, detail='CSRF validation failed.')
        return identity

    def validate(payload, keys):
        if not isinstance(payload, dict) or set(payload) != keys:
            raise HTTPException(status_code=400, detail='Invalid request.')
        for key, value in payload.items():
            if key == 'version':
                if type(value) is not int or value < 1 or value > 1000:
                    raise HTTPException(status_code=400, detail='Invalid version.')
            elif not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 200:
                raise HTTPException(status_code=400, detail='Invalid request.')

    def call(fn, actor, **kwargs):
        try:
            return fn(actor, **kwargs)
        except service.AuthorizationDenied as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except service.ProvisioningError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    common = {'customer_id','appliance_id','claim_id','edition'}

    @app.post('/api/operations/internal-gateway/authorizations')
    def authorize(payload: dict, request: Request):
        actor = actor_for(request); validate(payload, common | {'request_id'})
        return call(service.authorize_transactionally, actor, **payload)

    @app.post('/api/operations/internal-gateway/authorizations/{authorization_id}/revoke')
    def revoke(authorization_id: str, payload: dict, request: Request):
        actor = actor_for(request); validate(payload, common | {'version'})
        return call(service.change_transactionally, actor, authorization_id=authorization_id, action='revoke', **payload)

    @app.post('/api/operations/internal-gateway/authorizations/{authorization_id}/version')
    def advance(authorization_id: str, payload: dict, request: Request):
        actor = actor_for(request); validate(payload, common | {'version'})
        return call(service.change_transactionally, actor, authorization_id=authorization_id, action='advance', **payload)

    @app.post('/api/operations/internal-gateway/authorizations/{authorization_id}/confirm')
    def confirm(authorization_id: str, payload: dict, request: Request):
        actor = actor_for(request); validate(payload, common | {'version'})
        return call(service.confirm_transactionally, actor, authorization_id=authorization_id, **payload)
