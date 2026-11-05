"""Bounded canonical Cloud Gateway refresh; no worker is enabled by default."""
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

REFRESH_WORKER_READY = False
REFRESH_INTERVAL_SECONDS = 120


def refresh_one(customer_id, subscription_id, *, fetch=None):
    """Claim one durable, token-fenced refresh attempt per subscription."""
    import time, uuid
    import stripe_state
    from partner_db import transaction
    from database_backend import lock_customer_camera_capacity
    from cloud_gateway_subscription_evidence import WEBHOOK_PRODUCER_READY
    if not WEBHOOK_PRODUCER_READY:
        return {'status':'unavailable'}
    token=uuid.uuid4().hex
    now=int(time.time())
    with transaction() as db:
        if not lock_customer_camera_capacity(db,customer_id):
            raise stripe_state.RetryableStripeEventError('Customer unavailable.')
        exists=db.execute('SELECT 1 FROM cloud_gateway_subscription_evidence WHERE customer_id=? AND stripe_subscription_id=?',(customer_id,subscription_id)).fetchone()
        if not exists: return {'status':'not_active'}
        claimed=db.execute('INSERT INTO cloud_gateway_refresh_leases(stripe_subscription_id,customer_id,token,lease_until) VALUES(?,?,?,?) ON CONFLICT(stripe_subscription_id) DO UPDATE SET customer_id=excluded.customer_id,token=excluded.token,lease_until=excluded.lease_until WHERE cloud_gateway_refresh_leases.lease_until<?',
                           (subscription_id,customer_id,token,now+600,now)).rowcount
        if not claimed: return {'status':'busy'}
    try:
        return _refresh_claimed(customer_id,subscription_id,token,fetch=fetch)
    finally:
        with transaction() as db:
            db.execute('DELETE FROM cloud_gateway_refresh_leases WHERE stripe_subscription_id=? AND customer_id=? AND token=?',(subscription_id,customer_id,token))


def _refresh_claimed(customer_id, subscription_id, token, *, fetch=None):
    """Renew only an unchanged paid snapshot, leaving lifecycle changes to webhooks.

    A persistent refresh hold prevents a timeout from retaining usable evidence.
    This does not generate a Stripe event or change webhook ordering/version.
    """
    import stripe_state, billing_status
    from partner_db import transaction, audit
    from database_backend import lock_customer_camera_capacity
    from cloud_gateway_subscription_evidence import _id, _snapshot, WEBHOOK_PRODUCER_READY
    if not WEBHOOK_PRODUCER_READY:
        return {'status':'unavailable'}
    reader=fetch or stripe_state.stripe_get
    hold='refresh:'+subscription_id
    with transaction() as db:
        if not lock_customer_camera_capacity(db,customer_id):
            raise stripe_state.RetryableStripeEventError('Customer unavailable.')
        record=db.execute('SELECT * FROM cloud_gateway_subscription_evidence WHERE customer_id=? AND stripe_subscription_id=?',(customer_id,subscription_id)).fetchone()
        if not record or record['status'] != 'active': return {'status':'not_active'}
        added=db.execute('INSERT INTO cloud_gateway_evidence_holds(stripe_subscription_id,customer_id,event_id,event_created,created_at) VALUES(?,?,?,?,?) ON CONFLICT(stripe_subscription_id,event_id) DO NOTHING',
                         (subscription_id,customer_id,hold,record['event_created'],datetime.now(timezone.utc).isoformat())).rowcount
        if added:
            audit({'email':'stripe-webhook','role':'system'},'cloud_gateway.subscription_refresh_held','cloud_gateway_subscription',subscription_id,{'customer_id':customer_id})
    def get(path):
        result=reader(path,'Cloud Gateway refresh')
        if not isinstance(result,dict): raise stripe_state.RetryableStripeEventError('Invalid provider response.')
        return result
    def snapshots():
        sub=get('/v1/subscriptions/'+quote(subscription_id,safe=''))
        if _id(sub.get('id')) != subscription_id or sub.get('status') != 'active':
            raise stripe_state.RetryableStripeEventError('Subscription requires lifecycle reconciliation.')
        invoice_id=_id(sub.get('latest_invoice'))
        if not invoice_id: raise stripe_state.RetryableStripeEventError('Invoice missing.')
        invoice=get('/v1/invoices/'+quote(invoice_id,safe=''))
        charge_id=_id(invoice.get('charge'))
        if not charge_id: raise stripe_state.RetryableStripeEventError('Charge missing.')
        charge=get('/v1/charges/'+quote(charge_id,safe=''))
        return sub,invoice,charge
    sub,invoice,charge=snapshots()
    ids=(_id(charge.get('id')),_id(invoice.get('payment_intent')),_id(invoice.get('id')))
    with transaction() as db:
        billing_status.lock_payment(db,billing_status.payment_keys(*ids))
        if not lock_customer_camera_capacity(db,customer_id): raise stripe_state.RetryableStripeEventError('Customer unavailable.')
        import time
        lease=db.execute('UPDATE cloud_gateway_refresh_leases SET token=token WHERE stripe_subscription_id=? AND customer_id=? AND token=? AND lease_until>=?',(subscription_id,customer_id,token,int(time.time()))).rowcount
        if lease != 1:
            raise stripe_state.RetryableStripeEventError('Refresh lease expired or was replaced.')
        record=db.execute('SELECT * FROM cloud_gateway_subscription_evidence WHERE customer_id=? AND stripe_subscription_id=?',(customer_id,subscription_id)).fetchone()
        sub,invoice,charge=snapshots()
        if not record or ids != (_id(charge.get('id')),_id(invoice.get('payment_intent')),_id(invoice.get('id'))):
            raise stripe_state.RetryableStripeEventError('Payment changed during refresh.')
        bound=db.execute('SELECT stripe_customer_id FROM stripe_customer_bindings WHERE customer_id=?',(customer_id,)).fetchone()
        if not bound or bound['stripe_customer_id'] != record['stripe_customer_id'] or stripe_state.bound_elsewhere(record['stripe_customer_id'],customer_id):
            raise stripe_state.RetryableStripeEventError('Customer binding changed.')
        snapshot=_snapshot(sub,invoice,charge,customer_id=customer_id,stripe_customer_id=record['stripe_customer_id'],subscription_id=subscription_id,price_id=record['stripe_price_id'])
        keys=('status','stripe_status','quantity','current_period_end','invoice_id','charge_id','payment_intent_id','payment_verified','reversal')
        if (not snapshot or billing_status.reversal_reason(*ids) or
                any(snapshot[k] != record[k] for k in keys) or snapshot['status'] != 'active'):
            raise stripe_state.RetryableStripeEventError('Subscription requires lifecycle reconciliation.')
        now=datetime.now(timezone.utc).isoformat()
        changed=db.execute('UPDATE cloud_gateway_subscription_evidence SET updated_at=? WHERE customer_id=? AND stripe_subscription_id=? AND event_id=? AND source_version=?',
                           (now,customer_id,subscription_id,record['event_id'],record['source_version'])).rowcount
        if changed != 1: raise stripe_state.RetryableStripeEventError('Evidence changed during refresh.')
        db.execute('DELETE FROM cloud_gateway_evidence_holds WHERE stripe_subscription_id=? AND customer_id=? AND event_id=?',(subscription_id,customer_id,hold))
        audit({'email':'stripe-webhook','role':'system'},'cloud_gateway.subscription_refreshed','cloud_gateway_subscription',subscription_id,
              {'customer_id':customer_id,'event_id':record['event_id'],'source_version':record['source_version']})
    return {'status':'refreshed'}


def refresh_batch(*, fetch=None, limit=50, after=''):
    """One bounded pass. Individual failures remain held for a later pass."""
    if not REFRESH_WORKER_READY: return {'status':'unavailable'}
    from partner_db import rows
    due=(datetime.now(timezone.utc)-timedelta(seconds=REFRESH_INTERVAL_SECONDS)).isoformat()
    selected=rows("SELECT customer_id,stripe_subscription_id FROM cloud_gateway_subscription_evidence WHERE status='active' AND updated_at<? AND stripe_subscription_id>? ORDER BY stripe_subscription_id LIMIT ?",(due,after,max(1,min(int(limit),50))))
    succeeded=failed=0
    for record in selected:
        try:
            result=refresh_one(record['customer_id'],record['stripe_subscription_id'],fetch=fetch)
            succeeded += result.get('status') == 'refreshed'
        except Exception:
            failed += 1
    return {'status':'complete','selected':len(selected),'refreshed':succeeded,'failed':failed,
            'next_cursor':selected[-1]['stripe_subscription_id'] if selected else ''}


async def refresh_worker():
    import asyncio
    cursor=''
    correlation_cursor=''
    while REFRESH_WORKER_READY:
        try:
            from cloud_gateway_subscription_evidence import retry_pending_correlations
            correlation=await asyncio.to_thread(retry_pending_correlations,after=correlation_cursor)
            correlation_cursor=correlation.get('next_cursor','')
            result=await asyncio.to_thread(refresh_batch,after=cursor)
            cursor=result.get('next_cursor','')
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        # Drain bounded pages before sleeping; failed rows retry on the next scan.
        await asyncio.sleep(0.1 if cursor or correlation_cursor else 60)
