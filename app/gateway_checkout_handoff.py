"""Gateway-only authorization-bound handoff to the existing checkout guard."""
import secrets
from urllib.parse import quote, urlsplit


def create_session(actor, *, customer_id, appliance_id, edition, stripe_customer_id, price_id, camera_ids, purchase, fields):
    """Return an observed, authorized open session, or raise without a URL.

    Caller enforces the existing default-off route gate, CSRF and price check.
    No session is made unless the separate attempt service gate is enabled.
    """
    import gateway_checkout_attempts as attempts
    from partner_db import transaction
    from database_backend import lock_customer_camera_capacity
    import checkout_guard, stripe_state
    if not attempts.ATTEMPTS_SERVICE_READY:
        raise attempts.AttemptConflict('Checkout attempt integration is unavailable.')
    with transaction() as db:
        if not lock_customer_camera_capacity(db,customer_id): raise attempts.AttemptConflict('Customer unavailable.')
        current=db.execute("SELECT id,status FROM gateway_checkout_attempts WHERE appliance_id=? AND status IN ('reserved','creating','open','review','paid_review','late_paid_review') ORDER BY created_at",(appliance_id,)).fetchall()
        if len(current)>1 or (current and current[0]['status'] not in ('reserved','creating','open')):
            raise attempts.AttemptConflict('An attempt needs reconciliation.')
        attempt_id=current[0]['id'] if current else secrets.token_hex(16)
        result=attempts.reserve(db,actor,attempt_id=attempt_id,customer_id=customer_id,appliance_id=appliance_id,
                                edition=edition,stripe_customer_id=stripe_customer_id,price_id=price_id,camera_ids=camera_ids)
        if result.get('status') not in ('reserved','existing'): raise attempts.AttemptConflict('Attempt unavailable.')
        record=db.execute('SELECT * FROM gateway_checkout_attempts WHERE id=? AND customer_id=?',(attempt_id,customer_id)).fetchone()
        if not record or not attempts._valid_attempt(db,record): raise attempts.AttemptConflict('Authorization changed.')
        if record['status'] in ('reserved','creating'):
            attempts.begin(db,actor,customer_id=customer_id,attempt_id=attempt_id)
    tagged=list(fields)+[('metadata[anyaicam_gateway_attempt]',attempt_id),
                         ('subscription_data[metadata][anyaicam_gateway_attempt]',attempt_id)]
    quantity=len(camera_ids) if edition=='cloud_gateway' else 1
    session=checkout_guard.create_session(customer_id,purchase,price_id=price_id,quantity=quantity,fields=tagged)
    session_id=session.get('id') if isinstance(session,dict) else None
    if not isinstance(session_id,str) or not session_id: raise attempts.AttemptConflict('Session outcome requires reconciliation.')
    canonical=stripe_state.stripe_get('/v1/checkout/sessions/'+quote(session_id,safe=''),'the Gateway checkout session')
    items=stripe_state.stripe_get('/v1/checkout/sessions/'+quote(session_id,safe='')+'/line_items','the Gateway checkout items')
    data=items.get('data') if isinstance(items,dict) else None
    if (not isinstance(canonical,dict) or canonical.get('id')!=session_id or not isinstance(data,list) or len(data)!=1
            or not isinstance(data[0],dict) or data[0].get('quantity')!=quantity
            or type(data[0].get('quantity')) is not int or not isinstance(data[0].get('price'),dict)
            or data[0]['price'].get('id')!=price_id or items.get('has_more') is not False):
        raise attempts.AttemptConflict('Session items are not verified.')
    if canonical.get('payment_status')=='paid':
        raise attempts.AttemptConflict('Payment awaits verified webhook reconciliation.')
    with transaction() as db:
        result=attempts.observe_session(db,attempt_id=attempt_id,session=canonical)
    if result.get('status')!='open' or not result.get('authorization_current'):
        raise attempts.AttemptConflict('Session requires reconciliation.')
    url=canonical.get('url')
    try:
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname!='checkout.stripe.com' or parsed.username or parsed.password:
            raise ValueError()
    except (TypeError,ValueError):
        raise attempts.AttemptConflict('Checkout URL is invalid.')
    return {'id':session_id,'url':url}
