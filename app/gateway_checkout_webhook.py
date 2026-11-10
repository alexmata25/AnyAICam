"""Verified-dispatcher step for recording Gateway checkout observations."""
from urllib.parse import quote


def _refund_from_verified_webhook(event, *, fetch=None):
    """Reconcile only a refund linked to a recorded Gateway invoice."""
    import gateway_checkout_attempts as attempts
    if not attempts.ATTEMPTS_SERVICE_READY: return {'status':'unavailable'}
    obj=((event.get('data') or {}).get('object') or {})
    charge_id=obj.get('id') if isinstance(obj,dict) else None
    invoice_id=obj.get('invoice') if isinstance(obj,dict) else None
    if isinstance(invoice_id,dict): invoice_id=invoice_id.get('id')
    if not isinstance(charge_id,str) or not charge_id or not isinstance(invoice_id,str) or not invoice_id:
        return {'status':'ignored'}
    from partner_db import row, transaction
    try: anchor=row('SELECT id FROM gateway_checkout_attempts WHERE stripe_invoice_id=?',(invoice_id,))
    except Exception: return {'status':'unavailable'}
    if not anchor: return {'status':'ignored'}
    import stripe_state, billing_status
    reader=fetch or stripe_state.stripe_get
    def snapshots():
        charge=reader('/v1/charges/'+quote(charge_id,safe=''),'the Gateway refund charge')
        invoice=reader('/v1/invoices/'+quote(invoice_id,safe=''),'the Gateway refund invoice')
        if not isinstance(charge,dict) or not isinstance(invoice,dict) or charge.get('id')!=charge_id or invoice.get('id')!=invoice_id:
            raise attempts.AttemptConflict('Refund provider identity is invalid.')
        return charge,invoice
    charge,invoice=snapshots()
    intent=charge.get('payment_intent'); intent=intent.get('id') if isinstance(intent,dict) else intent
    if not isinstance(intent,str) or not intent: raise attempts.AttemptConflict('Refund payment identity is unavailable.')
    with transaction() as db:
        billing_status.lock_payment(db,billing_status.payment_keys(charge_id,intent,invoice_id))
        charge,invoice=snapshots()
        current=charge.get('payment_intent'); current=current.get('id') if isinstance(current,dict) else current
        if current!=intent: raise attempts.AttemptConflict('Refund payment changed during verification.')
        if type(charge.get('amount_refunded')) is int and type(charge.get('amount')) is int and 0 <= charge['amount_refunded'] < charge['amount']:
            return {'status':'partial_refund'}
        return attempts.record_verified_full_refund(db,attempt_id=anchor['id'],event_id=event.get('id'),charge=charge,invoice=invoice)


def sync_from_verified_webhook(event, *, fetch=None):
    import gateway_checkout_attempts as attempts
    if isinstance(event,dict) and event.get('type')=='charge.refunded':
        return _refund_from_verified_webhook(event,fetch=fetch)
    if not isinstance(event,dict) or event.get('type') not in ('checkout.session.completed','checkout.session.async_payment_succeeded','checkout.session.expired'):
        return {'status':'ignored'}
    obj=((event.get('data') or {}).get('object') or {})
    meta=obj.get('metadata') if isinstance(obj,dict) else None
    attempt_id=meta.get('anyaicam_gateway_attempt') if isinstance(meta,dict) else None
    if not isinstance(attempt_id,str) or not attempt_id:
        # A missing metadata field is not an ownership hint. Only a durable
        # server-owned exact session binding can identify an existing attempt.
        session_id=obj.get('id') if isinstance(obj,dict) else None
        if not attempts.ATTEMPTS_SERVICE_READY or not isinstance(session_id,str) or not session_id:
            return {'status':'ignored'}
        from partner_db import row
        try:
            anchor=row('SELECT id FROM gateway_checkout_attempts WHERE session_id=?',(session_id,))
        except Exception:
            # No tenant can be identified when the optional schema is
            # unavailable. Gateway activation remains disabled, while
            # unrelated Full VMS billing continues through its own steps.
            return {'status':'unavailable'}
        if not anchor:
            # The event may belong to a creating attempt whose provider
            # response was lost. Do not guess a tenant or block unrelated
            # Checkout: retain only its signed identity for later exact match.
            event_id=event.get('id'); created=event.get('created')
            if not isinstance(event_id,str) or not event_id or type(created) is not int or created<0:
                return {'status':'ignored'}
            from partner_db import transaction
            try:
                with transaction() as db:
                    unknown=db.execute("SELECT 1 FROM gateway_checkout_attempts WHERE status IN ('creating','review') AND session_id IS NULL LIMIT 1").fetchone()
                    if not unknown: return {'status':'ignored'}
                    customer=obj.get('customer')
                    if isinstance(customer,dict): customer=customer.get('id')
                    if not isinstance(customer,str) or not customer: customer=None
                    from datetime import datetime, timezone
                    db.execute("INSERT INTO cloud_gateway_webhook_correlation(event_id,event_type,object_id,stripe_customer_id,event_created,status,subscription_id,created_at) VALUES(?,?,?,?,?,'unidentified',NULL,?) ON CONFLICT(event_id) DO NOTHING",
                               (event_id,event['type'],session_id,customer,created,datetime.now(timezone.utc).isoformat()))
                    stored=db.execute('SELECT event_type,object_id,stripe_customer_id,event_created FROM cloud_gateway_webhook_correlation WHERE event_id=?',(event_id,)).fetchone()
                    if not stored or tuple(stored[key] for key in ('event_type','object_id','stripe_customer_id','event_created')) != (event['type'],session_id,customer,created):
                        raise attempts.AttemptConflict('Quarantined event identity conflicts.')
                return {'status':'quarantined'}
            except Exception:
                # An unavailable optional queue must not break unrelated VMS
                # billing; Gateway activation remains separately disabled.
                return {'status':'unavailable'}
        attempt_id=anchor['id']
    if not attempts.ATTEMPTS_SERVICE_READY:
        raise attempts.AttemptConflict('Gateway attempt recording is unavailable.')
    session_id=obj.get('id'); event_id=event.get('id')
    if not isinstance(session_id,str) or not session_id or not isinstance(event_id,str) or not event_id:
        raise attempts.AttemptConflict('Verified event identity is incomplete.')
    import stripe_state, billing_status
    from partner_db import transaction
    reader=fetch or stripe_state.stripe_get
    canonical=reader('/v1/checkout/sessions/'+quote(session_id,safe=''),'the Gateway checkout session')
    items=reader('/v1/checkout/sessions/'+quote(session_id,safe='')+'/line_items','the Gateway checkout items')
    if not isinstance(canonical,dict) or canonical.get('id')!=session_id or not isinstance(items,dict):
        raise attempts.AttemptConflict('Canonical session unavailable.')
    invoice=canonical.get('invoice')
    invoice_id=invoice.get('id') if isinstance(invoice,dict) else invoice
    if canonical.get('payment_status')=='paid' and (not isinstance(invoice_id,str) or not invoice_id):
        raise attempts.AttemptConflict('Paid session invoice identity unavailable.')
    with transaction() as db:
        billing_status.lock_payment(db,billing_status.payment_keys('', '', invoice_id or ''))
        record=db.execute('SELECT * FROM gateway_checkout_attempts WHERE id=?',(attempt_id,)).fetchone()
        data=items.get('data')
        if (not record or not isinstance(data,list) or len(data)!=1 or items.get('has_more') is not False
                or not isinstance(data[0],dict) or type(data[0].get('quantity')) is not int
                or data[0]['quantity']!=record['quantity'] or not isinstance(data[0].get('price'),dict)
                or data[0]['price'].get('id')!=record['stripe_price_id']):
            raise attempts.AttemptConflict('Session price or quantity does not match the attempt.')
        return attempts.observe_session(db,attempt_id=attempt_id,session=canonical,
                                        event_id=event_id if canonical.get('payment_status')=='paid' else None)
