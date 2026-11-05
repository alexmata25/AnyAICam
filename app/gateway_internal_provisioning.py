"""Unrouted internal-test provisioning services; not purchase entitlement.

Callers must use partner_db.transaction() and must not expose these services
until deployment isolation, schema and authorization controls are verified.
"""
import json
import os
from datetime import datetime, timezone

BASIS = 'internal_test_claimed_appliance'
SOURCE = 'authorized_internal_provisioning'
EDITIONS = frozenset(('local_gateway', 'cloud_gateway'))


class ProvisioningError(Exception):
    pass


class AuthorizationDenied(ProvisioningError):
    pass


def internal_test_enabled():
    # Require explicit opt-in and an explicit non-production environment;
    # cloud_config's cached environment must agree with the current setting.
    from cloud_config import settings
    configured = os.environ.get('ANYAICAM_ENV', '').strip().lower()
    return (configured in {'development', 'test'} and settings.environment == configured
            and os.environ.get('ANYAICAM_INTERNAL_GATEWAY_PROVISIONING_ENABLED', '').strip().lower() == 'true')


def _admin(db, actor):
    if not internal_test_enabled():
        raise AuthorizationDenied('Internal test provisioning is disabled.')
    from appliance_identity import has_global_administrator_grant
    if (not isinstance(actor, dict) or actor.get('role') != 'administrator'
            or not isinstance(actor.get('email'), str) or not actor['email'].strip()
            or not has_global_administrator_grant(db, email=actor['email'])):
        raise AuthorizationDenied('Global administrator required.')


def _bound_claim(db, customer_id, appliance_id, claim_id):
    # A conditional write locks the appliance until transaction completion,
    # serializing competing edition confirmations and ownership changes.
    changed = db.execute("UPDATE appliances SET gateway_edition=gateway_edition WHERE id=? AND customer_id=? AND activated_via='claim'",
                         (appliance_id, customer_id)).rowcount
    if changed != 1:
        raise ProvisioningError('Claimed appliance or owner changed.')
    appliance = db.execute('SELECT customer_id,gateway_edition,state,activation_status FROM appliances WHERE id=?', (appliance_id,)).fetchone()
    if not appliance or appliance['state'] in ('revoked', 'retired') or appliance['activation_status'] == 'released':
        raise ProvisioningError('Appliance is not eligible.')
    db.execute("UPDATE appliance_claims SET status=status WHERE appliance_id=? AND status='completed' AND revoked_at IS NULL", (appliance_id,))
    claims = db.execute("SELECT id,customer_id FROM appliance_claims WHERE appliance_id=? AND status='completed' AND revoked_at IS NULL",
                        (appliance_id,)).fetchall()
    if len(claims) != 1 or claims[0]['id'] != claim_id or claims[0]['customer_id'] != customer_id:
        raise ProvisioningError('Claim missing, stale or ambiguous.')
    return appliance


def _details(customer_id, appliance_id, claim_id, edition, version):
    return {'customer_id':customer_id, 'appliance_id':appliance_id, 'claim_id':claim_id,
            'edition':edition, 'basis':BASIS, 'version':version}


def authorize_internal_test(db, actor, *, request_id, customer_id, appliance_id, claim_id, edition):
    """Create an audited internal test authorization; transaction required."""
    from partner_db import audit
    _admin(db, actor)
    if edition not in EDITIONS or any(not isinstance(v, str) or not v.strip() or v != v.strip()
                                     for v in (request_id, customer_id, appliance_id, claim_id)):
        raise ProvisioningError('Invalid authorization parameters.')
    appliance = _bound_claim(db, customer_id, appliance_id, claim_id)
    if appliance['gateway_edition'] not in (None, edition):
        raise ProvisioningError('Conflicting edition.')
    existing = db.execute("SELECT * FROM internal_gateway_provisioning_authorizations WHERE id=?", (request_id,)).fetchone()
    if existing:
        expected = (customer_id, appliance_id, claim_id, edition, actor['email'], BASIS, 'active', 1)
        actual = tuple(existing[k] for k in ('customer_id','appliance_id','claim_id','edition','authorized_by','basis','status','version'))
        if actual == expected and verify_internal_test(db, request_id, customer_id, appliance_id, claim_id, edition, 1):
            return {'status':'already_authorized', 'authorization_id':request_id, 'version':1}
        raise ProvisioningError('Authorization request conflicts or is stale.')
    if db.execute("SELECT id FROM internal_gateway_provisioning_authorizations WHERE appliance_id=? AND status='active'", (appliance_id,)).fetchone():
        raise ProvisioningError('Appliance already has an active authorization.')
    now = datetime.now(timezone.utc).isoformat()
    db.execute('INSERT INTO internal_gateway_provisioning_authorizations(id,customer_id,appliance_id,claim_id,edition,basis,authorized_by,created_at,status,version,audit_reference) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (request_id, customer_id, appliance_id, claim_id, edition, BASIS, actor['email'], now, 'active', 1, request_id))
    audit(actor, 'gateway.internal_test_authorized', 'internal_gateway_authorization', request_id,
          _details(customer_id, appliance_id, claim_id, edition, 1))
    return {'status':'authorized', 'authorization_id':request_id, 'version':1}


def verify_internal_test(db, authorization_id, customer_id, appliance_id, claim_id, edition, version):
    """Check current source record, claim and matching transactional audit."""
    if not internal_test_enabled() or type(version) is not int or not 1 <= version <= 1000 or not isinstance(edition, str) or edition not in EDITIONS:
        return False
    try:
        appliance = _bound_claim(db, customer_id, appliance_id, claim_id)
    except ProvisioningError:
        return False
    if appliance['gateway_edition'] not in (None, edition):
        return False
    if db.execute("UPDATE internal_gateway_provisioning_authorizations SET version=version WHERE id=? AND status='active' AND version=?", (authorization_id, version)).rowcount != 1:
        return False
    records = db.execute("SELECT * FROM internal_gateway_provisioning_authorizations WHERE appliance_id=? AND status='active'", (appliance_id,)).fetchall()
    if len(records) != 1:
        return False
    record = records[0]
    expected = (authorization_id, customer_id, appliance_id, claim_id, edition, BASIS, 'active', version, authorization_id)
    if tuple(record[k] for k in ('id','customer_id','appliance_id','claim_id','edition','basis','status','version','audit_reference')) != expected:
        return False
    return _authorization_audit_valid(db, record, customer_id, appliance_id, claim_id, edition, version)


def _authorization_audit_valid(db, record, customer_id, appliance_id, claim_id, edition, version):
    authorization_id = record['id']
    logs = db.execute("SELECT actor_email,actor_role,details_json FROM audit_logs WHERE action=? AND entity_type=? AND entity_id=?",
                      ('gateway.internal_test_authorized','internal_gateway_authorization',authorization_id)).fetchall()
    if len(logs) != 1 or logs[0]['actor_email'] != record['authorized_by'] or logs[0]['actor_role'] != 'administrator':
        return False
    try:
        if json.loads(logs[0]['details_json']) != _details(customer_id, appliance_id, claim_id, edition, 1):
            return False
        history = db.execute("SELECT actor_role,details_json FROM audit_logs WHERE action=? AND entity_type=? AND entity_id=?",
                             ('gateway.internal_test_version_updated','internal_gateway_authorization',authorization_id)).fetchall()
        if len(history) != version - 1:
            return False
        versions = set()
        for entry in history:
            data = json.loads(entry['details_json'])
            if entry['actor_role'] != 'administrator' or type(data.get('version')) is not int:
                return False
            v = data['version']
            if data != _details(customer_id, appliance_id, claim_id, edition, v) or v in versions:
                return False
            versions.add(v)
        return versions == set(range(2, version + 1))
    except (TypeError, ValueError):
        return False


def edition_authorization_effective(db, customer_id, appliance_id, edition):
    """An appliance known to be a Gateway must have current edition evidence."""
    history = db.execute('SELECT * FROM appliance_edition_provisioning_decisions WHERE appliance_id=?', (appliance_id,)).fetchall()
    if not history:
        return False
    decisions = [item for item in history if item['revoked_at'] is None]
    if len(decisions) != 1:
        return False
    decision = decisions[0]
    if decision['customer_id'] != customer_id or decision['edition'] != edition or decision['evidence_source'] != SOURCE:
        return False
    try:
        version = int(decision['evidence_version'])
    except (ValueError, TypeError):
        return False
    return verify_internal_test(db, decision['evidence_reference'], customer_id, appliance_id, decision['claim_id'], edition, version)


def confirm_internal_test(db, actor, *, authorization_id, customer_id, appliance_id, claim_id, edition, version):
    """Prepare an atomic edition decision; caller owns the transaction."""
    from partner_db import audit
    _admin(db, actor)
    if not verify_internal_test(db, authorization_id, customer_id, appliance_id, claim_id, edition, version):
        raise ProvisioningError('Pending verification: authorization invalid or stale.')
    appliance = db.execute('SELECT gateway_edition FROM appliances WHERE id=? AND customer_id=?', (appliance_id, customer_id)).fetchone()
    decision = db.execute('SELECT * FROM appliance_edition_provisioning_decisions WHERE appliance_id=? AND revoked_at IS NULL', (appliance_id,)).fetchone()
    if appliance['gateway_edition'] == edition and decision:
        if (decision['customer_id'], decision['claim_id'], decision['edition'], decision['evidence_source'], decision['evidence_reference'], decision['evidence_version']) == (customer_id, claim_id, edition, SOURCE, authorization_id, str(version)):
            logs = db.execute("SELECT details_json FROM audit_logs WHERE action=? AND entity_type=? AND entity_id=?",
                              ('gateway.internal_test_edition_confirmed','appliance',appliance_id)).fetchall()
            expected = {'customer_id':customer_id, 'claim_id':claim_id, 'edition':edition, 'authorization_id':authorization_id, 'version':version}
            try:
                if sum(json.loads(item['details_json']) == expected for item in logs) == 1:
                    return {'status':'already_confirmed'}
            except (TypeError, ValueError):
                pass
        if decision['evidence_source'] != SOURCE or edition_authorization_effective(db, decision['customer_id'], appliance_id, edition):
            raise ProvisioningError('Conflicting edition decision.')
        # Preserve the prior decision and its audit as history, then record a
        # fresh explicit confirmation against the new valid authorization.
        db.execute('UPDATE appliance_edition_provisioning_decisions SET revoked_at=? WHERE id=? AND revoked_at IS NULL',
                   (datetime.now(timezone.utc).isoformat(), decision['id']))
        decision = None
    if appliance['gateway_edition'] not in (None, edition) or decision:
        raise ProvisioningError('Conflicting edition decision.')
    now = datetime.now(timezone.utc).isoformat()
    db.execute('INSERT INTO appliance_edition_provisioning_decisions(id,customer_id,appliance_id,claim_id,edition,evidence_source,evidence_reference,evidence_version,verified_by,verified_at,revoked_at) VALUES(?,?,?,?,?,?,?,?,?,?,NULL)',
               (authorization_id,customer_id,appliance_id,claim_id,edition,SOURCE,authorization_id,str(version),actor['email'],now))
    changed = db.execute('UPDATE appliances SET gateway_edition=? WHERE id=? AND customer_id=? AND (gateway_edition IS NULL OR gateway_edition=?)', (edition, appliance_id, customer_id, edition)).rowcount
    if changed != 1:
        raise ProvisioningError('Appliance changed during confirmation.')
    audit(actor, 'gateway.internal_test_edition_confirmed', 'appliance', appliance_id,
          {'customer_id':customer_id, 'claim_id':claim_id, 'edition':edition, 'authorization_id':authorization_id, 'version':version})
    return {'status':'confirmed'}


def change_authorization(db, actor, *, authorization_id, customer_id, appliance_id, claim_id, edition, version, action):
    """Revoke or advance a live authorization with a versioned audit entry."""
    from partner_db import audit
    _admin(db, actor)
    if action not in ('revoke', 'advance'):
        raise ProvisioningError('Invalid authorization action.')
    if type(version) is not int or not 1 <= version <= 1000 or (action == 'advance' and version == 1000):
        raise ProvisioningError('Invalid authorization version.')
    if action == 'revoke':
        # Revoke by the recorded identity even when current ownership changed.
        if db.execute('UPDATE appliances SET gateway_edition=gateway_edition WHERE id=?', (appliance_id,)).rowcount != 1:
            raise ProvisioningError('Appliance missing.')
        db.execute('UPDATE appliance_claims SET status=status WHERE appliance_id=?', (appliance_id,))
        if db.execute("UPDATE internal_gateway_provisioning_authorizations SET version=version WHERE id=? AND status='active' AND version=?", (authorization_id,version)).rowcount != 1:
            raise ProvisioningError('Authorization invalid or stale.')
        record = db.execute('SELECT * FROM internal_gateway_provisioning_authorizations WHERE id=?', (authorization_id,)).fetchone()
        expected = (authorization_id,customer_id,appliance_id,claim_id,edition,BASIS,'active',version,authorization_id)
        if (not record or tuple(record[k] for k in ('id','customer_id','appliance_id','claim_id','edition','basis','status','version','audit_reference')) != expected
                or not _authorization_audit_valid(db,record,customer_id,appliance_id,claim_id,edition,version)):
            raise ProvisioningError('Authorization identity or audit invalid.')
    elif not verify_internal_test(db, authorization_id, customer_id, appliance_id, claim_id, edition, version):
        raise ProvisioningError('Pending verification: authorization invalid or stale.')
    new_version = version + 1
    status = 'revoked' if action == 'revoke' else 'active'
    changed = db.execute("UPDATE internal_gateway_provisioning_authorizations SET status=?,version=? WHERE id=? AND status='active' AND version=?",
                         (status, new_version, authorization_id, version)).rowcount
    if changed != 1:
        raise ProvisioningError('Authorization changed; refresh and retry.')
    event = 'gateway.internal_test_revoked' if action == 'revoke' else 'gateway.internal_test_version_updated'
    audit(actor, event, 'internal_gateway_authorization', authorization_id,
          _details(customer_id, appliance_id, claim_id, edition, new_version))
    return {'status':status, 'version':new_version}


def authorize_transactionally(actor, **kwargs):
    from partner_db import transaction
    with transaction() as db:
        return authorize_internal_test(db, actor, **kwargs)


def confirm_transactionally(actor, **kwargs):
    from partner_db import transaction
    with transaction() as db:
        return confirm_internal_test(db, actor, **kwargs)


def change_transactionally(actor, **kwargs):
    from partner_db import transaction
    with transaction() as db:
        return change_authorization(db, actor, **kwargs)
