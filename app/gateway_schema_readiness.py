"""Read-only Gateway schema readiness checks; never applies migrations."""
import re

SESSION_INDEX = 'idx_claim_one_live_session_per_device'
INDEXES = {
    SESSION_INDEX: ('appliance_claims', 'lower(device_id)', ('pending','claimed','revoked_at')),
    'idx_appliance_claim_one_current_completed': ('appliance_claims', 'appliance_id', ('completed','revoked_at')),
    'idx_appliance_edition_current_decision': ('appliance_edition_provisioning_decisions', 'appliance_id', ('revoked_at',)),
    'idx_internal_gateway_active_authorization': ('internal_gateway_provisioning_authorizations', 'appliance_id', ('active','status')),
    'idx_cloud_gateway_camera_current': ('cloud_gateway_camera_entitlements', 'camera_id', ('pending','active','suspended','status')),
    'idx_gateway_checkout_one_unresolved': ('gateway_checkout_attempts', 'appliance_id', ('reserved','creating','open','review','paid_review','status')),
    'idx_gateway_claim_one_pending_recovery': ('gateway_claim_recovery_requests','appliance_id',('pending_manual_verification','status')),
}
COLUMNS = {
    'appliances': {'id','customer_id','gateway_edition','activated_via','state','activation_status'},
    'appliance_claims': {'id','device_id','appliance_id','customer_id','status','revoked_at','expires_at','proof_expires_at','claim_proof_encrypted','claim_proof_plaintext','claim_code_hash','claim_session_id','device_secret_hash','label_verifier_hash'},
    'appliance_edition_provisioning_decisions': {'id','customer_id','appliance_id','claim_id','edition','evidence_source','evidence_reference','evidence_version','verified_by','verified_at','revoked_at'},
    'internal_gateway_provisioning_authorizations': {'id','customer_id','appliance_id','claim_id','edition','basis','authorized_by','created_at','status','version','audit_reference'},
    'audit_logs': {'actor_email','actor_role','action','entity_type','entity_id','details_json','created_at'},
    'cloud_gateway_camera_entitlements': {'id','customer_id','appliance_id','camera_id','camera_number','device_key','edition','billing_source','billing_reference','billing_version','status','version','audit_reference','created_at','updated_at','ended_reason'},
    'cloud_gateway_subscription_evidence': {'stripe_subscription_id','customer_id','stripe_customer_id','stripe_price_id','checkout_session_id','product','quantity','amount_cents','currency','billing_interval','interval_count','status','stripe_status','current_period_end','invoice_id','charge_id','payment_intent_id','payment_verified','reversal','source_version','event_created','event_id','updated_at'},
    'cloud_gateway_evidence_holds': {'stripe_subscription_id','customer_id','event_id','event_created','created_at'},
    'cloud_gateway_webhook_correlation': {'event_id','event_type','object_id','stripe_customer_id','event_created','status','subscription_id','created_at'},
    'cloud_gateway_refresh_leases': {'stripe_subscription_id','customer_id','token','lease_until'},
    'gateway_checkout_attempts': {'id','customer_id','appliance_id','claim_id','edition','decision_id','authorization_reference','authorization_version','stripe_customer_id','stripe_price_id','quantity','camera_ids_json','status','authorization_invalidated','session_id','stripe_subscription_id','stripe_invoice_id','payment_event_id','reversal_state','refund_charge_id','refund_event_id','created_at','updated_at'},
    'gateway_checkout_review_actions': {'request_id','attempt_id','actor_email','action','created_at'},
    'gateway_stripe_payment_links': {'kind','provider_id','customer_id','stripe_customer_id','stripe_subscription_id','created_at'},
    'gateway_stable_installation_claim_ids': {'claim_id','appliance_id','original_claim_id','created_at'},
    'gateway_claim_recovery_requests': {'request_id','appliance_id','customer_id','requested_by','status','created_at'},
}
VERSIONS = ('20260910_appliance_claims','20261103_appliance_gateway_edition',
            '20261104_appliance_edition_provisioning_decisions',
            '20261105_internal_gateway_provisioning_authorizations',
            '20261106_one_current_completed_claim_per_appliance',
            '20261107_one_current_claim_session_per_device', '20261108_cloud_gateway_camera_entitlements',
            '20261109_cloud_gateway_subscription_evidence','20261110_gateway_checkout_attempts','20261111_gateway_stable_installation_claim_ids')
DEPENDENCIES = ((VERSIONS[0],VERSIONS[2]),(VERSIONS[1],VERSIONS[2]),
                (VERSIONS[2],VERSIONS[3]),(VERSIONS[0],VERSIONS[4]),(VERSIONS[0],VERSIONS[5]),
                (VERSIONS[2],VERSIONS[6]),(VERSIONS[6],VERSIONS[7]),(VERSIONS[7],VERSIONS[8]),(VERSIONS[8],VERSIONS[9]))


def _compact(value):
    return re.sub(r'[\s()\'";]+|::text', '', str(value or '').lower())


def _index(db, backend, name):
    if backend == 'sqlite':
        row = db.execute("SELECT tbl_name AS table_name,sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
        return dict(row) if row else None
    if backend == 'postgresql':
        row = db.execute("SELECT t.relname AS table_name,pg_get_indexdef(i.indexrelid) AS sql,i.indisvalid AS valid "
                         "FROM pg_index i JOIN pg_class x ON x.oid=i.indexrelid JOIN pg_class t ON t.oid=i.indrelid "
                         "JOIN pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname=current_schema() AND x.relname=?", (name,)).fetchone()
        return dict(row) if row else None
    return None


def _index_compatible(record, name):
    if not record or record.get('valid', True) is not True or record.get('table_name') != INDEXES[name][0]:
        return False
    sql = _compact(record.get('sql'))
    # Reject extra OR predicates, which could make the partial index weaker.
    if 'createuniqueindex' not in sql or 'where' not in sql or 'or' in sql.split('where',1)[1]:
        return False
    body, predicate = sql.split('where',1)
    marker = 'usingbtree' if 'usingbtree' in body else 'on' + _compact(INDEXES[name][0])
    if marker not in body or body.split(marker,1)[1] != _compact(INDEXES[name][1]):
        return False
    if name == SESSION_INDEX:
        return predicate in ('statusinpending,claimedandrevoked_atisnull',
                             'status=anyarray[pending,claimed]andrevoked_atisnull')
    if name == 'idx_appliance_claim_one_current_completed':
        return predicate in ('appliance_idisnotnullandstatus=completedandrevoked_atisnull',
                             'appliance_idisnotnullandstatus=completedandrevoked_atisnull')
    if name == 'idx_appliance_edition_current_decision':
        return predicate == 'revoked_atisnull'
    if name == 'idx_cloud_gateway_camera_current':
        return predicate in ('statusinpending,active,suspended', 'status=anyarray[pending,active,suspended]')
    if name == 'idx_gateway_checkout_one_unresolved':
        return predicate in ('statusinreserved,creating,open,review,paid_review', 'status=anyarray[reserved,creating,open,review,paid_review]')
    if name == 'idx_gateway_claim_one_pending_recovery':
        return predicate == 'status=pending_manual_verification'
    return predicate == 'status=active'


def claim_session_index_ready(db):
    try:
        from database_backend import backend
        return _index_compatible(_index(db,backend(),SESSION_INDEX),SESSION_INDEX)
    except Exception:
        return False


def inspect_gateway_schema(db, backend):
    """Return actionable read-only diagnostics for a supplied connection."""
    issues = []
    if backend not in {'sqlite','postgresql'}:
        return {'ready':False,'issues':['Unsupported database backend.']}
    try:
        for table, expected in COLUMNS.items():
            if backend == 'sqlite':
                metadata = db.execute('PRAGMA table_info("'+table+'")').fetchall()
                types = {item['name']:str(item['type'] or '').lower() for item in metadata}
            else:
                metadata = db.execute('SELECT column_name,data_type FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?',(table,)).fetchall()
                types = {item['column_name']:str(item['data_type'] or '').lower() for item in metadata}
            cols = set(types)
            if not expected.issubset(cols):
                issues.append('Missing or incompatible columns in '+table+': '+', '.join(sorted(expected-cols)))
            for column in expected & cols:
                wanted = 'integer' if ((table == 'internal_gateway_provisioning_authorizations' and column == 'version') or
                                       (table == 'cloud_gateway_camera_entitlements' and column in {'version','camera_number'}) or
                                       (table == 'cloud_gateway_subscription_evidence' and column in {'quantity','amount_cents','interval_count','current_period_end','payment_verified','event_created'}) or
                                       (table in {'cloud_gateway_evidence_holds','cloud_gateway_webhook_correlation'} and column == 'event_created') or
                                       (table == 'cloud_gateway_refresh_leases' and column == 'lease_until') or
                                       (table == 'gateway_checkout_attempts' and column in {'quantity','authorization_invalidated'})) else 'text'
                if types[column] != wanted:
                    issues.append('Incompatible column type: '+table+'.'+column)
        for name in INDEXES:
            if not _index_compatible(_index(db,backend,name),name):
                issues.append('Missing or incompatible index: '+name)
        records = db.execute('SELECT version,applied_at FROM schema_migrations').fetchall()
        applied = {item['version']:str(item['applied_at'] or '') for item in records}
        for version in VERSIONS:
            if not applied.get(version): issues.append('Missing migration record: '+version)
        for prerequisite, dependent in DEPENDENCIES:
            if applied.get(prerequisite) and applied.get(dependent) and applied[prerequisite] > applied[dependent]:
                issues.append('Migration ordering conflict: '+prerequisite+' must precede '+dependent)
        for sql,label in (("SELECT appliance_id FROM appliance_claims WHERE appliance_id IS NOT NULL AND status='completed' AND revoked_at IS NULL GROUP BY appliance_id HAVING COUNT(*)>1",'completed appliance claims'),
                          ("SELECT LOWER(device_id) FROM appliance_claims WHERE status IN ('pending','claimed') AND revoked_at IS NULL GROUP BY LOWER(device_id) HAVING COUNT(*)>1",'pending/claimed device sessions'),
                          ("SELECT camera_id FROM cloud_gateway_camera_entitlements WHERE status IN ('pending','active','suspended') GROUP BY camera_id HAVING COUNT(*)>1",'cloud camera bindings')):
            if db.execute(sql).fetchone(): issues.append('Duplicate current '+label+'.')
    except Exception:
        issues.append('Schema metadata or duplicate-claim query unavailable.')
    return {'ready':not issues,'issues':issues}
