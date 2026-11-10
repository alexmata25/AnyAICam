"""Prepared Cloud Gateway camera assignments; no paid producer or route is enabled.

The existing v2 camera plan is customer-wide and cannot verify one camera's
Cloud Gateway payment. Paid assignment remains hard-disabled until an
authoritative source is integrated; retirement never grants or changes a
camera or software license.
"""
import json
import os
import time
from datetime import datetime, timezone

END_REASONS = {'camera_removed','camera_replaced','appliance_reassigned','ownership_transferred','entitlement_revoked','subscription_cancelled','quantity_reduced'}


class BindingConflict(Exception):
    pass


CLOUD_GATEWAY_MONTHLY_CENTS = 999
CLOUD_GATEWAY_PRICE_ENV = 'ANYAICAM_STRIPE_PRICE_CLOUD_GATEWAY'
CLOUD_GATEWAY_BILLING_SOURCE_READY = False
# No durable, independently authenticated owner replacement intent is available.
CLOUD_GATEWAY_REPLACEMENT_INTENT_READY = False
# Evidence must be refreshed through the canonical provider path. This is a
# maximum verification age, not a billing grace period or a license change.
CLOUD_GATEWAY_EVIDENCE_MAX_AGE_SECONDS = 300


def cloud_price_classification(price_id):
    """Classify an exact configured candidate, never establish payment."""
    expected = os.environ.get(CLOUD_GATEWAY_PRICE_ENV, '').strip()
    other = {os.environ.get(name, '').strip() for name in (
        'ANYAICAM_STRIPE_PRICE_BASIC_LOCAL','ANYAICAM_STRIPE_PRICE_AI_LOCAL',
        'ANYAICAM_STRIPE_PRICE_HYBRID','ANYAICAM_STRIPE_PRICE_LOCAL_GATEWAY_REMOTE')}
    other.update(item.strip().partition(':')[0].strip() for item in
                 os.environ.get('ANYAICAM_STRIPE_PRICE_HYBRID_PREVIOUS','').split(','))
    if not isinstance(price_id,str) or not expected.startswith('price_') or price_id != expected or expected in other:
        return None
    return {'product':'cloud_gateway', 'price_id':expected, 'monthly_cents_per_camera':CLOUD_GATEWAY_MONTHLY_CENTS}


def _current_verified_cloud_subscription(db, customer_id, source_reference):
    """Read the prepared server evidence record; the feature remains disabled."""
    record=db.execute('SELECT * FROM cloud_gateway_subscription_evidence WHERE customer_id=? AND stripe_subscription_id=?',
                      (customer_id,source_reference)).fetchone()
    if not record: return None
    # Missing/unreadable hold schema must fail closed as well.
    try:
        if db.execute('SELECT 1 FROM cloud_gateway_evidence_holds WHERE stripe_subscription_id=? AND customer_id=? LIMIT 1',
                      (source_reference,customer_id)).fetchone():
            return None
        verified_at = datetime.fromisoformat(record['updated_at'])
        age = (datetime.now(timezone.utc) - verified_at).total_seconds()
        if verified_at.tzinfo is None or not 0 <= age <= CLOUD_GATEWAY_EVIDENCE_MAX_AGE_SECONDS:
            return None
    except Exception:
        return None
    logs=db.execute("SELECT actor_email,actor_role,details_json FROM audit_logs WHERE action=? AND entity_type=? AND entity_id=?",
                    ('cloud_gateway.subscription_evidence_updated','cloud_gateway_subscription',source_reference)).fetchall()
    expected={'customer_id':customer_id,'event_id':record['event_id'],'source_version':record['source_version'],'status':record['status'],
              'quantity':record['quantity'],'invoice_id':record['invoice_id']}
    try:
        matching=[entry for entry in logs if entry['actor_email']=='stripe-webhook' and entry['actor_role']=='system'
                  and json.loads(entry['details_json']) == expected]
    except (TypeError,ValueError):
        return None
    if len(matching) != 1: return None
    return {'customer_id':record['customer_id'],'subscription_id':record['stripe_subscription_id'],
            'version':record['source_version'],'product':record['product'],'status':record['status'],
            'stripe_status':record['stripe_status'],'verified_paid':record['payment_verified'] == 1,
            'payment_failed_at':None,'reversal':bool(record['reversal']),'quantity':record['quantity'],
            'current_period_end':record['current_period_end'],'price_id':record['stripe_price_id'],
            'amount_cents':record['amount_cents'],'currency':record['currency'],
            'interval':record['billing_interval'],'interval_count':record['interval_count']}


def _verified_subscription(db, customer_id, source_reference, source_version):
    record = _current_verified_cloud_subscription(db, customer_id, source_reference)
    if not isinstance(record,dict):
        return None
    quantity, end = record.get('quantity'), record.get('current_period_end')
    if (record.get('customer_id') != customer_id or record.get('subscription_id') != source_reference
            or record.get('version') != source_version or record.get('product') != 'cloud_gateway'
            or record.get('status') != 'active' or record.get('stripe_status') != 'active'
            or record.get('verified_paid') is not True or record.get('payment_failed_at')
            or record.get('reversal') is not False or type(quantity) is not int or quantity < 1
            or type(record.get('amount_cents')) is not int or record['amount_cents'] != CLOUD_GATEWAY_MONTHLY_CENTS
            or record.get('currency') != 'usd' or record.get('interval') != 'month'
            or type(record.get('interval_count')) is not int or record['interval_count'] != 1
            or type(end) is not int or end <= int(time.time())
            or not cloud_price_classification(record.get('price_id'))):
        return None
    return record


def _lock_customer(db, customer_id):
    from database_backend import lock_customer_camera_capacity
    if not lock_customer_camera_capacity(db,customer_id):
        raise BindingConflict('Customer not found.')


def _camera_mapping(db, customer_id, appliance_id, camera_id):
    camera = db.execute('SELECT c.id,c.customer_id,c.appliance_id,c.camera_number,c.device_key,c.status,a.gateway_edition '
                        'FROM cameras c JOIN appliances a ON a.id=c.appliance_id '
                        'WHERE c.id=? AND c.customer_id=? AND c.appliance_id=? AND a.customer_id=?',
                        (camera_id,customer_id,appliance_id,customer_id)).fetchone()
    if not camera or camera['gateway_edition'] != 'cloud_gateway' or camera['status'] not in {'configured','active'}:
        return None
    number,key = camera['camera_number'],camera['device_key']
    if type(number) is not int or not 1 <= number <= 10000 or not isinstance(key,str) or not key or key != key.strip():
        return None
    collisions = db.execute("SELECT id FROM cameras WHERE customer_id=? AND COALESCE(status,'')!='removed' AND ((appliance_id=? AND camera_number=?) OR device_key=?)", (customer_id,appliance_id,number,key)).fetchall()
    if len(collisions) != 1 or collisions[0]['id'] != camera_id:
        return None
    from gateway_internal_provisioning import edition_authorization_effective
    if not edition_authorization_effective(db,customer_id,appliance_id,'cloud_gateway'):
        return None
    return camera


def _assignment_audit_valid(db, record):
    try:
        logs = db.execute("SELECT details_json FROM audit_logs WHERE action=? AND entity_type=? AND entity_id=?",
                          ('cloud_gateway.camera_binding_assigned','cloud_gateway_camera_entitlement',record['id'])).fetchall()
        if len(logs)!=1 or type(record['version']) is not int or record['version'] < 1:
            return False
        initial=json.loads(logs[0]['details_json'])
        if not isinstance(initial,dict) or not isinstance(initial.get('billing_version'),str) or not initial['billing_version']:
            return False
        expected=_assignment_details(record['customer_id'],record['appliance_id'],record['camera_id'],
                                     record['camera_number'],record['device_key'],record['billing_reference'],initial['billing_version'])
        if initial != expected:
            return False
        logs=db.execute("SELECT details_json FROM audit_logs WHERE action=? AND entity_type=? AND entity_id=?",
                        ('cloud_gateway.camera_binding_reconciled','cloud_gateway_camera_entitlement',record['id'])).fetchall()
        steps=[json.loads(item['details_json']) for item in logs]
        if len(steps)!=record['version']-1 or any(not isinstance(item,dict) or type(item.get('version')) is not int for item in steps):
            return False
        current=initial['billing_version']
        for version,step in enumerate(sorted(steps,key=lambda item:item['version']),2):
            target=step.get('to_billing_version')
            if not isinstance(target,str) or not target or step!=_reconciliation_details(record,current,target,version):
                return False
            current=target
        return current == record['billing_version']
    except (KeyError,TypeError,ValueError):
        return False


def _assignment_details(customer_id, appliance_id, camera_id, camera_number, device_key, source_reference, source_version):
    return {'customer_id':customer_id, 'appliance_id':appliance_id, 'camera_id':camera_id,
            'camera_number':camera_number, 'device_key':device_key, 'edition':'cloud_gateway',
            'subscription_id':source_reference, 'billing_version':source_version}


def _reconciliation_details(record, old_version, new_version, version):
    return {'customer_id':record['customer_id'],'appliance_id':record['appliance_id'],
            'camera_id':record['camera_id'],'camera_number':record['camera_number'],'device_key':record['device_key'],
            'subscription_id':record['billing_reference'],'from_billing_version':old_version,
            'to_billing_version':new_version,'version':version}


def reconcile_paid_bindings(db, actor, *, customer_id, subscription_id, old_version, new_version, quantity):
    """Reconcile changed paid evidence under the caller's transaction and customer lock.

    Preserve active bindings first, then suspended, then pending; within each
    group preserve the earliest creation timestamp, then binding ID. Retired
    rows are never revived. Any ambiguous ownership, mapping or audit aborts.
    """
    if (type(quantity) is not int or quantity < 1 or any(not isinstance(v,str) or not v or v!=v.strip()
            for v in (customer_id,subscription_id,old_version,new_version)) or old_version==new_version):
        raise BindingConflict('Invalid reduction evidence.')
    _lock_customer(db,customer_id)
    records=db.execute("SELECT * FROM cloud_gateway_camera_entitlements WHERE billing_reference=? AND status IN ('pending','active','suspended')",(subscription_id,)).fetchall()
    ranked=[]; seen=set()
    for record in records:
        if (record['customer_id']!=customer_id or record['billing_source']!='verified_subscription'
                or record['edition']!='cloud_gateway' or record['billing_version']!=old_version
                or record['audit_reference']!=record['id'] or record['camera_id'] in seen
                or not _assignment_audit_valid(db,record)):
            raise BindingConflict('Ambiguous or stale subscription binding.')
        seen.add(record['camera_id'])
        same=db.execute("SELECT id FROM cloud_gateway_camera_entitlements WHERE camera_id=? AND status IN ('pending','active','suspended')",(record['camera_id'],)).fetchall()
        camera=_camera_mapping(db,customer_id,record['appliance_id'],record['camera_id'])
        if (len(same)!=1 or same[0]['id']!=record['id'] or not camera
                or (record['camera_number'],record['device_key']) != (camera['camera_number'],camera['device_key'])):
            raise BindingConflict('Camera ownership or mapping changed.')
        try:
            created=datetime.fromisoformat(record['created_at'])
            if created.tzinfo is None or created.utcoffset() is None: raise ValueError()
            created=created.astimezone(timezone.utc)
        except (TypeError,ValueError,OverflowError) as error:
            raise BindingConflict('Binding creation time is unavailable.') from error
        ranked.append(({'active':0,'suspended':1,'pending':2}[record['status']],created,record['id'],record))
    ranked.sort(key=lambda item:item[:3])
    from partner_db import audit
    for _,_,_,record in ranked[:quantity]:
        version=record['version']+1
        changed=db.execute("UPDATE cloud_gateway_camera_entitlements SET billing_version=?,version=?,updated_at=? WHERE id=? AND customer_id=? AND appliance_id=? AND camera_id=? AND billing_reference=? AND billing_version=? AND version=? AND status=?",
                           (new_version,version,datetime.now(timezone.utc).isoformat(),record['id'],customer_id,record['appliance_id'],record['camera_id'],subscription_id,old_version,record['version'],record['status'])).rowcount
        if changed!=1: raise BindingConflict('Binding changed during reconciliation.')
        audit(actor,'cloud_gateway.camera_binding_reconciled','cloud_gateway_camera_entitlement',record['id'],
              _reconciliation_details(record,old_version,new_version,version))
    for _,_,_,record in ranked[quantity:]:
        retire_binding(db,actor,binding_id=record['id'],customer_id=customer_id,appliance_id=record['appliance_id'],
                       camera_id=record['camera_id'],version=record['version'],reason='quantity_reduced')
    return {'retained':min(len(ranked),quantity),'retired':max(0,len(ranked)-quantity)}


def produce_paid_binding(db, *, source_reference, source_version, customer_id, appliance_id, camera_id, actor=None, request_id=None):
    """Assign one verified paid seat; caller must hold a transaction.

    The upstream verifier currently returns no evidence, so real calls fail
    closed. Tests may replace it with synthetic evidence; no route is mounted.
    """
    if any(not isinstance(v,str) or not v.strip() or v != v.strip() for v in (source_reference,source_version,customer_id,appliance_id,camera_id)):
        return {'status':'unavailable', 'reason':'Invalid subscription identity.'}
    if not CLOUD_GATEWAY_BILLING_SOURCE_READY:
        return {'status':'unavailable', 'reason':'Camera-specific Cloud Gateway billing evidence is not connected.'}
    _lock_customer(db,customer_id)
    subscription = _verified_subscription(db,customer_id,source_reference,source_version)
    if not subscription:
        return {'status':'unavailable', 'reason':'Camera-specific Cloud Gateway billing evidence is not connected or current.'}
    if (not isinstance(actor,dict) or not actor.get('email') or not actor.get('role')
            or not isinstance(request_id,str) or not request_id.strip() or request_id != request_id.strip()):
        raise BindingConflict('Authenticated actor and request ID required.')
    camera = _camera_mapping(db,customer_id,appliance_id,camera_id)
    if not camera:
        raise BindingConflict('Camera ownership, mapping or edition is not verified.')
    camera_bindings = db.execute("SELECT * FROM cloud_gateway_camera_entitlements WHERE camera_id=? AND status IN ('pending','active','suspended')", (camera_id,)).fetchall()
    if len(camera_bindings) > 1 or (camera_bindings and
            (camera_bindings[0]['customer_id'] != customer_id or camera_bindings[0]['billing_reference'] != source_reference)):
        raise BindingConflict('Camera already has a conflicting current assignment.')
    current = db.execute("SELECT * FROM cloud_gateway_camera_entitlements WHERE customer_id=? AND billing_reference=? AND status IN ('pending','active','suspended')", (customer_id,source_reference)).fetchall()
    if len(current) > subscription['quantity']:
        raise BindingConflict('Assignments exceed the current paid quantity; reconciliation required.')
    matching = [item for item in current if item['camera_id'] == camera_id]
    if matching:
        record = matching[0]
        if (len(matching) == 1 and record['id'] == request_id and record['appliance_id'] == appliance_id
                and record['camera_number'] == camera['camera_number'] and record['device_key'] == camera['device_key']
                and record['billing_version'] == source_version and record['status'] == 'active'
                and record['edition'] == 'cloud_gateway' and record['billing_source'] == 'verified_subscription'
                and record['audit_reference'] == request_id and _assignment_audit_valid(db,record)):
            return {'status':'already_assigned', 'binding_id':request_id}
        raise BindingConflict('Camera assignment conflicts or is stale.')
    if len(current) >= subscription['quantity']:
        raise BindingConflict('No verified paid camera seats remain.')
    now = datetime.now(timezone.utc).isoformat()
    db.execute('INSERT INTO cloud_gateway_camera_entitlements(id,customer_id,appliance_id,camera_id,camera_number,device_key,edition,billing_source,billing_reference,billing_version,status,version,audit_reference,created_at,updated_at,ended_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)',
               (request_id,customer_id,appliance_id,camera_id,camera['camera_number'],camera['device_key'],
                'cloud_gateway','verified_subscription',source_reference,source_version,'active',1,request_id,now,now))
    from partner_db import audit
    audit(actor,'cloud_gateway.camera_binding_assigned','cloud_gateway_camera_entitlement',request_id,
          _assignment_details(customer_id,appliance_id,camera_id,camera['camera_number'],camera['device_key'],source_reference,source_version))
    return {'status':'assigned','binding_id':request_id}


def move_paid_camera_binding(db, *, actor, customer_id, old_binding_id, old_version, source_reference,
                             source_version, target_appliance_id, request_id):
    """Unmounted same-owner camera move; caller must authenticate and own a transaction.

    A failed mapping or replacement must abort the entire enclosing transaction.
    This adapter does not authorize ownership transfers or provision cameras.
    """
    values=(customer_id,old_binding_id,source_reference,source_version,target_appliance_id,request_id)
    if (not CLOUD_GATEWAY_BILLING_SOURCE_READY or not CLOUD_GATEWAY_REPLACEMENT_INTENT_READY
            or any(not isinstance(v,str) or not v or v!=v.strip() for v in values)
            or type(old_version) is not int or old_version<1 or not isinstance(actor,dict)
            or actor.get('role')!='customer_owner' or actor.get('customer_id')!=customer_id
            or not isinstance(actor.get('email'),str) or not actor['email'].strip()):
        raise BindingConflict('Authenticated owner and current billing evidence required.')
    _lock_customer(db,customer_id)
    if not _verified_subscription(db,customer_id,source_reference,source_version):
        raise BindingConflict('Current paid subscription evidence required.')
    old=db.execute('SELECT * FROM cloud_gateway_camera_entitlements WHERE id=?',(old_binding_id,)).fetchone()
    if (not old or old['customer_id']!=customer_id or old['billing_reference']!=source_reference
            or old['billing_version']!=source_version or old['appliance_id']==target_appliance_id):
        raise BindingConflict('Original binding identity or evidence conflicts.')
    target=db.execute('SELECT site_id,gateway_edition FROM appliances WHERE id=? AND customer_id=?',
                      (target_appliance_id,customer_id)).fetchone()
    if not target or not target['site_id'] or target['gateway_edition']!='cloud_gateway':
        raise BindingConflict('Target appliance ownership or edition is unavailable.')
    kwargs=dict(actor=actor,customer_id=customer_id,old_binding_id=old_binding_id,old_version=old_version,
                source_reference=source_reference,source_version=source_version,new_appliance_id=target_appliance_id,
                new_camera_id=old['camera_id'],request_id=request_id)
    if old['status']!='active':
        mapped=db.execute('SELECT 1 FROM cameras WHERE id=? AND customer_id=? AND appliance_id=? AND site_id=?',
                          (old['camera_id'],customer_id,target_appliance_id,target['site_id'])).fetchone()
        if not mapped:
            raise BindingConflict('Replacement mapping changed.')
        return replace_paid_binding(db,**kwargs)
    if (old['version']!=old_version or verify_paid_binding(db,binding_id=old_binding_id,customer_id=customer_id,
            appliance_id=old['appliance_id'],camera_id=old['camera_id'])['status']!='verified'):
        raise BindingConflict('Original binding or camera mapping is not current.')
    changed=db.execute('UPDATE cameras SET appliance_id=?,site_id=? WHERE id=? AND customer_id=? AND appliance_id=? AND camera_number=? AND device_key=? AND status IN (?,?)',
                       (target_appliance_id,target['site_id'],old['camera_id'],customer_id,old['appliance_id'],
                        old['camera_number'],old['device_key'],'active','configured')).rowcount
    if changed!=1:
        raise BindingConflict('Camera mapping changed during move.')
    return replace_paid_binding(db,**kwargs)


def move_paid_camera_transactionally(**kwargs):
    """Unmounted entry point that rolls the mapping and binding back together."""
    if not CLOUD_GATEWAY_BILLING_SOURCE_READY or not CLOUD_GATEWAY_REPLACEMENT_INTENT_READY:
        raise BindingConflict('Camera-specific Cloud Gateway billing evidence is not connected.')
    from partner_db import transaction
    with transaction() as db:
        return move_paid_camera_binding(db,**kwargs)


def replace_paid_binding(db, *, actor, customer_id, old_binding_id, old_version, source_reference,
                         source_version, new_appliance_id, new_camera_id, request_id):
    """Unmounted same-owner replacement; caller must authenticate and own a transaction.

    The caller must roll back the entire transaction on any exception. This
    operation neither changes camera ownership nor authorizes a claim transfer.
    """
    values=(customer_id,old_binding_id,source_reference,source_version,new_appliance_id,new_camera_id,request_id)
    if (not CLOUD_GATEWAY_BILLING_SOURCE_READY or not CLOUD_GATEWAY_REPLACEMENT_INTENT_READY
            or any(not isinstance(v,str) or not v or v!=v.strip() for v in values)
            or old_binding_id==request_id or type(old_version) is not int or old_version<1
            or not isinstance(actor,dict) or actor.get('role')!='customer_owner'
            or actor.get('customer_id')!=customer_id or not isinstance(actor.get('email'),str) or not actor['email'].strip()):
        raise BindingConflict('Authenticated owner and current billing evidence required.')
    _lock_customer(db,customer_id)
    if not _verified_subscription(db,customer_id,source_reference,source_version):
        raise BindingConflict('Current paid subscription evidence required.')
    old=db.execute('SELECT * FROM cloud_gateway_camera_entitlements WHERE id=?',(old_binding_id,)).fetchone()
    if (not old or old['customer_id']!=customer_id or old['edition']!='cloud_gateway'
            or old['billing_source']!='verified_subscription' or old['billing_reference']!=source_reference
            or old['billing_version']!=source_version or old['audit_reference']!=old_binding_id):
        raise BindingConflict('Original binding identity or evidence conflicts.')
    owner=db.execute('SELECT id FROM appliances WHERE id=? AND customer_id=?',(old['appliance_id'],customer_id)).fetchone()
    old_camera=db.execute('SELECT customer_id,appliance_id FROM cameras WHERE id=?',(old['camera_id'],)).fetchone()
    expected_appliance=new_appliance_id if old['camera_id']==new_camera_id else old['appliance_id']
    from gateway_internal_provisioning import edition_authorization_effective
    if (not owner or not old_camera or (old_camera['customer_id'],old_camera['appliance_id'])!=(customer_id,expected_appliance)
            or not edition_authorization_effective(db,customer_id,old['appliance_id'],'cloud_gateway')):
        raise BindingConflict('Original camera ownership changed.')
    target=_camera_mapping(db,customer_id,new_appliance_id,new_camera_id)
    if not target:
        raise BindingConflict('Replacement camera ownership or identity is ambiguous.')
    details={'customer_id':customer_id,'old_binding_id':old_binding_id,'old_version':old_version,
             'old_appliance_id':old['appliance_id'],'old_camera_id':old['camera_id'],
             'new_binding_id':request_id,'new_appliance_id':new_appliance_id,'new_camera_id':new_camera_id,
             'subscription_id':source_reference,'billing_version':source_version}
    if old['status']=='revoked' and old['version']==old_version+1 and old['ended_reason']=='camera_replaced':
        logs=db.execute('SELECT details_json FROM audit_logs WHERE action=? AND entity_type=? AND entity_id=?',
                        ('cloud_gateway.camera_binding_replaced','cloud_gateway_camera_entitlement',request_id)).fetchall()
        try:
            valid=len(logs)==1 and json.loads(logs[0]['details_json'])==details
        except (TypeError,ValueError):
            valid=False
        if (valid and verify_paid_binding(db,binding_id=request_id,customer_id=customer_id,
                                         appliance_id=new_appliance_id,camera_id=new_camera_id)['status']=='verified'):
            retire_binding(db,actor,binding_id=old_binding_id,customer_id=customer_id,appliance_id=old['appliance_id'],
                           camera_id=old['camera_id'],version=old_version,reason='camera_replaced')
            return {'status':'already_replaced','binding_id':request_id}
        raise BindingConflict('Replacement retry is stale or conflicts.')
    if old['status']!='active' or old['version']!=old_version or not _assignment_audit_valid(db,old):
        raise BindingConflict('Original binding is not current.')
    current=db.execute("SELECT id FROM cloud_gateway_camera_entitlements WHERE camera_id=? AND status IN ('pending','active','suspended')",(old['camera_id'],)).fetchall()
    if len(current)!=1 or current[0]['id']!=old_binding_id:
        raise BindingConflict('Original camera binding is ambiguous.')
    if (old['camera_id']==new_camera_id and old['appliance_id']==new_appliance_id
            and (old['camera_number'],old['device_key'])==(target['camera_number'],target['device_key'])):
        raise BindingConflict('Replacement camera is unchanged.')
    retire_binding(db,actor,binding_id=old_binding_id,customer_id=customer_id,appliance_id=old['appliance_id'],
                   camera_id=old['camera_id'],version=old_version,reason='camera_replaced')
    assigned=produce_paid_binding(db,source_reference=source_reference,source_version=source_version,
        customer_id=customer_id,appliance_id=new_appliance_id,camera_id=new_camera_id,actor=actor,request_id=request_id)
    if assigned.get('status')!='assigned':
        raise BindingConflict('Replacement assignment could not be made.')
    from partner_db import audit
    audit(actor,'cloud_gateway.camera_binding_replaced','cloud_gateway_camera_entitlement',request_id,details)
    return {'status':'replaced','binding_id':request_id}


def verify_paid_binding(db, *, binding_id, customer_id, appliance_id, camera_id):
    """Revalidate exact binding against current upstream state and seat count."""
    if not CLOUD_GATEWAY_BILLING_SOURCE_READY:
        return {'status':'unavailable'}
    _lock_customer(db,customer_id)
    record = db.execute('SELECT * FROM cloud_gateway_camera_entitlements WHERE id=?', (binding_id,)).fetchone()
    if not record or (record['customer_id'],record['appliance_id'],record['camera_id'],record['edition'],record['status'],record['billing_source'],record['audit_reference']) != (customer_id,appliance_id,camera_id,'cloud_gateway','active','verified_subscription',binding_id):
        return {'status':'unavailable'}
    # Do not rely on the schema index alone: a missing index, legacy data or
    # conflicting customer mapping must not make an ambiguous camera usable.
    try:
        current_camera = db.execute("SELECT id FROM cloud_gateway_camera_entitlements WHERE camera_id=? AND status IN ('pending','active','suspended')", (camera_id,)).fetchall()
        if len(current_camera) != 1 or current_camera[0]['id'] != binding_id:
            return {'status':'unavailable'}
    except Exception:
        return {'status':'unavailable'}
    subscription = _verified_subscription(db,customer_id,record['billing_reference'],record['billing_version'])
    if not subscription or not _assignment_audit_valid(db,record):
        return {'status':'unavailable'}
    camera = _camera_mapping(db,customer_id,appliance_id,camera_id)
    if not camera or (record['camera_number'],record['device_key']) != (camera['camera_number'],camera['device_key']):
        return {'status':'unavailable'}
    count = db.execute("SELECT COUNT(*) AS n FROM cloud_gateway_camera_entitlements WHERE customer_id=? AND billing_reference=? AND status IN ('pending','active','suspended')", (customer_id,record['billing_reference'])).fetchone()['n']
    return {'status':'verified'} if count <= subscription['quantity'] else {'status':'unavailable'}


def assign_transactionally(**kwargs):
    """Entry point for a future authenticated caller; currently unavailable."""
    if not CLOUD_GATEWAY_BILLING_SOURCE_READY:
        return {'status':'unavailable', 'reason':'Camera-specific Cloud Gateway billing evidence is not connected.'}
    from partner_db import transaction
    with transaction() as db:
        return produce_paid_binding(db,**kwargs)


def replace_transactionally(**kwargs):
    """Unmounted entry point; propagate errors so the complete transaction rolls back."""
    if not CLOUD_GATEWAY_BILLING_SOURCE_READY or not CLOUD_GATEWAY_REPLACEMENT_INTENT_READY:
        raise BindingConflict('Camera-specific Cloud Gateway billing evidence is not connected.')
    from partner_db import transaction
    with transaction() as db:
        return replace_paid_binding(db,**kwargs)


def verify_transactionally(**kwargs):
    if not CLOUD_GATEWAY_BILLING_SOURCE_READY:
        return {'status':'unavailable'}
    from partner_db import transaction
    with transaction() as db:
        return verify_paid_binding(db,**kwargs)


def retire_binding(db, actor, *, binding_id, customer_id, appliance_id, camera_id, version, reason):
    """Retire one existing binding and audit it; caller must own transaction.

    The caller must independently authenticate the lifecycle event and tenant.
    This is deliberately unmounted; it cannot grant or transfer an entitlement.
    Recorded identity permits cleanup after a camera was removed or transferred.
    """
    from partner_db import audit
    if (not isinstance(actor,dict) or not actor.get('email') or not actor.get('role')
            or type(version) is not int or version < 1 or reason not in END_REASONS
            or any(not isinstance(v,str) or not v.strip() or v != v.strip()
                   for v in (binding_id,customer_id,appliance_id,camera_id))):
        raise BindingConflict('Invalid binding transition.')
    record = db.execute('SELECT * FROM cloud_gateway_camera_entitlements WHERE id=?', (binding_id,)).fetchone()
    if not record or tuple(record[k] for k in ('customer_id','appliance_id','camera_id','edition')) != (customer_id,appliance_id,camera_id,'cloud_gateway'):
        raise BindingConflict('Binding not found for the recorded identity.')
    target = 'cancelled' if reason == 'subscription_cancelled' else 'revoked'
    details = {'customer_id':customer_id,'appliance_id':appliance_id,'camera_id':camera_id,
               'reason':reason,'version':version+1,'status':target}
    if record['status'] == target and record['version'] == version+1 and record['ended_reason'] == reason:
        logs = db.execute("SELECT details_json FROM audit_logs WHERE action=? AND entity_type=? AND entity_id=?",
                          ('cloud_gateway.camera_binding_retired','cloud_gateway_camera_entitlement',binding_id)).fetchall()
        try:
            if sum(json.loads(entry['details_json']) == details for entry in logs) == 1:
                return {'status':'already_retired','version':version+1}
        except (TypeError,ValueError):
            pass
        raise BindingConflict('Retirement audit is missing or ambiguous.')
    updated = db.execute("UPDATE cloud_gateway_camera_entitlements SET status=?,version=?,updated_at=?,ended_reason=? "
                         "WHERE id=? AND customer_id=? AND appliance_id=? AND camera_id=? AND edition='cloud_gateway' "
                         "AND version=? AND status IN ('pending','active','suspended')",
                         (target,version+1,datetime.now(timezone.utc).isoformat(),reason,binding_id,customer_id,appliance_id,camera_id,version)).rowcount
    if updated != 1:
        raise BindingConflict('Binding changed or is already retired.')
    audit(actor,'cloud_gateway.camera_binding_retired','cloud_gateway_camera_entitlement',binding_id,details)
    return {'status':target,'version':version+1}


def retire_transactionally(actor, **kwargs):
    from partner_db import transaction
    with transaction() as db:
        return retire_binding(db,actor,**kwargs)


def retire_for_camera_transition(db, actor, *, customer_id, appliance_id, camera_id, reason):
    """Retire the current binding inside an authenticated camera transaction.

    Older databases have no binding table and cannot contain a binding. If it
    exists, malformed schema, conflicting ownership or audit failure aborts the
    whole camera removal rather than silently leaving a current binding.
    """
    from database_backend import backend
    kind = backend()
    if kind == 'sqlite':
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", ('cloud_gateway_camera_entitlements',)).fetchone()
    elif kind == 'postgresql':
        exists = db.execute('SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name=?', ('cloud_gateway_camera_entitlements',)).fetchone()
    else:
        raise BindingConflict('Unsupported database backend.')
    if not exists:
        return 0
    records = db.execute("SELECT id,customer_id,appliance_id,camera_id,version FROM cloud_gateway_camera_entitlements "
                         "WHERE camera_id=? AND status IN ('pending','active','suspended')", (camera_id,)).fetchall()
    if len(records) > 1:
        raise BindingConflict('Ambiguous current camera bindings.')
    if not records:
        return 0
    record = records[0]
    if (record['customer_id'],record['appliance_id'],record['camera_id']) != (customer_id,appliance_id,camera_id):
        raise BindingConflict('Camera binding belongs to another owner or appliance.')
    retire_binding(db,actor,binding_id=record['id'],customer_id=customer_id,appliance_id=appliance_id,
                   camera_id=camera_id,version=record['version'],reason=reason)
    return 1


def retire_for_camera_removal(db, actor, *, customer_id, appliance_id, camera_id):
    return retire_for_camera_transition(db,actor,customer_id=customer_id,appliance_id=appliance_id,
                                        camera_id=camera_id,reason='camera_removed')
