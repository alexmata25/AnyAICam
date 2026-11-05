"""Cloud Gateway verified webhook integration, disabled pending acceptance."""
from datetime import datetime, timezone

WEBHOOK_PRODUCER_READY = False
PRODUCT = 'cloud_gateway'


class EvidenceConflict(Exception):
    pass


def checkout_fields(*, customer_id, stripe_customer_id, price_id, quantity, success_url, cancel_url):
    """Prepare a server-owned checkout anchor; never calls the provider."""
    from cloud_gateway_camera_entitlements import cloud_price_classification
    if (not all(isinstance(v,str) and v and v == v.strip() for v in
                (customer_id,stripe_customer_id,price_id,success_url,cancel_url))
            or type(quantity) is not int or quantity < 1 or not cloud_price_classification(price_id)):
        raise ValueError('Invalid Cloud Gateway checkout anchor.')
    fields=[('mode','subscription'),('customer',stripe_customer_id),('client_reference_id',customer_id),
            ('line_items[0][price]',price_id),('line_items[0][quantity]',str(quantity)),
            ('success_url',success_url),('cancel_url',cancel_url)]
    for key,value in {'anyaicam_product':PRODUCT,'anyaicam_customer_id':customer_id}.items():
        fields.extend(((f'metadata[{key}]',value),(f'subscription_data[metadata][{key}]',value)))
    return 'cloud_gateway',fields


def sync_from_verified_webhook(event, *, fetch=None):
    """Verified-dispatcher step using canonical provider reads and ledger locks."""
    event_type = str(event.get('type') or '') if isinstance(event,dict) else ''
    obj = ((event.get('data') or {}).get('object') or {}) if isinstance(event,dict) else {}
    metadata = obj.get('metadata') if isinstance(obj,dict) else None
    checkout_event = event_type in {'checkout.session.completed','checkout.session.async_payment_succeeded'}
    supported = (checkout_event or event_type.startswith('customer.subscription.') or
                 event_type in {'invoice.paid','invoice.payment_succeeded','invoice.payment_failed',
                                'charge.refunded','charge.dispute.created','charge.dispute.closed'})
    if not supported or not isinstance(obj,dict):
        return {'status':'ignored'}
    explicit = isinstance(metadata,dict) and metadata.get('anyaicam_product') == PRODUCT
    if checkout_event and not explicit:
        return {'status':'ignored'}
    if not WEBHOOK_PRODUCER_READY:
        if explicit:
            from stripe_state import RetryableStripeEventError
            raise RetryableStripeEventError('Cloud Gateway billing producer is disabled.')
        return {'status':'ignored'}
    from urllib.parse import quote
    import stripe_state, billing_status
    from partner_db import row, transaction
    reader = fetch or stripe_state.stripe_get
    def get(path, description):
        found=reader(path,description)
        if not isinstance(found,dict): raise stripe_state.RetryableStripeEventError('Provider response unavailable.')
        return found
    event_id=event.get('id'); event_created=event.get('created')
    if not isinstance(event_id,str) or not event_id or type(event_created) is not int:
        raise stripe_state.RetryableStripeEventError('Cloud Gateway event identity is invalid.')
    object_id=_id(obj.get('id')) or _id(obj.get('charge'))
    stripe_customer_hint=_id(obj.get('customer'))
    if not object_id:
        raise stripe_state.RetryableStripeEventError('Cloud Gateway event object identity is missing.')
    # Fence known subscriptions before any provider request. Holds are per
    # event, so an older successful worker cannot clear a newer failed one.
    if checkout_event:
        column, identity = 'checkout_session_id', _id(obj.get('id'))
    elif event_type.startswith('customer.subscription.'):
        column, identity = 'stripe_subscription_id', _id(obj.get('id'))
    elif event_type.startswith('invoice.'):
        column, identity = 'invoice_id', _id(obj.get('id'))
        if _id(obj.get('subscription')):
            column, identity = 'stripe_subscription_id', _id(obj.get('subscription'))
    else:
        column, identity = 'charge_id', _id(obj.get('charge') if event_type.startswith('charge.dispute.') else obj.get('id'))
    if object_id:
        from partner_db import audit
        from database_backend import lock_customer_camera_capacity
        with transaction() as db:
            db.execute("INSERT INTO cloud_gateway_webhook_correlation(event_id,event_type,object_id,stripe_customer_id,event_created,status,subscription_id,created_at) VALUES(?,?,?,?,?,'pending',NULL,?) ON CONFLICT(event_id) DO NOTHING",
                       (event_id,event_type,object_id,stripe_customer_hint or None,event_created,datetime.now(timezone.utc).isoformat()))
            correlation=db.execute('SELECT event_type,object_id,stripe_customer_id,event_created FROM cloud_gateway_webhook_correlation WHERE event_id=?',(event_id,)).fetchone()
            if not correlation or (correlation['event_type'],correlation['object_id'],correlation['stripe_customer_id'],correlation['event_created']) != (event_type,object_id,stripe_customer_hint or None,event_created):
                raise stripe_state.RetryableStripeEventError('Cloud Gateway event identity conflict.')
            candidates=db.execute('SELECT customer_id,stripe_subscription_id,event_id FROM cloud_gateway_subscription_evidence WHERE '+column+'=?',(identity,)).fetchall()
            # Earlier invoice and charge IDs remain tied to the subscription
            # that was canonically verified when that payment was current.
            if not candidates and identity and column in ('invoice_id','charge_id'):
                kind='invoice' if column=='invoice_id' else 'charge'
                candidates=db.execute('SELECT e.customer_id,e.stripe_subscription_id,e.event_id FROM gateway_stripe_payment_links l JOIN cloud_gateway_subscription_evidence e ON e.stripe_subscription_id=l.stripe_subscription_id AND e.customer_id=l.customer_id AND e.stripe_customer_id=l.stripe_customer_id WHERE l.kind=? AND l.provider_id=?',
                                      (kind,identity)).fetchall()
            # A signed first-class Stripe customer ID can fence that customer's
            # subscriptions while an older charge/invoice is being resolved.
            # No metadata assertion is used to choose a customer here.
            if not candidates and stripe_customer_hint:
                candidates=db.execute('SELECT customer_id,stripe_subscription_id,event_id FROM cloud_gateway_subscription_evidence WHERE stripe_customer_id=?',(stripe_customer_hint,)).fetchall()
            for candidate in candidates:
                customer=candidate['customer_id']; sub_id=candidate['stripe_subscription_id']
                if not lock_customer_camera_capacity(db,customer):
                    raise stripe_state.RetryableStripeEventError('Cloud Gateway customer unavailable.')
                # A fully reconciled duplicate needs no new hold.
                if candidate['event_id'] == event_id: continue
                added=db.execute('INSERT INTO cloud_gateway_evidence_holds(stripe_subscription_id,customer_id,event_id,event_created,created_at) VALUES(?,?,?,?,?) ON CONFLICT(stripe_subscription_id,event_id) DO NOTHING',
                                 (sub_id,customer,event_id,event_created,datetime.now(timezone.utc).isoformat())).rowcount
                if added:
                    audit({'email':'stripe-webhook','role':'system'},'cloud_gateway.subscription_verification_held','cloud_gateway_subscription',sub_id,
                          {'customer_id':customer,'event_id':event_id})
    checkout=None; event_invoice=None; subscription_id=''; session_id=''; customer_id=''
    if checkout_event:
        session_id=_id(obj.get('id'))
        if not session_id: raise stripe_state.RetryableStripeEventError('Checkout ID missing.')
        checkout=get('/v1/checkout/sessions/'+quote(session_id,safe=''),'the Cloud Gateway checkout')
        canonical_meta=checkout.get('metadata') or {}
        if not isinstance(canonical_meta,dict) or canonical_meta.get('anyaicam_product') != PRODUCT:
            raise stripe_state.RetryableStripeEventError('Cloud Gateway checkout classification mismatch.')
        customer_id=str(canonical_meta.get('anyaicam_customer_id') or '')
        subscription_id=_id(checkout.get('subscription'))
    elif event_type.startswith('customer.subscription.'):
        subscription_id=_id(obj.get('id'))
    elif event_type.startswith('invoice.'):
        subscription_id=_id(obj.get('subscription'))
    else:
        charge_id=_id(obj.get('charge')) if event_type.startswith('charge.dispute.') else _id(obj.get('id'))
        if not charge_id: return {'status':'ignored'}
        charge_event=get('/v1/charges/'+quote(charge_id,safe=''),'the event charge')
        if _id(charge_event.get('id')) != charge_id:
            raise stripe_state.RetryableStripeEventError('Cloud Gateway event charge mismatch.')
        invoice_event_id=_id(charge_event.get('invoice'))
        if not invoice_event_id: return {'status':'ignored'}
        event_invoice=get('/v1/invoices/'+quote(invoice_event_id,safe=''),'the event invoice')
        if (_id(event_invoice.get('id')) != invoice_event_id or
                (_id(charge_event.get('customer')) and _id(charge_event.get('customer')) != _id(event_invoice.get('customer')))):
            raise stripe_state.RetryableStripeEventError('Cloud Gateway event invoice mismatch.')
        subscription_id=_id(event_invoice.get('subscription'))
    if not subscription_id:
        if explicit: raise stripe_state.RetryableStripeEventError('Cloud Gateway subscription ID missing.')
        return {'status':'ignored'}
    if not checkout_event:
        binding=row('SELECT customer_id,checkout_session_id FROM cloud_gateway_subscription_evidence WHERE stripe_subscription_id=?',(subscription_id,))
        if not binding:
            if explicit: raise stripe_state.RetryableStripeEventError('Cloud Gateway subscription anchor missing.')
            return {'status':'ignored'}
        customer_id,session_id=binding['customer_id'],binding['checkout_session_id']
        # Once the canonical object graph identifies a stored subscription,
        # fence it before any further provider reads, including retry failures.
        from partner_db import audit
        from database_backend import lock_customer_camera_capacity
        with transaction() as db:
            if not lock_customer_camera_capacity(db,customer_id):
                raise stripe_state.RetryableStripeEventError('Cloud Gateway customer unavailable.')
            anchored=db.execute('SELECT event_id FROM cloud_gateway_subscription_evidence WHERE customer_id=? AND stripe_subscription_id=? AND checkout_session_id=?',
                                (customer_id,subscription_id,session_id)).fetchone()
            if not anchored: raise stripe_state.RetryableStripeEventError('Cloud Gateway anchor changed.')
            if anchored['event_id'] != event_id:
                added=db.execute('INSERT INTO cloud_gateway_evidence_holds(stripe_subscription_id,customer_id,event_id,event_created,created_at) VALUES(?,?,?,?,?) ON CONFLICT(stripe_subscription_id,event_id) DO NOTHING',
                                 (subscription_id,customer_id,event_id,event_created,datetime.now(timezone.utc).isoformat())).rowcount
                if added:
                    audit({'email':'stripe-webhook','role':'system'},'cloud_gateway.subscription_verification_held','cloud_gateway_subscription',subscription_id,
                          {'customer_id':customer_id,'event_id':event_id})
    if not customer_id or not session_id:
        raise stripe_state.RetryableStripeEventError('Cloud Gateway customer anchor missing.')
    def snapshots():
        subscription=get('/v1/subscriptions/'+quote(subscription_id,safe=''),'the Cloud Gateway subscription')
        if _id(subscription.get('id')) != subscription_id:
            raise stripe_state.RetryableStripeEventError('Cloud Gateway subscription mismatch.')
        invoice=charge=None
        if subscription.get('status') == 'active':
            invoice_id=_id(subscription.get('latest_invoice'))
            if not invoice_id: raise stripe_state.RetryableStripeEventError('Cloud Gateway invoice missing.')
            invoice=get('/v1/invoices/'+quote(invoice_id,safe=''),'the Cloud Gateway invoice')
            if invoice.get('status') == 'paid':
                charge_id=_id(invoice.get('charge'))
                if not charge_id: raise stripe_state.RetryableStripeEventError('Cloud Gateway charge missing.')
                charge=get('/v1/charges/'+quote(charge_id,safe=''),'the Cloud Gateway charge')
        return subscription,invoice,charge
    first_sub,first_invoice,first_charge=snapshots()
    stripe_customer_id=_id(first_sub.get('customer'))
    if (not stripe_customer_id or stripe_state.bound_elsewhere(stripe_customer_id,customer_id)
            or (event_invoice is not None and _id(event_invoice.get('customer')) != stripe_customer_id)):
        raise stripe_state.RetryableStripeEventError('Cloud Gateway customer binding mismatch.')
    def payment_ids(invoice,charge):
        return (_id((charge or {}).get('id')),_id((invoice or {}).get('payment_intent')),_id((invoice or {}).get('id')))
    first_keys=payment_ids(first_invoice,first_charge)
    try:
        with transaction() as db:
            billing_status.lock_payment(db,billing_status.payment_keys(*first_keys))
            from database_backend import lock_customer_camera_capacity
            if not lock_customer_camera_capacity(db,customer_id): raise EvidenceConflict('Customer not found.')
            current,invoice,charge=snapshots()
            if payment_ids(invoice,charge) != first_keys or _id(current.get('customer')) != stripe_customer_id:
                raise stripe_state.RetryableStripeEventError('Cloud Gateway payment changed during reconciliation.')
            reversal=billing_status.reversal_reason(*first_keys)
            if reversal and charge:
                charge=dict(charge)
                if reversal == 'disputed': charge['disputed']=True
                elif reversal == 'refunded': charge['refunded']=True
                else: raise stripe_state.RetryableStripeEventError('Unknown payment reversal.')
            result=reconcile_verified_snapshot(db,event_id=event_id,event_created=event_created,customer_id=customer_id,
                      session_id=session_id,subscription=current,invoice=invoice,charge=charge,checkout=checkout)
            if checkout_event and result.get('status') not in {'active','already_processed'}:
                raise stripe_state.RetryableStripeEventError('Cloud Gateway checkout not provisioned.')
            cleared=db.execute('DELETE FROM cloud_gateway_evidence_holds WHERE stripe_subscription_id=? AND customer_id=? AND event_id=?',
                               (subscription_id,customer_id,event_id)).rowcount
            if cleared:
                from partner_db import audit
                audit({'email':'stripe-webhook','role':'system'},'cloud_gateway.subscription_verification_released','cloud_gateway_subscription',subscription_id,
                      {'customer_id':customer_id,'event_id':event_id})
            if event_invoice is not None:
                # The canonical charge -> invoice -> subscription graph has
                # identified the exact subscription and customer. Release
                # only this event's other candidate holds for that same
                # customer; never clear another tenant's hold.
                others=db.execute('SELECT h.stripe_subscription_id FROM cloud_gateway_evidence_holds h JOIN cloud_gateway_subscription_evidence e ON e.stripe_subscription_id=h.stripe_subscription_id AND e.customer_id=h.customer_id WHERE h.event_id=? AND h.customer_id=? AND e.stripe_customer_id=? AND h.stripe_subscription_id<>?',
                                  (event_id,customer_id,stripe_customer_id,subscription_id)).fetchall()
                for other in others:
                    other_id=other['stripe_subscription_id']
                    removed=db.execute('DELETE FROM cloud_gateway_evidence_holds WHERE stripe_subscription_id=? AND customer_id=? AND event_id=?',
                                       (other_id,customer_id,event_id)).rowcount
                    if removed:
                        from partner_db import audit
                        audit({'email':'stripe-webhook','role':'system'},'cloud_gateway.subscription_verification_released','cloud_gateway_subscription',other_id,
                              {'customer_id':customer_id,'event_id':event_id})
            remaining=db.execute('SELECT 1 FROM cloud_gateway_evidence_holds WHERE event_id=? LIMIT 1',(event_id,)).fetchone()
            if not remaining:
                db.execute("UPDATE cloud_gateway_webhook_correlation SET status='resolved',subscription_id=? WHERE event_id=?",(subscription_id,event_id))
            return result
    except EvidenceConflict as error:
        raise stripe_state.RetryableStripeEventError('Cloud Gateway billing evidence rejected.') from error


def _id(value):
    return str((value.get('id') if isinstance(value,dict) else value) or '')


def retry_pending_correlations(*, fetch=None, limit=50, after=''):
    """Retry durable signed-event identities through Stripe's canonical Events API.

    A provider failure or an event that still cannot be mapped stays pending.
    The queue contains identities, not payment claims or client metadata.
    """
    if not WEBHOOK_PRODUCER_READY:
        return {'status':'unavailable'}
    from urllib.parse import quote
    from partner_db import rows, row
    import stripe_state
    reader=fetch or stripe_state.stripe_get
    records=rows("SELECT event_id,event_type,object_id,stripe_customer_id,event_created,status FROM cloud_gateway_webhook_correlation WHERE (status='pending' OR (status='unidentified' AND EXISTS (SELECT 1 FROM gateway_checkout_attempts a WHERE a.session_id=cloud_gateway_webhook_correlation.object_id))) AND event_id>? ORDER BY event_id LIMIT ?",
                 (after,max(1,min(int(limit),50))))
    resolved=failed=0
    for record in records:
        try:
            event=reader('/v1/events/'+quote(record['event_id'],safe=''),'the pending Stripe event')
            obj=((event.get('data') or {}).get('object') or {}) if isinstance(event,dict) else {}
            if (not isinstance(obj,dict) or not isinstance(event,dict) or event.get('id') != record['event_id']
                    or event.get('type') != record['event_type'] or event.get('created') != record['event_created']
                    or (_id(obj.get('id')) or _id(obj.get('charge'))) != record['object_id']
                    or (_id(obj.get('customer')) or None) != record['stripe_customer_id']):
                raise stripe_state.RetryableStripeEventError('Pending event identity mismatch.')
            if record['status']=='unidentified':
                from gateway_checkout_webhook import sync_from_verified_webhook as sync_attempt
                outcome=sync_attempt(event,fetch=reader)
                if outcome.get('status') not in {'ignored','unavailable','quarantined'}:
                    from partner_db import transaction
                    with transaction() as db:
                        db.execute("UPDATE cloud_gateway_webhook_correlation SET status='resolved' WHERE event_id=? AND status='unidentified'",(record['event_id'],))
            else:
                sync_from_verified_webhook(event,fetch=reader)
            state=row('SELECT status FROM cloud_gateway_webhook_correlation WHERE event_id=?',(record['event_id'],))
            if state and state['status']=='resolved':
                resolved += 1
        except Exception:
            failed += 1
    return {'status':'complete','selected':len(records),'reconciled':resolved,'failed':failed,
            'next_cursor':records[-1]['event_id'] if records else ''}


def _snapshot(subscription, invoice, charge, *, customer_id, stripe_customer_id, subscription_id, price_id):
    """Validate canonical provider snapshots, never a browser payment claim."""
    from cloud_gateway_camera_entitlements import cloud_price_classification
    if not isinstance(subscription,dict) or not cloud_price_classification(price_id):
        return None
    metadata = subscription.get('metadata')
    items = (subscription.get('items') or {}).get('data') if isinstance(subscription.get('items'),dict) else None
    if (subscription.get('id') != subscription_id or _id(subscription.get('customer')) != stripe_customer_id
            or not isinstance(metadata,dict) or metadata.get('anyaicam_product') != PRODUCT
            or metadata.get('anyaicam_customer_id') != customer_id or not isinstance(items,list) or len(items) != 1
            or not isinstance(items[0],dict)):
        return None
    item=items[0]; price=item.get('price'); quantity=item.get('quantity')
    if not isinstance(price,dict): return None
    recurring=price.get('recurring')
    if (price.get('id') != price_id or price.get('unit_amount') != 999 or type(price.get('unit_amount')) is not int
            or price.get('currency') != 'usd' or not isinstance(recurring,dict) or recurring.get('interval') != 'month'
            or type(recurring.get('interval_count')) is not int or recurring['interval_count'] != 1
            or type(quantity) is not int or quantity < 1):
        return None
    period=subscription.get('current_period_end')
    if type(period) is not int or period <= 0: return None
    stripe_status=subscription.get('status')
    if stripe_status in ('canceled','incomplete_expired'):
        return {'status':'cancelled','stripe_status':stripe_status,'quantity':quantity,'current_period_end':period,
                'invoice_id':None,'charge_id':None,'payment_intent_id':None,'payment_verified':False,'reversal':None}
    if stripe_status != 'active':
        return {'status':'pending','stripe_status':str(stripe_status or 'unknown'),'quantity':quantity,'current_period_end':period,
                'invoice_id':None,'charge_id':None,'payment_intent_id':None,'payment_verified':False,'reversal':None}
    invoice_id=_id(subscription.get('latest_invoice'))
    if (not invoice_id or not isinstance(invoice,dict) or invoice.get('id') != invoice_id
            or _id(invoice.get('subscription')) != subscription_id or _id(invoice.get('customer')) != stripe_customer_id):
        return None
    if invoice.get('status') != 'paid':
        return {'status':'pending','stripe_status':stripe_status,'quantity':quantity,'current_period_end':period,
                'invoice_id':invoice_id,'charge_id':None,'payment_intent_id':None,'payment_verified':False,'reversal':None}
    if (invoice.get('currency') != 'usd'
            or type(invoice.get('amount_paid')) is not int or invoice['amount_paid'] <= 0
            or not isinstance(charge,dict) or not charge.get('id') or _id(invoice.get('charge')) != charge['id']
            or not _id(invoice.get('payment_intent')) or _id(invoice.get('payment_intent')) != _id(charge.get('payment_intent'))
            or charge.get('status') != 'succeeded' or charge.get('currency') != 'usd'
            or type(charge.get('amount')) is not int or charge['amount'] != invoice['amount_paid']
            or type(charge.get('amount_refunded')) is not int or not 0 <= charge['amount_refunded'] <= charge['amount']
            or type(charge.get('refunded')) is not bool or type(charge.get('disputed')) is not bool):
        return None
    reversal = 'disputed' if charge['disputed'] else ('refunded' if charge['refunded'] or charge['amount_refunded'] == charge['amount'] else None)
    return {'status':'suspended' if reversal else 'active','stripe_status':stripe_status,'quantity':quantity,
            'current_period_end':period,'invoice_id':invoice_id,'charge_id':charge['id'],
            'payment_intent_id':_id(charge.get('payment_intent')),'payment_verified':not bool(reversal),'reversal':reversal}


def reconcile_verified_snapshot(db, *, event_id, event_created, customer_id, session_id, subscription, invoice, charge, checkout=None):
    """Prepared transaction reducer for already-verified server snapshots only."""
    if not WEBHOOK_PRODUCER_READY:
        return {'status':'unavailable'}
    if (not all(isinstance(v,str) and v and v == v.strip() for v in (event_id,customer_id,session_id))
            or type(event_created) is not int or event_created < 0 or not isinstance(subscription,dict)):
        raise EvidenceConflict('Invalid event identity.')
    from database_backend import lock_customer_camera_capacity
    if not lock_customer_camera_capacity(db,customer_id): raise EvidenceConflict('Customer not found.')
    subscription_id=_id(subscription.get('id')); stripe_customer_id=_id(subscription.get('customer'))
    if not subscription_id or not stripe_customer_id: raise EvidenceConflict('Subscription identity is incomplete.')
    bound=db.execute('SELECT stripe_customer_id FROM stripe_customer_bindings WHERE customer_id=?',(customer_id,)).fetchone()
    if not bound or bound['stripe_customer_id'] != stripe_customer_id:
        raise EvidenceConflict('Stripe customer binding mismatch.')
    existing=db.execute('SELECT * FROM cloud_gateway_subscription_evidence WHERE stripe_subscription_id=?',(subscription_id,)).fetchone()
    if existing and (existing['customer_id'] != customer_id or existing['stripe_customer_id'] != stripe_customer_id or existing['checkout_session_id'] != session_id):
        raise EvidenceConflict('Subscription belongs to another customer.')
    # A Checkout Session is an immutable anchor for one subscription. A new
    # subscription requires its own checkout/session, never reuse of an old
    # anchor or a silent reassignment across customers.
    session_bindings=db.execute('SELECT stripe_subscription_id,customer_id,stripe_customer_id FROM cloud_gateway_subscription_evidence WHERE checkout_session_id=?',(session_id,)).fetchall()
    if any((item['stripe_subscription_id'],item['customer_id'],item['stripe_customer_id']) !=
           (subscription_id,customer_id,stripe_customer_id) for item in session_bindings):
        raise EvidenceConflict('Checkout session is bound to another subscription.')
    if existing and event_id == existing['event_id']:
        return {'status':'already_processed'}
    if existing and event_created < existing['event_created']:
        return {'status':'stale'}
    price_id = existing['stripe_price_id'] if existing else None
    if not existing:
        # Hold the same checkout row against replacement while binding this
        # canonical session. SQLite already has the transaction write lock;
        # PostgreSQL takes the row lock on this conditional update.
        locked=db.execute("UPDATE checkout_pending SET status=status WHERE customer_id=? AND purchase='cloud_gateway' AND session_id=? AND status IN ('open','paid')",(customer_id,session_id)).rowcount
        if locked != 1: raise EvidenceConflict('Verified checkout anchor is unavailable.')
        pending=db.execute("SELECT price_id,quantity FROM checkout_pending WHERE customer_id=? AND purchase='cloud_gateway' AND session_id=? AND status IN ('open','paid')",(customer_id,session_id)).fetchone()
        if not pending: raise EvidenceConflict('Verified checkout anchor is unavailable.')
        meta=checkout.get('metadata') if isinstance(checkout,dict) else None
        if (not isinstance(checkout,dict) or checkout.get('id') != session_id or checkout.get('mode') != 'subscription'
                or checkout.get('payment_status') != 'paid' or _id(checkout.get('customer')) != stripe_customer_id
                or _id(checkout.get('subscription')) != subscription_id or not isinstance(meta,dict)
                or meta.get('anyaicam_product') != PRODUCT or meta.get('anyaicam_customer_id') != customer_id):
            raise EvidenceConflict('Paid checkout evidence is unavailable or mismatched.')
        price_id=pending['price_id']
    snapshot=_snapshot(subscription,invoice,charge,customer_id=customer_id,stripe_customer_id=stripe_customer_id,subscription_id=subscription_id,price_id=price_id)
    if not snapshot: raise EvidenceConflict('Provider evidence is incomplete or mismatched.')
    if not existing and pending['quantity'] != snapshot['quantity']:
        raise EvidenceConflict('Checkout quantity does not match subscription.')
    if not existing and isinstance(meta,dict) and meta.get('anyaicam_gateway_attempt'):
        try:
            import gateway_checkout_attempts as attempts
            anchor=db.execute('SELECT appliance_id FROM gateway_checkout_attempts WHERE id=? AND customer_id=?',
                              (meta['anyaicam_gateway_attempt'],customer_id)).fetchone()
            authorized=bool(anchor) and attempts.exact_paid_attempt_authorized(db,attempt_id=meta['anyaicam_gateway_attempt'],
                customer_id=customer_id,appliance_id=anchor['appliance_id'],edition='cloud_gateway',price_id=price_id,
                quantity=snapshot['quantity'],session=checkout,subscription=subscription)
            if not authorized or not attempts.PAID_ACTIVATION_READY:
                raise EvidenceConflict('Checkout attempt awaits explicit payment reconciliation.')
        except EvidenceConflict:
            raise
        except Exception as error:
            raise EvidenceConflict('Checkout attempt authorization unavailable.') from error
    # Distinct events in the same second cannot be ordered safely. Deny until
    # a later verified reconciliation rather than restoring an older state.
    if existing and event_created == existing['event_created']:
        snapshot={**snapshot,'status':'suspended','payment_verified':False,'reversal':'ambiguous_order'}
    from partner_db import audit
    now=datetime.now(timezone.utc).isoformat()
    # A harmless refresh or partial refund must not invalidate otherwise
    # current camera bindings. Material changes get a new evidence version.
    stable = bool(existing and existing['status']=='active' and existing['payment_verified']==1
                  and not existing['reversal'] and snapshot['status']=='active' and snapshot['payment_verified']
                  and not snapshot['reversal'] and existing['quantity']==snapshot['quantity']
                  and existing['current_period_end']==snapshot['current_period_end']
                  and existing['invoice_id']==snapshot['invoice_id'] and existing['charge_id']==snapshot['charge_id']
                  and existing['payment_intent_id']==snapshot['payment_intent_id'])
    source_version=existing['source_version'] if stable else event_id
    values=(customer_id,stripe_customer_id,price_id,session_id,PRODUCT,snapshot['quantity'],999,'usd','month',1,snapshot['status'],snapshot['stripe_status'],
            snapshot['current_period_end'],snapshot['invoice_id'],snapshot['charge_id'],snapshot['payment_intent_id'],int(snapshot['payment_verified']),snapshot['reversal'],source_version,event_created,event_id,now)
    if existing:
        changed=db.execute('UPDATE cloud_gateway_subscription_evidence SET customer_id=?,stripe_customer_id=?,stripe_price_id=?,checkout_session_id=?,product=?,quantity=?,amount_cents=?,currency=?,billing_interval=?,interval_count=?,status=?,stripe_status=?,current_period_end=?,invoice_id=?,charge_id=?,payment_intent_id=?,payment_verified=?,reversal=?,source_version=?,event_created=?,event_id=?,updated_at=? WHERE stripe_subscription_id=? AND event_id=?', values+(subscription_id,existing['event_id'])).rowcount
        if changed != 1: raise EvidenceConflict('Subscription changed during reconciliation.')
    else:
        db.execute('INSERT INTO cloud_gateway_subscription_evidence(customer_id,stripe_customer_id,stripe_price_id,checkout_session_id,product,quantity,amount_cents,currency,billing_interval,interval_count,status,stripe_status,current_period_end,invoice_id,charge_id,payment_intent_id,payment_verified,reversal,source_version,event_created,event_id,updated_at,stripe_subscription_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', values+(subscription_id,))
    for kind,provider_id in (('invoice',snapshot['invoice_id']),('charge',snapshot['charge_id']),('payment_intent',snapshot['payment_intent_id'])):
        if not provider_id: continue
        db.execute('INSERT INTO gateway_stripe_payment_links(kind,provider_id,customer_id,stripe_customer_id,stripe_subscription_id,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(kind,provider_id) DO NOTHING',
                   (kind,provider_id,customer_id,stripe_customer_id,subscription_id,now))
        linked=db.execute('SELECT customer_id,stripe_customer_id,stripe_subscription_id FROM gateway_stripe_payment_links WHERE kind=? AND provider_id=?',(kind,provider_id)).fetchone()
        if not linked or tuple(linked[key] for key in ('customer_id','stripe_customer_id','stripe_subscription_id')) != (customer_id,stripe_customer_id,subscription_id):
            raise EvidenceConflict('Payment identity belongs to another subscription.')
    actor={'email':'stripe-webhook','role':'system'}
    audit(actor,'cloud_gateway.subscription_evidence_updated','cloud_gateway_subscription',subscription_id,
          {'customer_id':customer_id,'event_id':event_id,'source_version':source_version,'status':snapshot['status'],'quantity':snapshot['quantity'],'invoice_id':snapshot['invoice_id']})
    if (existing and existing['status']=='active' and existing['payment_verified']==1 and not existing['reversal']
            and snapshot['status']=='active' and snapshot['payment_verified'] and not snapshot['reversal']
            and type(existing['quantity']) is int and source_version != existing['source_version']):
        from cloud_gateway_camera_entitlements import reconcile_paid_bindings
        reconcile_paid_bindings(db,actor,customer_id=customer_id,subscription_id=subscription_id,
                                     old_version=existing['source_version'],new_version=source_version,
                                     quantity=snapshot['quantity'])
    if snapshot['status'] in ('cancelled','suspended'):
        from cloud_gateway_camera_entitlements import retire_binding
        bindings=db.execute("SELECT id,appliance_id,camera_id,version FROM cloud_gateway_camera_entitlements WHERE customer_id=? AND billing_reference=? AND billing_source='verified_subscription' AND status IN ('pending','active','suspended')",(customer_id,subscription_id)).fetchall()
        for binding in bindings:
            retire_binding(db,actor,binding_id=binding['id'],customer_id=customer_id,appliance_id=binding['appliance_id'],camera_id=binding['camera_id'],version=binding['version'],reason='subscription_cancelled' if snapshot['status']=='cancelled' else 'entitlement_revoked')
    return {'status':snapshot['status'],'version':source_version}
