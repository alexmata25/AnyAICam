"""Read-only, unmounted Gateway purchase-to-installation reconciliation.

The caller must authenticate the actor and provide the application's transaction
connection. Generic order/license rows do not prove Gateway edition or a
purchase-to-appliance association. This service never grants, claims, writes,
issues credentials, assigns an edition or confirms payment with a provider.
"""

import hashlib
import hmac
import json
import re

EDITIONS = frozenset(('local_gateway', 'cloud_gateway'))
KINDS = frozenset(('software_only', 'preinstalled_appliance'))
_FULFILLMENT = frozenset(('paid','preparing','configuring','testing','ready_to_ship','shipped','delivered','cancelled'))

# Prototype contract only. No current trusted producer or transactional revision
# store exists. This must stay false until integration acceptance is complete.
PURCHASE_EVIDENCE_CONTRACT_READY = False
_EVIDENCE_FIELDS = frozenset(('schema_version','evidence_id','revision','customer_id','edition',
    'stripe_product_id','stripe_price_id','payment_reference','payment_status','reversal_state',
    'license_reference','license_customer_id','license_status','license_capacity',
    'fulfillment_kind','hardware_order_id','hardware_unit_id','fulfillment_status',
    'installation_id','cloud_subscription_id','cloud_subscription_status','cloud_quantity','issued_at','expires_at'))
_REFERENCE = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')


class HandoffDenied(Exception):
    pass


def validate_signed_candidate(document, signature, *, key, now, customer_id, edition,
                              installation_id, claim_verified, previous=None):
    """Validate a synthetic signed candidate; never authorize or mutate state.

    Customer, installation, claim status, previous revision and key must be
    obtained independently by a future authenticated caller, within its
    transaction. A signed document does not prove current claim ownership.
    A returned candidate is not a paid entitlement or edition confirmation.
    """
    if not PURCHASE_EVIDENCE_CONTRACT_READY:
        return {'status':'unavailable'}
    if (not isinstance(document,dict) or set(document)!=_EVIDENCE_FIELDS
            or not isinstance(signature,str) or not re.fullmatch(r'[0-9a-f]{64}',signature)
            or not isinstance(key,bytes) or len(key)<32 or type(now) is not int):
        raise HandoffDenied('Invalid evidence envelope.')
    references=('evidence_id','customer_id','stripe_product_id','stripe_price_id',
                'payment_reference','license_reference','license_customer_id')
    if any(not isinstance(document[k],str) or not _REFERENCE.fullmatch(document[k]) for k in references):
        raise HandoffDenied('Invalid evidence identity.')
    if not document['stripe_product_id'].startswith('prod_') or not document['stripe_price_id'].startswith('price_'):
        raise HandoffDenied('Invalid provider product or price identity.')
    for name in ('hardware_order_id','hardware_unit_id','installation_id','cloud_subscription_id'):
        value=document[name]
        if value is not None and (not isinstance(value,str) or not _REFERENCE.fullmatch(value)):
            raise HandoffDenied('Invalid evidence identity.')
    if (type(document['schema_version']) is not int or document['schema_version']!=1
            or type(document['revision']) is not int or document['revision']<1
            or type(document['license_capacity']) is not int or document['license_capacity']<1
            or type(document['issued_at']) is not int or type(document['expires_at']) is not int
            or not 0 <= document['issued_at'] <= now < document['expires_at']):
        raise HandoffDenied('Invalid or stale evidence.')
    if (not isinstance(document['edition'],str) or document['edition'] not in EDITIONS
            or not isinstance(document['fulfillment_kind'],str) or document['fulfillment_kind'] not in KINDS):
        raise HandoffDenied('Invalid edition or fulfillment kind.')
    for name,values in (('payment_status',('paid','pending','unpaid','canceled','refunded','revoked')),
                        ('reversal_state',('none','partial_refund','refunded','disputed','unknown')),
                        ('license_status',('active','pending','revoked','expired'))):
        if not isinstance(document[name],str) or document[name] not in values:
            raise HandoffDenied('Invalid evidence status.')
    if (document['customer_id']!=customer_id or document['license_customer_id']!=customer_id
            or document['edition']!=edition):
        raise HandoffDenied('Customer, licence or edition mismatch.')
    if document['edition']=='cloud_gateway':
        if (not document['cloud_subscription_id'] or document['cloud_subscription_status'] not in
                ('active','pending','past_due','canceled','unpaid','revoked') or type(document['cloud_quantity']) is not int
                or document['cloud_quantity']<1):
            raise HandoffDenied('Cloud subscription quantity is unavailable.')
    elif (document['cloud_subscription_id'] is not None or document['cloud_subscription_status'] is not None
          or document['cloud_quantity'] is not None):
        raise HandoffDenied('Unexpected Cloud subscription evidence.')
    if document['fulfillment_kind']=='software_only':
        if (document['hardware_order_id'] is not None or document['hardware_unit_id'] is not None
                or document['fulfillment_status']!='not_applicable'):
            raise HandoffDenied('Conflicting software-only fulfillment.')
    elif (not document['hardware_order_id'] or not document['hardware_unit_id']
          or not isinstance(document['fulfillment_status'],str) or document['fulfillment_status'] not in _FULFILLMENT):
        raise HandoffDenied('Hardware unit or fulfillment is unavailable.')
    payload=json.dumps(document,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')
    digest=hashlib.sha256(payload).hexdigest()
    # A newer revision may update status and quantity, but it must not silently
    # replace the product, price or subscription whose evidence was reviewed.
    binding={name:document[name] for name in ('customer_id','edition','stripe_product_id','stripe_price_id',
             'cloud_subscription_id','license_reference','fulfillment_kind','hardware_order_id','hardware_unit_id')}
    binding_digest=hashlib.sha256(json.dumps(binding,sort_keys=True,separators=(',',':'),
                                           ensure_ascii=True).encode('ascii')).hexdigest()
    if not hmac.compare_digest(hmac.new(key,payload,hashlib.sha256).hexdigest(),signature):
        raise HandoffDenied('Evidence authentication failed.')
    if previous is not None:
        if (not isinstance(previous,dict) or set(previous)!={'evidence_id','revision','digest','binding_digest','installation_id'}
                or previous['evidence_id']!=document['evidence_id'] or type(previous['revision']) is not int
                or previous['revision']<1 or not isinstance(previous['digest'],str)
                or not re.fullmatch(r'[0-9a-f]{64}',previous['digest'])
                or not isinstance(previous['binding_digest'],str)
                or not re.fullmatch(r'[0-9a-f]{64}',previous['binding_digest'])
                or (previous['installation_id'] is not None and
                    (not isinstance(previous['installation_id'],str) or not _REFERENCE.fullmatch(previous['installation_id'])))):
            raise HandoffDenied('Invalid stored revision.')
        if (not hmac.compare_digest(binding_digest,previous['binding_digest']) or
                (previous['installation_id'] is not None and previous['installation_id']!=document['installation_id'])):
            raise HandoffDenied('Evidence binding changed; a separately authorized handoff is required.')
        if document['revision']<previous['revision']:
            raise HandoffDenied('Stale evidence revision.')
        if document['revision']==previous['revision'] and not hmac.compare_digest(digest,previous['digest']):
            raise HandoffDenied('Conflicting evidence revision.')
    if (document['payment_status']!='paid' or document['reversal_state']!='none'
            or document['license_status']!='active' or document['fulfillment_status']=='cancelled'
            or (document['edition']=='cloud_gateway' and document['cloud_subscription_status']!='active')):
        return {'status':'ineligible','revision':document['revision'],'digest':digest,'binding_digest':binding_digest}
    if document['fulfillment_kind']=='preinstalled_appliance' and document['fulfillment_status'] not in ('shipped','delivered'):
        return {'status':'awaiting_fulfillment','revision':document['revision'],'digest':digest,'binding_digest':binding_digest}
    if document['installation_id'] is None:
        return {'status':'awaiting_claim','revision':document['revision'],'digest':digest,'binding_digest':binding_digest}
    if (not isinstance(installation_id,str) or document['installation_id']!=installation_id
            or claim_verified is not True):
        raise HandoffDenied('Current installation ownership and possession are required.')
    return {'status':'candidate_only','revision':document['revision'],'digest':digest,'binding_digest':binding_digest,
            'duplicate':bool(previous and previous['revision']==document['revision'])}


def reconcile(db, actor, *, kind, purchase_reference, requested_edition, appliance_id=None):
    """Return a repeatable pending state scoped to the authenticated owner.

    Software reference is the saved Stripe Checkout session ID on the existing
    VMS licence; hardware reference is the existing hardware order ID. These
    references select records, never prove edition, payment or possession.
    """
    if (not isinstance(actor,dict) or actor.get('role') != 'customer_owner'
            or not isinstance(actor.get('customer_id'),str) or not actor['customer_id'].strip()):
        raise HandoffDenied('Customer owner required.')
    if (not isinstance(kind,str) or kind not in KINDS
            or not isinstance(requested_edition,str) or requested_edition not in EDITIONS
            or not isinstance(purchase_reference,str) or not purchase_reference
            or purchase_reference != purchase_reference.strip() or len(purchase_reference)>128
            or (appliance_id is not None and (not isinstance(appliance_id,str) or not appliance_id
                or appliance_id != appliance_id.strip() or len(appliance_id)>128))):
        raise HandoffDenied('Invalid handoff request.')
    customer=actor['customer_id']
    try:
        if kind == 'software_only':
            rows=db.execute("SELECT status,expires_at FROM customer_entitlements WHERE customer_id=? AND product='vms_license' AND stripe_checkout_session_id=?",
                            (customer,purchase_reference)).fetchall()
        else:
            rows=db.execute("SELECT status,fulfillment_status,sku,quantity FROM hardware_orders WHERE customer_id=? AND id=?",
                            (customer,purchase_reference)).fetchall()
        if len(rows)!=1:
            raise HandoffDenied('Purchase unavailable.')
        record=rows[0]
        if kind == 'software_only':
            # An active row is shown as observed only, not asserted valid: its
            # expiry, payment reversal and independent licence require review.
            if record['status'] != 'active':
                return {'status':'payment_or_license_ineligible','edition_status':'pending_verification'}
            fulfillment='not_applicable'
        else:
            if (not isinstance(record['sku'],str) or not record['sku'].startswith('AIC-APPLIANCE-')
                    or type(record['quantity']) is not int or record['quantity'] != 1):
                raise HandoffDenied('Purchase unavailable.')
            if record['status'] != 'paid' or record['fulfillment_status']=='cancelled':
                return {'status':'payment_or_license_ineligible','edition_status':'pending_verification'}
            fulfillment=record['fulfillment_status']
            if fulfillment not in _FULFILLMENT:
                return {'status':'pending_verification','edition_status':'pending_verification'}
        if appliance_id is not None:
            appliances=db.execute('SELECT customer_id,gateway_edition FROM appliances WHERE id=? AND customer_id=?',
                                  (appliance_id,customer)).fetchall()
            if len(appliances)!=1:
                raise HandoffDenied('Installation unavailable.')
            edition=appliances[0]['gateway_edition']
            if edition not in (None,requested_edition):
                return {'status':'edition_conflict','edition_status':'pending_verification'}
        # Even a completed claim or shipped order is not proof of the mapping.
        if kind=='preinstalled_appliance' and fulfillment not in ('shipped','delivered'):
            state='awaiting_fulfillment'
        elif appliance_id is None:
            state='awaiting_installation_claim'
        else:
            state='pending_verification'
        return {'status':state, 'edition_status':'pending_verification',
                'fulfillment_status':fulfillment, 'purchase_binding_verified':False,
                'activation_authorized':False}
    except HandoffDenied:
        raise
    except Exception as error:
        raise HandoffDenied('Handoff unavailable.') from error
