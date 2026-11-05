"""Prepared authorization-bound Gateway checkout state; no provider calls.

All mutations require the caller's partner_db.transaction(). This service is
not mounted and never grants an entitlement or refunds a payment.
"""
import json
import os
from datetime import datetime, timezone

ATTEMPTS_SERVICE_READY = False
PAID_ACTIVATION_READY = False


class AttemptConflict(Exception):
    pass


def _actor(actor, customer_id):
    if (not isinstance(actor,dict) or actor.get('role') != 'customer_owner'
            or actor.get('customer_id') != customer_id or not actor.get('email')):
        raise AttemptConflict('Authenticated customer owner required.')


def _current_decision(db, customer_id, appliance_id, edition):
    from gateway_internal_provisioning import edition_authorization_effective
    if not edition_authorization_effective(db,customer_id,appliance_id,edition):
        return None
    rows=db.execute('SELECT id,claim_id,evidence_reference,evidence_version FROM appliance_edition_provisioning_decisions WHERE customer_id=? AND appliance_id=? AND edition=? AND revoked_at IS NULL',
                    (customer_id,appliance_id,edition)).fetchall()
    return rows[0] if len(rows)==1 else None


def _valid_attempt(db, attempt):
    decision=_current_decision(db,attempt['customer_id'],attempt['appliance_id'],attempt['edition'])
    if not decision or tuple(decision[k] for k in ('id','claim_id','evidence_reference','evidence_version')) != tuple(attempt[k] for k in ('decision_id','claim_id','authorization_reference','authorization_version')):
        return False
    binding=db.execute('SELECT stripe_customer_id FROM stripe_customer_bindings WHERE customer_id=?',(attempt['customer_id'],)).fetchone()
    price_env='ANYAICAM_STRIPE_PRICE_LOCAL_GATEWAY_REMOTE' if attempt['edition']=='local_gateway' else 'ANYAICAM_STRIPE_PRICE_CLOUD_GATEWAY'
    if not binding or binding['stripe_customer_id'] != attempt['stripe_customer_id'] or os.environ.get(price_env,'').strip() != attempt['stripe_price_id']:
        return False
    try:
        cameras=json.loads(attempt['camera_ids_json'])
    except (ValueError,TypeError):
        return False
    if (not isinstance(cameras,list) or any(not isinstance(camera,str) or not camera for camera in cameras)
            or len(cameras)!=len(set(cameras))):
        return False
    if attempt['edition']=='local_gateway':
        return cameras==[] and attempt['quantity']==1
    if not cameras or len(cameras)!=attempt['quantity']:
        return False
    from cloud_gateway_camera_entitlements import _camera_mapping
    return all(_camera_mapping(db,attempt['customer_id'],attempt['appliance_id'],camera) for camera in cameras)


def reserve(db, actor, *, attempt_id, customer_id, appliance_id, edition, stripe_customer_id, price_id, camera_ids):
    """Reserve one unresolved attempt for a verified owned claim and edition."""
    if not ATTEMPTS_SERVICE_READY: return {'status':'unavailable'}
    _actor(actor,customer_id)
    if (edition not in ('local_gateway','cloud_gateway') or
        any(not isinstance(value,str) or not value or value != value.strip() or len(value)>128
            for value in (attempt_id,customer_id,appliance_id,stripe_customer_id,price_id)) or
        not isinstance(camera_ids,list) or len(camera_ids)>100 or
        any(not isinstance(value,str) or not value or value != value.strip() or len(value)>128 for value in camera_ids) or
        len(set(camera_ids))!=len(camera_ids)):
        raise AttemptConflict('Invalid checkout identity or camera selection.')
    configured=os.environ.get('ANYAICAM_STRIPE_PRICE_LOCAL_GATEWAY_REMOTE' if edition=='local_gateway' else 'ANYAICAM_STRIPE_PRICE_CLOUD_GATEWAY','').strip()
    if not configured.startswith('price_') or price_id != configured:
        raise AttemptConflict('Price is not configured for this edition.')
    if edition=='cloud_gateway':
        from cloud_gateway_camera_entitlements import cloud_price_classification
        if not camera_ids or not cloud_price_classification(price_id): raise AttemptConflict('Cloud camera selection or price invalid.')
    elif camera_ids:
        raise AttemptConflict('Local billing is per system.')
    bound=db.execute('SELECT stripe_customer_id FROM stripe_customer_bindings WHERE customer_id=?',(customer_id,)).fetchone()
    if not bound or bound['stripe_customer_id']!=stripe_customer_id: raise AttemptConflict('Stripe customer binding invalid.')
    decision=_current_decision(db,customer_id,appliance_id,edition)
    if not decision: raise AttemptConflict('Claim or edition authorization unavailable.')
    cameras=sorted(camera_ids); quantity=len(cameras) if edition=='cloud_gateway' else 1
    expected={'id':attempt_id,'customer_id':customer_id,'appliance_id':appliance_id,'claim_id':decision['claim_id'],
              'edition':edition,'decision_id':decision['id'],'authorization_reference':decision['evidence_reference'],
              'authorization_version':decision['evidence_version'],'stripe_customer_id':stripe_customer_id,
              'stripe_price_id':price_id,'quantity':quantity,'camera_ids_json':json.dumps(cameras,separators=(',',':'))}
    if not _valid_attempt(db,expected): raise AttemptConflict('Camera mapping or authorization changed.')
    old=db.execute('SELECT * FROM gateway_checkout_attempts WHERE id=?',(attempt_id,)).fetchone()
    if old:
        if all(old[key]==value for key,value in expected.items()): return {'status':'existing','attempt_id':attempt_id}
        raise AttemptConflict('Checkout attempt identity conflicts.')
    if db.execute("SELECT 1 FROM gateway_checkout_attempts WHERE appliance_id=? AND status IN ('paid_review','late_paid_review')",(appliance_id,)).fetchone():
        raise AttemptConflict('A payment requires reconciliation.')
    now=datetime.now(timezone.utc).isoformat()
    fields=list(expected); values=[expected[key] for key in fields]
    try:
        db.execute('INSERT INTO gateway_checkout_attempts('+','.join(fields)+',status,created_at,updated_at) VALUES('+','.join('?' for _ in fields)+",'reserved',?,?)",tuple(values)+(now,now))
    except Exception as error:
        raise AttemptConflict('An unresolved attempt already exists or storage is unavailable.') from error
    from partner_db import audit
    audit(actor,'gateway.checkout_reserved','gateway_checkout_attempt',attempt_id,{'customer_id':customer_id,'appliance_id':appliance_id,'edition':edition,'quantity':quantity,'decision_id':decision['id'],'authorization_version':decision['evidence_version']})
    return {'status':'reserved','attempt_id':attempt_id}


def begin(db, actor, *, customer_id, attempt_id):
    """Return a stable provider key only while the same authority is current."""
    if not ATTEMPTS_SERVICE_READY: return {'status':'unavailable'}
    _actor(actor,customer_id)
    attempt=db.execute('SELECT * FROM gateway_checkout_attempts WHERE id=? AND customer_id=?',(attempt_id,customer_id)).fetchone()
    if not attempt or attempt['status'] not in ('reserved','creating') or not _valid_attempt(db,attempt):
        raise AttemptConflict('Checkout authorization changed or attempt is unavailable.')
    changed=db.execute("UPDATE gateway_checkout_attempts SET status='creating',updated_at=? WHERE id=? AND customer_id=? AND status IN ('reserved','creating')",(datetime.now(timezone.utc).isoformat(),attempt_id,customer_id)).rowcount
    if changed != 1: raise AttemptConflict('Checkout attempt changed.')
    return {'status':'creating','idempotency_key':'anyaicam-gateway-attempt-'+attempt_id}


def exact_paid_attempt_authorized(db, *, attempt_id, customer_id, appliance_id, edition,
                                  price_id, quantity, session, subscription):
    """Revalidate an exact recorded paid attempt; this function never grants service.

    Caller owns the transaction and payment locks, and supplies independently
    fetched canonical provider objects. A separate activation gate remains off.
    """
    if not ATTEMPTS_SERVICE_READY or type(quantity) is not int or quantity < 1:
        return False
    try:
        attempt=db.execute('SELECT * FROM gateway_checkout_attempts WHERE id=?',(attempt_id,)).fetchone()
        if not attempt or tuple(attempt[k] for k in ('customer_id','appliance_id','edition','stripe_price_id','quantity')) != (customer_id,appliance_id,edition,price_id,quantity):
            return False
        if (attempt['status']!='paid_review' or attempt['authorization_invalidated'] != 0 or attempt['reversal_state']!='none'
                or not attempt['payment_event_id'] or not isinstance(attempt['stripe_invoice_id'],str) or not attempt['stripe_invoice_id']
                or not isinstance(attempt['stripe_subscription_id'],str) or not attempt['stripe_subscription_id'] or not _valid_attempt(db,attempt)):
            return False
        from billing_status import reversal_reason
        if reversal_reason('', '', attempt['stripe_invoice_id']): return False
        locked=db.execute("UPDATE gateway_checkout_attempts SET status=status WHERE id=? AND status='paid_review' AND payment_event_id=? AND session_id=? AND stripe_subscription_id=?",
                          (attempt_id,attempt['payment_event_id'],attempt['session_id'],attempt['stripe_subscription_id'])).rowcount
        if locked!=1: return False
        if not isinstance(session,dict) or not isinstance(subscription,dict): return False
        meta=session.get('metadata'); items=(subscription.get('items') or {}).get('data') if isinstance(subscription.get('items'),dict) else None
        sub_id=session.get('subscription'); sub_id=sub_id.get('id') if isinstance(sub_id,dict) else sub_id
        invoice_id=session.get('invoice'); invoice_id=invoice_id.get('id') if isinstance(invoice_id,dict) else invoice_id
        if (not isinstance(meta,dict) or meta.get('anyaicam_gateway_attempt')!=attempt_id
                or session.get('id')!=attempt['session_id'] or session.get('customer')!=attempt['stripe_customer_id']
                or session.get('mode')!='subscription' or session.get('status')!='complete' or session.get('payment_status')!='paid'
                or sub_id!=attempt['stripe_subscription_id'] or subscription.get('id')!=sub_id or invoice_id!=attempt['stripe_invoice_id']
                or subscription.get('customer')!=attempt['stripe_customer_id'] or subscription.get('status')!='active'
                or not isinstance(items,list) or len(items)!=1 or not isinstance(items[0],dict)
                or type(items[0].get('quantity')) is not int or items[0]['quantity']!=quantity
                or not isinstance(items[0].get('price'),dict) or items[0]['price'].get('id')!=price_id):
            return False
        return True
    except Exception:
        return False


def record_verified_full_refund(db, *, attempt_id, event_id, charge, invoice):
    """Record a full refund for the attempt's exact paid invoice; never refund.

    Caller verifies the signed event, fetches canonical objects and holds the
    existing ordered payment locks in the caller-owned transaction.
    """
    if not ATTEMPTS_SERVICE_READY: return {'status':'unavailable'}
    if not isinstance(event_id,str) or not event_id or not isinstance(charge,dict) or not isinstance(invoice,dict):
        raise AttemptConflict('Refund evidence is incomplete.')
    attempt=db.execute('SELECT * FROM gateway_checkout_attempts WHERE id=?',(attempt_id,)).fetchone()
    def ident(value): return value.get('id') if isinstance(value,dict) else value
    invoice_id=ident(charge.get('invoice')); charge_id=charge.get('id'); intent=ident(charge.get('payment_intent'))
    amount=charge.get('amount'); refunded=charge.get('amount_refunded')
    if (not attempt or attempt['status'] not in ('paid_review','late_paid_review') or not attempt['payment_event_id']
            or not isinstance(invoice_id,str) or not invoice_id or invoice_id!=attempt['stripe_invoice_id']
            or invoice.get('id')!=invoice_id or ident(invoice.get('subscription'))!=attempt['stripe_subscription_id']
            or ident(invoice.get('customer'))!=attempt['stripe_customer_id'] or ident(charge.get('customer'))!=attempt['stripe_customer_id']
            or not isinstance(charge_id,str) or not charge_id or not isinstance(intent,str) or not intent
            or ident(invoice.get('payment_intent'))!=intent or charge.get('status')!='succeeded'
            or charge.get('currency')!='usd' or invoice.get('currency')!='usd'
            or type(amount) is not int or amount<=0 or type(refunded) is not int or refunded!=amount
            or type(charge.get('refunded')) is not bool or charge['refunded'] is not True
            or type(invoice.get('amount_paid')) is not int or invoice['amount_paid']!=amount
            or type(charge.get('livemode')) is not bool or invoice.get('livemode') is not charge['livemode']):
        raise AttemptConflict('Refund evidence does not match the paid attempt.')
    if attempt['reversal_state']=='refunded':
        if attempt['refund_charge_id']==charge_id: return {'status':'refunded','duplicate':True}
        raise AttemptConflict('Refund charge conflicts with recorded evidence.')
    import billing_status
    if billing_status.reversal_reason(charge_id,intent,invoice_id)!='refunded':
        raise AttemptConflict('Full refund is not present in the verified reversal ledger.')
    changed=db.execute("UPDATE gateway_checkout_attempts SET reversal_state='refunded',refund_charge_id=?,refund_event_id=?,updated_at=? WHERE id=? AND stripe_invoice_id=? AND reversal_state='none' AND status IN ('paid_review','late_paid_review')",
                       (charge_id,event_id,datetime.now(timezone.utc).isoformat(),attempt_id,invoice_id)).rowcount
    if changed!=1: raise AttemptConflict('Attempt changed during refund reconciliation.')
    from partner_db import audit
    audit({'email':'stripe-webhook','role':'system'},'gateway.checkout_refund_observed','gateway_checkout_attempt',attempt_id,
          {'customer_id':attempt['customer_id'],'invoice_id':invoice_id,'charge_id':charge_id,'event_id':event_id})
    return {'status':'refunded'}


def observe_session(db, *, attempt_id, session, event_id=None):
    """Record a canonical provider session; paid sessions always await review.

    The caller must fetch this session via the existing authenticated Stripe
    client and verify the event signature. No HTTP route invokes this function.
    """
    if not ATTEMPTS_SERVICE_READY: return {'status':'unavailable'}
    attempt=db.execute('SELECT * FROM gateway_checkout_attempts WHERE id=?',(attempt_id,)).fetchone()
    meta=session.get('metadata') if isinstance(session,dict) else None
    if (not attempt or not isinstance(session,dict) or not isinstance(meta,dict)
            or meta.get('anyaicam_gateway_attempt') != attempt_id or session.get('mode') != 'subscription'
            or session.get('customer') != attempt['stripe_customer_id'] or not isinstance(session.get('id'),str)
            or not session['id'] or (attempt['session_id'] and attempt['session_id'] != session['id'])
            or session.get('status') not in ('open','complete','expired')
            or session.get('payment_status') not in ('paid','unpaid','no_payment_required')):
        raise AttemptConflict('Canonical session does not match the attempt.')
    paid=session['payment_status']=='paid'
    if paid and (not isinstance(event_id,str) or not event_id): raise AttemptConflict('Verified payment event required.')
    subscription=session.get('subscription')
    if isinstance(subscription,dict): subscription=subscription.get('id')
    invoice=session.get('invoice')
    if isinstance(invoice,dict): invoice=invoice.get('id')
    if paid and (session['status']!='complete' or not isinstance(subscription,str) or not subscription or not isinstance(invoice,str) or not invoice):
        raise AttemptConflict('Paid subscription identity unavailable.')
    if attempt['stripe_subscription_id'] and subscription and attempt['stripe_subscription_id'] != subscription:
        raise AttemptConflict('Subscription conflicts with recorded evidence.')
    if attempt['stripe_invoice_id'] and invoice and attempt['stripe_invoice_id'] != invoice:
        raise AttemptConflict('Invoice conflicts with recorded evidence.')
    if attempt['status'] in ('paid_review','late_paid_review'):
        # Distinct signed Stripe events may report the same canonical paid
        # session. The existing event ledger records each delivery; preserve
        # the first payment evidence and never grant service on a replay.
        if paid and attempt['payment_event_id']: return {'status':attempt['status'],'duplicate':True}
        raise AttemptConflict('Paid attempt requires reconciliation.')
    if attempt['payment_event_id'] and attempt['payment_event_id'] != event_id:
        raise AttemptConflict('Payment event conflicts with recorded evidence.')
    if attempt['status']=='expired' and not paid:
        return {'status':'expired','duplicate':True}
    try:
        current=_valid_attempt(db,attempt)
    except Exception:
        current=False
    status=('late_paid_review' if attempt['status']=='expired' else 'paid_review') if paid else ('review' if not current or attempt['status']=='review' else ('expired' if session['status']=='expired' else 'open'))
    if session['status']=='complete' and not paid: status='review'
    invalidated=int(attempt['authorization_invalidated'] != 0 or not current or attempt['status']=='review')
    changed=db.execute('UPDATE gateway_checkout_attempts SET status=?,authorization_invalidated=?,session_id=?,stripe_subscription_id=?,stripe_invoice_id=?,payment_event_id=?,updated_at=? WHERE id=? AND status=?',
                       (status,invalidated,session['id'],subscription if paid else attempt['stripe_subscription_id'],invoice if paid else attempt['stripe_invoice_id'],event_id if paid else None,datetime.now(timezone.utc).isoformat(),attempt_id,attempt['status'])).rowcount
    if changed!=1: raise AttemptConflict('Checkout attempt changed.')
    from partner_db import audit
    audit({'email':'stripe-webhook','role':'system'},'gateway.checkout_session_observed','gateway_checkout_attempt',attempt_id,
          {'customer_id':attempt['customer_id'],'status':status,'session_id':session['id'],'event_id':event_id if paid else None})
    return {'status':status,'authorization_current':bool(current)}
