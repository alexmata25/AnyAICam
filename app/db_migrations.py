import logging
from datetime import datetime

from database_backend import connect

logger=logging.getLogger('anyaicam.migrations')

MIGRATIONS=[
    # Provisioning Phase 1 (customer purchase -> AWS entitlement -> VMS
    # activation): the authoritative record of what a customer has
    # actually paid for, independent of the pre-existing partner-entered
    # `plans`/`analytics_subscriptions` quote tables (which a partner
    # types in by hand during onboarding and are never touched by a real
    # Stripe event) and independent of the legacy billing_accounts.json/
    # LICENSE_PLAN_FEATURES system the live Stripe webhook currently
    # updates (keyed to the old current_user()/users.json identity, not
    # to customers.id). See customer_entitlements.py's module docstring
    # for the full audit finding this migration exists to fix.
    #
    # camera_slot_quantity is deliberately a plain integer summed at read
    # time (customer_entitlements.total_camera_slots()), not a running
    # ledger of +/- adjustments -- an entitlement row always reflects
    # "what Stripe currently says for this product", so a later webhook
    # for the same customer_id+product updates the existing row in place
    # instead of accumulating duplicates.
    ('20260908_customer_entitlements','''
CREATE TABLE IF NOT EXISTS customer_entitlements(
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    product TEXT NOT NULL,
    camera_slot_quantity INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    stripe_checkout_session_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE INDEX IF NOT EXISTS idx_customer_entitlements_customer ON customer_entitlements(customer_id,product);
CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_entitlements_customer_product ON customer_entitlements(customer_id,product);
CREATE TABLE IF NOT EXISTS pending_customer_links(
    id TEXT PRIMARY KEY,
    normalized_email TEXT NOT NULL,
    stripe_customer_id TEXT,
    stripe_checkout_session_id TEXT,
    product TEXT NOT NULL,
    camera_slot_quantity INTEGER NOT NULL DEFAULT 0,
    raw_event_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_customer_id TEXT,
    FOREIGN KEY(resolved_customer_id) REFERENCES customers(id)
);
CREATE INDEX IF NOT EXISTS idx_pending_customer_links_email_status ON pending_customer_links(normalized_email,status);
CREATE TABLE IF NOT EXISTS provisioning_webhook_events(
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    result TEXT NOT NULL
);
'''),
    ('20260907_customer_registration_requests','''
CREATE TABLE IF NOT EXISTS customer_registration_requests(
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    partner_id TEXT,
    customer_id TEXT,
    user_id TEXT,
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    rejection_reason TEXT,
    FOREIGN KEY(partner_id) REFERENCES partners(id),
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(user_id) REFERENCES partner_users(id)
);
CREATE INDEX IF NOT EXISTS idx_customer_registration_status_partner ON customer_registration_requests(status,partner_id,requested_at);
'''),
    ('20260801_cloud_security','''
CREATE TABLE IF NOT EXISTS account_lockouts(email TEXT PRIMARY KEY,attempts INTEGER NOT NULL DEFAULT 0,locked_until TEXT,last_attempt_at TEXT);
CREATE TABLE IF NOT EXISTS password_reset_tokens(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,email TEXT NOT NULL,token_hash TEXT NOT NULL,expires_at TEXT NOT NULL,used_at TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS data_retention_policies(category TEXT PRIMARY KEY,retention_days INTEGER NOT NULL,updated_at TEXT NOT NULL,updated_by TEXT);
CREATE TABLE IF NOT EXISTS storage_objects(id TEXT PRIMARY KEY,category TEXT NOT NULL,object_key TEXT NOT NULL,backend TEXT NOT NULL,size INTEGER,sha256 TEXT,created_at TEXT NOT NULL,UNIQUE(category,object_key));
CREATE TABLE IF NOT EXISTS email_messages(id TEXT PRIMARY KEY,message_type TEXT NOT NULL,recipient TEXT NOT NULL,status TEXT NOT NULL,provider TEXT NOT NULL,metadata_json TEXT,created_at TEXT NOT NULL);
'''),
    ('20260801_partner_website','''
CREATE TABLE IF NOT EXISTS partner_applications(id TEXT PRIMARY KEY,company_name TEXT NOT NULL,contact_name TEXT NOT NULL,email TEXT NOT NULL,phone TEXT,website TEXT,service_area TEXT,license_information TEXT,company_type TEXT,estimated_installations INTEGER,notes TEXT,status TEXT NOT NULL DEFAULT 'pending',submitted_at TEXT NOT NULL,reviewed_at TEXT,reviewed_by TEXT);
CREATE TABLE IF NOT EXISTS partner_terms_acceptances(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,terms_version TEXT NOT NULL,accepted_at TEXT NOT NULL,ip_address TEXT,FOREIGN KEY(user_id) REFERENCES partner_users(id));
'''),
    ('20260801_unified_customer_platform','''
CREATE TABLE IF NOT EXISTS user_sessions(id TEXT PRIMARY KEY,user_id TEXT,email TEXT NOT NULL,role TEXT NOT NULL,device_name TEXT,session_type TEXT NOT NULL,token_hash TEXT,created_at TEXT NOT NULL,last_seen_at TEXT,expires_at TEXT NOT NULL,revoked_at TEXT,ip_address TEXT,user_agent TEXT);
CREATE TABLE IF NOT EXISTS customer_camera_permissions(user_id TEXT NOT NULL,camera_id TEXT NOT NULL,can_live INTEGER NOT NULL DEFAULT 1,can_playback INTEGER NOT NULL DEFAULT 1,can_download INTEGER NOT NULL DEFAULT 0,can_share INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(user_id,camera_id),FOREIGN KEY(user_id) REFERENCES partner_users(id),FOREIGN KEY(camera_id) REFERENCES cameras(id));
CREATE TABLE IF NOT EXISTS customer_site_permissions(user_id TEXT NOT NULL,site_id TEXT NOT NULL,PRIMARY KEY(user_id,site_id),FOREIGN KEY(user_id) REFERENCES partner_users(id),FOREIGN KEY(site_id) REFERENCES sites(id));
CREATE TABLE IF NOT EXISTS customer_clip_shares(id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,recording_id TEXT NOT NULL,created_by TEXT NOT NULL,expires_at TEXT NOT NULL,revoked_at TEXT,created_at TEXT NOT NULL,FOREIGN KEY(customer_id) REFERENCES customers(id));
CREATE TABLE IF NOT EXISTS customer_bookmarks(id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,camera_id TEXT,event_timestamp TEXT NOT NULL,note TEXT,created_by TEXT NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(customer_id) REFERENCES customers(id));
CREATE TABLE IF NOT EXISTS mfa_settings(user_id TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 0,method TEXT,status TEXT NOT NULL DEFAULT 'not_configured',updated_at TEXT,FOREIGN KEY(user_id) REFERENCES partner_users(id));
'''),
    ('20260801_secure_video_preparation','''
CREATE TABLE IF NOT EXISTS live_view_sessions(id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,site_id TEXT NOT NULL,camera_id TEXT NOT NULL,user_id TEXT,requested_by TEXT NOT NULL,role TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'requested',transport TEXT NOT NULL DEFAULT 'not_configured',requested_at TEXT NOT NULL,ready_at TEXT,failed_at TEXT,expires_at TEXT NOT NULL,error TEXT,relay_reference TEXT,FOREIGN KEY(customer_id) REFERENCES customers(id),FOREIGN KEY(site_id) REFERENCES sites(id),FOREIGN KEY(camera_id) REFERENCES cameras(id));
CREATE TABLE IF NOT EXISTS customer_clip_jobs(id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,camera_id TEXT NOT NULL,requested_by TEXT NOT NULL,start_time TEXT NOT NULL,end_time TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'requested',recording_id TEXT,created_at TEXT NOT NULL,expires_at TEXT,error TEXT,FOREIGN KEY(customer_id) REFERENCES customers(id),FOREIGN KEY(camera_id) REFERENCES cameras(id));
'''),
    ('20260801_pwa_notifications','''
CREATE TABLE IF NOT EXISTS mobile_devices(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,customer_id TEXT,device_uid TEXT NOT NULL,device_type TEXT,platform TEXT,push_token TEXT,last_active_at TEXT,app_version TEXT,revoked_at TEXT,created_at TEXT NOT NULL,UNIQUE(user_id,device_uid),FOREIGN KEY(user_id) REFERENCES partner_users(id),FOREIGN KEY(customer_id) REFERENCES customers(id));
CREATE TABLE IF NOT EXISTS mobile_refresh_tokens(id TEXT PRIMARY KEY,family_id TEXT NOT NULL,user_id TEXT NOT NULL,device_id TEXT NOT NULL,token_hash TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,used_at TEXT,revoked_at TEXT,replaced_by TEXT,reuse_detected_at TEXT,FOREIGN KEY(user_id) REFERENCES partner_users(id),FOREIGN KEY(device_id) REFERENCES mobile_devices(id));
CREATE TABLE IF NOT EXISTS notification_preferences(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,customer_id TEXT NOT NULL,site_id TEXT,camera_id TEXT,event_type TEXT NOT NULL,severity TEXT NOT NULL DEFAULT 'all',schedule_start TEXT NOT NULL DEFAULT '00:00',schedule_end TEXT NOT NULL DEFAULT '23:59',in_app INTEGER NOT NULL DEFAULT 1,email INTEGER NOT NULL DEFAULT 0,web_push INTEGER NOT NULL DEFAULT 0,sms INTEGER NOT NULL DEFAULT 0,enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(user_id,customer_id,site_id,camera_id,event_type),FOREIGN KEY(user_id) REFERENCES partner_users(id),FOREIGN KEY(customer_id) REFERENCES customers(id));
CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,customer_id TEXT NOT NULL,site_id TEXT,camera_id TEXT,event_id TEXT,recording_id TEXT,event_type TEXT NOT NULL,severity TEXT NOT NULL,title TEXT NOT NULL,message TEXT,timestamp TEXT NOT NULL,thumbnail TEXT,read_at TEXT,acknowledged_at TEXT,dismissed_at TEXT,bookmarked_at TEXT,created_at TEXT NOT NULL,FOREIGN KEY(user_id) REFERENCES partner_users(id),FOREIGN KEY(customer_id) REFERENCES customers(id));
CREATE TABLE IF NOT EXISTS notification_deliveries(id TEXT PRIMARY KEY,notification_id TEXT NOT NULL,channel TEXT NOT NULL,status TEXT NOT NULL,provider TEXT,error TEXT,created_at TEXT NOT NULL,FOREIGN KEY(notification_id) REFERENCES notifications(id));
'''),
    ('20260816_live_relay_idle_tracking','''
CREATE TABLE IF NOT EXISTS live_relay_idle_tracking(camera_id TEXT PRIMARY KEY,appliance_id TEXT NOT NULL,idle_since TEXT NOT NULL,stop_queued_at TEXT,FOREIGN KEY(camera_id) REFERENCES cameras(id));
'''),
    ('20260821_recordings_catalog','''
CREATE TABLE IF NOT EXISTS recordings(id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,site_id TEXT NOT NULL,appliance_id TEXT NOT NULL,camera_id TEXT NOT NULL,s3_key TEXT NOT NULL,started_at TEXT NOT NULL,ended_at TEXT NOT NULL,duration_seconds INTEGER,size_bytes INTEGER,status TEXT NOT NULL DEFAULT 'available',created_at TEXT NOT NULL,UNIQUE(camera_id,s3_key),FOREIGN KEY(customer_id) REFERENCES customers(id),FOREIGN KEY(site_id) REFERENCES sites(id),FOREIGN KEY(appliance_id) REFERENCES appliances(id),FOREIGN KEY(camera_id) REFERENCES cameras(id));
CREATE INDEX IF NOT EXISTS idx_recordings_camera_started ON recordings(camera_id,started_at);
'''),
    ('20260821_detection_events','''
CREATE TABLE IF NOT EXISTS detection_events(id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,site_id TEXT NOT NULL,appliance_id TEXT NOT NULL,camera_id TEXT NOT NULL,local_event_id TEXT NOT NULL,event_type TEXT NOT NULL,confidence REAL,object_count INTEGER NOT NULL DEFAULT 1,detections_json TEXT,event_timestamp TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(camera_id,local_event_id),FOREIGN KEY(customer_id) REFERENCES customers(id),FOREIGN KEY(site_id) REFERENCES sites(id),FOREIGN KEY(appliance_id) REFERENCES appliances(id),FOREIGN KEY(camera_id) REFERENCES cameras(id));
CREATE INDEX IF NOT EXISTS idx_detection_events_camera_timestamp ON detection_events(camera_id,event_timestamp);
'''),
    ('20260821_talk_down_sessions','''
CREATE TABLE IF NOT EXISTS customer_talk_sessions(id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,site_id TEXT NOT NULL,camera_id TEXT NOT NULL,user_id TEXT,requested_by TEXT NOT NULL,role TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'requested',requested_at TEXT NOT NULL,ended_at TEXT,expires_at TEXT NOT NULL,FOREIGN KEY(customer_id) REFERENCES customers(id),FOREIGN KEY(site_id) REFERENCES sites(id),FOREIGN KEY(camera_id) REFERENCES cameras(id));
CREATE INDEX IF NOT EXISTS idx_customer_talk_sessions_camera ON customer_talk_sessions(camera_id,state);
'''),
    # Restored from the production/AWS deployment's own db_migrations.py
    # (verified by reading its actual SQL over SSH, not inferred from
    # the migration ID) -- this branch was missing it entirely, which
    # is the confirmed root cause of "no such table: camera_
    # provisioning_requests" (partner_workspace.py's provisioning-job
    # queue) and all 12 test_camera_discovery_provisioning.py failures.
    # Production already has this version recorded in its own schema_
    # migrations table, so apply_migrations() skips re-running it there
    # entirely (see the `if version in applied: continue` guard below)
    # -- restoring it here only changes behavior on a database that
    # never had it: every fresh install (Samsung after a wipe, Ryzen, a
    # new customer appliance).
    #
    # Production's version of this migration also does `ALTER TABLE
    # cameras ADD COLUMN device_key TEXT` and creates an appliance-
    # scoped idx_cameras_appliance_device_key index -- deliberately NOT
    # included here verbatim. This branch already, independently, adds
    # device_key via apply_migrations()'s own unconditional camera_
    # columns block above (with its own idempotent existence check) and
    # creates a DIFFERENT index there -- idx_cameras_customer_device_key,
    # customer-scoped and UNIQUE-partial, not appliance-scoped and
    # plain like production's. Running production's raw `ALTER TABLE
    # ADD COLUMN` here crashes with "duplicate column name: device_key"
    # on any database (confirmed on this repo's own local dev database)
    # that already has the column from that other path but never had
    # this migration version recorded -- true for this branch's own
    # already-running installations (Samsung, almost certainly), not
    # just a hypothetical. The column-add is intentionally left to that
    # existing, already-idempotent mechanism; production's own missing
    # appliance-scoped index is created there instead (see
    # idx_cameras_appliance_device_key, added right after idx_cameras_
    # customer_device_key above) so both indexes still end up existing,
    # matching production, without the crash. The two indexes'
    # divergence (unique-partial-customer-scoped vs. plain-appliance-
    # scoped) is NOT reconciled by this change -- restoring production's
    # camera_provisioning_requests table is the only thing this
    # migration is responsible for; the index question stays open.
    ('20260824_camera_discovery','''
CREATE TABLE IF NOT EXISTS camera_provisioning_requests(id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,appliance_id TEXT NOT NULL,site_id TEXT NOT NULL,device_key TEXT NOT NULL,camera_name TEXT NOT NULL,recording_mode TEXT,analytics_json TEXT NOT NULL DEFAULT '[]',encrypted_credentials BLOB,status TEXT NOT NULL DEFAULT 'queued',camera_id TEXT,message TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,FOREIGN KEY(customer_id) REFERENCES customers(id),FOREIGN KEY(appliance_id) REFERENCES appliances(id),FOREIGN KEY(site_id) REFERENCES sites(id));
CREATE INDEX IF NOT EXISTS idx_camera_provisioning_appliance_status ON camera_provisioning_requests(appliance_id,status);
'''),
    ('20260827_camera_provisioning','''
CREATE TABLE IF NOT EXISTS camera_credentials(camera_id TEXT PRIMARY KEY,encrypted_blob TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,FOREIGN KEY(camera_id) REFERENCES cameras(id));
'''),
    ('20260827_camera_analytics_entitlements','''
CREATE TABLE IF NOT EXISTS camera_analytics_entitlements(camera_id TEXT NOT NULL,analytic_key TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(camera_id,analytic_key),FOREIGN KEY(camera_id) REFERENCES cameras(id));
'''),
    ('20260901_detection_event_media','''
CREATE TABLE IF NOT EXISTS detection_event_media(
    id TEXT PRIMARY KEY,
    detection_event_id TEXT NOT NULL UNIQUE,
    customer_id TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    s3_key TEXT NOT NULL,
    thumbnail_s3_key TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    duration_seconds REAL,
    size_bytes INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY(detection_event_id) REFERENCES detection_events(id),
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(camera_id) REFERENCES cameras(id)
);
CREATE INDEX IF NOT EXISTS idx_detection_event_media_camera
ON detection_event_media(camera_id,started_at);
'''),
    # Provisioning Phase 5: one-time HARDWARE purchases (Ryzen appliances,
    # the Numato relay module) get their own table, deliberately separate
    # from customer_entitlements (recurring Local/Hybrid camera-slot
    # subscriptions). See hardware_orders.py's module docstring for the
    # full separation contract -- a hardware Price ID must never resolve
    # against PRICE_ID_CAMERA_SLOT_MAP and a camera-slot Price ID must
    # never resolve against HARDWARE_CATALOG; each module's own resolver
    # only recognizes its own price IDs and fails closed on everything
    # else, so this table and customer_entitlements can never cross-grant.
    #
    # customer_id is nullable (unlike customer_entitlements' NOT NULL):
    # a hardware purchase can complete before the buyer's authoritative
    # customer_id is resolvable (no signed-in session, checkout email
    # doesn't match an existing customer yet) -- see hardware_orders.py's
    # pending-link handling, which mirrors customer_entitlements.py's own
    # pending_customer_links pattern rather than inventing a second one.
    #
    # One row per (stripe_checkout_session_id, sku): a session can contain
    # more than one hardware line item (see stripe-checkout.php's existing
    # multi-item hardware_items pattern), and Stripe may retry the same
    # webhook delivery, so this composite unique index is what makes a
    # replayed event a no-op rather than a duplicate order row.
    ('20260909_hardware_orders','''
CREATE TABLE IF NOT EXISTS hardware_orders(
    id TEXT PRIMARY KEY,
    customer_id TEXT,
    sku TEXT NOT NULL,
    product_name TEXT NOT NULL,
    stripe_price_id TEXT NOT NULL,
    stripe_checkout_session_id TEXT,
    stripe_payment_intent_id TEXT,
    stripe_customer_id TEXT,
    quantity INTEGER NOT NULL DEFAULT 1,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'usd',
    status TEXT NOT NULL DEFAULT 'pending',
    fulfillment_status TEXT NOT NULL DEFAULT 'unfulfilled',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE INDEX IF NOT EXISTS idx_hardware_orders_customer ON hardware_orders(customer_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hardware_orders_session_sku ON hardware_orders(stripe_checkout_session_id,sku);
CREATE TABLE IF NOT EXISTS pending_hardware_order_links(
    id TEXT PRIMARY KEY,
    normalized_email TEXT NOT NULL,
    stripe_customer_id TEXT,
    stripe_checkout_session_id TEXT,
    stripe_price_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    product_name TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    amount_cents INTEGER NOT NULL,
    raw_event_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_customer_id TEXT,
    FOREIGN KEY(resolved_customer_id) REFERENCES customers(id)
);
CREATE INDEX IF NOT EXISTS idx_pending_hardware_links_email_status ON pending_hardware_order_links(normalized_email,status);
'''),
    # Provisioning Phase 6: durable, at-most-once tracking of customer-
    # facing post-purchase emails ("your service is ready", "plan
    # updated", "cancelled", hardware order confirmation, "complete your
    # setup"). Deliberately its own table, separate from both the
    # entitlement-processing idempotency (provisioning_webhook_events --
    # gates whether entitlement LOGIC re-runs) and the generic in-app
    # notifications/notification_deliveries tables (event/camera-alert
    # shaped, not purchase-shaped). See purchase_notifications.py's
    # module docstring for why these two idempotency concerns must stay
    # separate: entitlement processing and email delivery can fail
    # independently, and a failed email must be retryable on the next
    # webhook redelivery WITHOUT re-running (or being blocked by) already-
    # completed entitlement processing.
    #
    # UNIQUE(stripe_event_id,notification_type): the same Stripe event,
    # redelivered any number of times, can produce at most one row per
    # notification type -- a second delivery either finds status='sent'
    # (skip, already sent) or status='failed' (retry the send, update
    # this same row in place).
    ('20260909_provisioning_notifications','''
CREATE TABLE IF NOT EXISTS provisioning_notifications(
    id TEXT PRIMARY KEY,
    stripe_event_id TEXT NOT NULL,
    notification_type TEXT NOT NULL,
    customer_id TEXT,
    recipient_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error_detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_provisioning_notifications_event_type ON provisioning_notifications(stripe_event_id,notification_type);
CREATE INDEX IF NOT EXISTS idx_provisioning_notifications_customer ON provisioning_notifications(customer_id);
'''),
    # Provisioning Phase 8: hardware order FULFILLMENT lifecycle (paid ->
    # preparing -> configuring -> testing -> ready_to_ship -> shipped ->
    # delivered, or cancelled) and the separate RETURN/refund workflow.
    # See hardware_fulfillment.py's and hardware_returns.py's module
    # docstrings for the full state-machine contract. Additive columns on
    # the EXISTING hardware_orders table (never touches customer_
    # entitlements or camera_slot logic in any way -- hardware fulfillment
    # and camera-slot subscriptions remain completely separate systems,
    # per this phase's explicit requirement) plus one new table for
    # returns, since a return is a materially different shape of record
    # (inspection findings, restocking fee, approved refund amount) than
    # an order itself, and one order can in principle have zero or one
    # return -- never modeled as more columns bolted onto hardware_orders.
    ('20260910_hardware_fulfillment_and_returns','''
ALTER TABLE hardware_orders ADD COLUMN order_number TEXT;
ALTER TABLE hardware_orders ADD COLUMN carrier TEXT;
ALTER TABLE hardware_orders ADD COLUMN tracking_number TEXT;
ALTER TABLE hardware_orders ADD COLUMN tracking_link TEXT;
ALTER TABLE hardware_orders ADD COLUMN shipped_at TEXT;
ALTER TABLE hardware_orders ADD COLUMN delivered_at TEXT;
ALTER TABLE hardware_orders ADD COLUMN cancelled_at TEXT;
CREATE TABLE IF NOT EXISTS hardware_returns(
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    customer_id TEXT,
    sku TEXT NOT NULL,
    serial_number TEXT,
    status TEXT NOT NULL DEFAULT 'return_requested',
    return_reference TEXT,
    requested_at TEXT NOT NULL,
    authorized_at TEXT,
    shipped_back_at TEXT,
    received_at TEXT,
    condition_notes TEXT,
    accessories_included TEXT,
    damage_notes TEXT,
    inspected_at TEXT,
    inspected_by TEXT,
    original_amount_cents INTEGER NOT NULL,
    restocking_fee_cents INTEGER,
    restocking_fee_percent_applied REAL,
    approved_refund_cents INTEGER,
    refund_status TEXT NOT NULL DEFAULT 'not_started',
    refund_approved_by TEXT,
    refund_approved_at TEXT,
    refunded_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(order_id) REFERENCES hardware_orders(id),
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE INDEX IF NOT EXISTS idx_hardware_returns_order ON hardware_returns(order_id);
CREATE INDEX IF NOT EXISTS idx_hardware_returns_customer ON hardware_returns(customer_id);
'''),
    # Provisioning Phase 8 follow-up: explicit, testable defective/
    # damaged-on-arrival exception to the restocking fee. Approved
    # policy: no restocking fee applies when the hardware was defective
    # due to AnyAiCam or arrived damaged. Recorded as its own boolean at
    # inspection time (see hardware_returns.record_inspection_and_
    # calculate_refund()) rather than inferred from damage_notes text --
    # an explicit flag an admin deliberately sets, never a guess parsed
    # from free text.
    ('20260911_hardware_returns_defective_flag','''
ALTER TABLE hardware_returns ADD COLUMN is_defective_or_damaged_on_arrival INTEGER NOT NULL DEFAULT 0;
'''),
    # Website checkout build: an anonymous storefront visitor buying an
    # analytics add-on (no signed-in customer_owner session, no matching
    # customers row for their email yet) needs the exact same checkout-
    # before-registration safety net customer_entitlements.py's
    # pending_customer_links and hardware_orders.py's pending_hardware_
    # order_links already give camera-slot and hardware purchases -- see
    # analytics_entitlements.py's own module docstring for why this is
    # the same existing pattern, not a new one. Schema deliberately
    # mirrors pending_hardware_order_links column-for-column (minus the
    # hardware-only sku/product_name/amount_cents columns, plus
    # analytic_key in their place).
    ('20260912_pending_analytics_links','''
CREATE TABLE IF NOT EXISTS pending_analytics_links(
    id TEXT PRIMARY KEY,
    normalized_email TEXT NOT NULL,
    stripe_customer_id TEXT,
    stripe_checkout_session_id TEXT,
    stripe_price_id TEXT NOT NULL,
    analytic_key TEXT NOT NULL,
    raw_event_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_customer_id TEXT,
    FOREIGN KEY(resolved_customer_id) REFERENCES customers(id)
);
CREATE INDEX IF NOT EXISTS idx_pending_analytics_links_email_status ON pending_analytics_links(normalized_email,status);
'''),
    ('20260910_appliance_update_results','''
CREATE TABLE IF NOT EXISTS appliance_update_results(
    update_id TEXT NOT NULL,
    appliance_id TEXT NOT NULL,
    from_version TEXT,
    to_version TEXT,
    state TEXT NOT NULL,
    error TEXT,
    rollback_from TEXT,
    duration_seconds REAL,
    reported_at TEXT NOT NULL,
    PRIMARY KEY(update_id,appliance_id),
    FOREIGN KEY(appliance_id) REFERENCES appliances(id)
);
'''),
    ('20260910_appliance_claims','''
CREATE TABLE IF NOT EXISTS appliance_claims(
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    claim_session_id TEXT UNIQUE NOT NULL,
    claim_code_hash TEXT NOT NULL,
    claim_proof_hash TEXT,
    claim_proof_plaintext TEXT,
    status TEXT NOT NULL,
    customer_id TEXT,
    site_id TEXT,
    claimed_by TEXT,
    appliance_id TEXT,
    proof_expires_at TEXT,
    expires_at TEXT NOT NULL,
    claimed_at TEXT,
    completed_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(site_id) REFERENCES sites(id),
    FOREIGN KEY(appliance_id) REFERENCES appliances(id)
);
CREATE INDEX IF NOT EXISTS idx_appliance_claims_device_id ON appliance_claims(device_id);
CREATE INDEX IF NOT EXISTS idx_appliance_claims_status ON appliance_claims(status);
'''),
    # Cloud->edge camera-configuration sync (2026-09-12): closes the gap
    # where a camera successfully provisioned through the cloud (RTSP
    # DESCRIBE already verified by the appliance-agent) never reached the
    # edge VMS's own camera_credentials table, so process_supervisor()
    # never had a credential to start the stream with -- see
    # app/edge_camera_sync.py's own module docstring for the full trace.
    # This table exists ONLY on an edge appliance's local database
    # (RUNTIME_ROLE=edge/combined); the cloud database never uses it.
    # Keyed by device_key (known to the appliance-agent immediately,
    # before the cloud has necessarily assigned this camera a
    # camera_number/camera_id it could otherwise be keyed by), holding
    # only an already-encrypted blob -- the local POST endpoint that
    # writes this row (main.py's provisioned_camera_credential()) encrypts
    # before this row is ever created; nothing in this codebase ever
    # writes a plaintext value here. edge_camera_sync.sync_provisioned_
    # cameras() moves a row from here into the real camera_credentials
    # table (keyed by camera_id) the moment it learns this device_key's
    # assigned camera_id from GET /api/appliance/configuration, then
    # deletes it from here -- so this table only ever holds a credential
    # that arrived locally before the cloud-assigned camera_id was known
    # yet, never a permanent second copy.
    ('20260912_pending_camera_credentials','''
CREATE TABLE IF NOT EXISTS pending_camera_credentials(
    device_key TEXT PRIMARY KEY,
    encrypted_blob TEXT NOT NULL,
    created_at TEXT NOT NULL
);
'''),
    # RDM-controlled Hybrid cloud cost policy (2026-09-16). Cloud-side
    # authoritative source: an RDM administrator's own explicit override
    # of the system defaults (event_media_policy.DEFAULT_DAILY_SECONDS
    # for daily_cloud_seconds; MOTION_RETENTION_DAYS' 7/14/30 set for
    # retention_days), one row per customer, NULL meaning "no override,
    # use the system default" -- never a second, silently-conflicting
    # copy of either default. One row per customer (PRIMARY KEY), the
    # same upsert-in-place shape as customer_entitlements/customer_
    # notification_channels, not accumulating history rows.
    #
    # camera_cloud_upload_daily is the EDGE-side counterpart: real,
    # local, DB-backed daily usage per camera, so the allowance survives
    # a container/service restart (an in-memory counter, like recording_
    # uploader.py's own _uploaded_files dict, would silently reset the
    # allowance on every restart -- exactly the bypass this table exists
    # to prevent). Keyed by (camera_number, upload_date) so a new day
    # starts a fresh row automatically -- no explicit "reset" logic
    # needed anywhere. Present (harmlessly, always empty) on the cloud
    # role too, since this schema-init code runs identically on both
    # roles; only the edge-side recording_upload_worker() ever writes to
    # it.
    ('20260916_cloud_cost_policy','''
CREATE TABLE IF NOT EXISTS customer_cloud_policy(
    customer_id TEXT PRIMARY KEY,
    daily_cloud_seconds INTEGER,
    retention_days INTEGER,
    updated_at TEXT NOT NULL,
    updated_by TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE TABLE IF NOT EXISTS camera_cloud_upload_daily(
    camera_number INTEGER NOT NULL,
    upload_date TEXT NOT NULL,
    seconds_uploaded INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(camera_number,upload_date)
);
'''),
    # P2P live-view foundation (2026-09-16): direct browser<->appliance
    # WebRTC as the PREFERRED transport, with the already-proven S3/
    # CloudFront relay (live_relay_uploader.py/live_playlist.py, unchanged
    # by this migration) kept as the automatic fallback -- never replaced,
    # per explicit product direction. p2p_attempted/transport/ready_at/
    # failed_at/error/relay_bytes are the instrumentation a later cost/
    # NAT-failure-rate decision (e.g. whether to add self-hosted TURN)
    # needs; transport/ready_at/failed_at/error already existed on this
    # table (unused placeholders from the original relay design) and are
    # now actually written to, not newly added.
    #
    # live_view_p2p_signaling is a small, append-only offer/answer/ICE
    # relay table -- both sides (customer browser, appliance) reach it
    # only through their OWN already-authenticated channel (partner_
    # identity cookie for the browser, authenticate_appliance() bearer+
    # nonce for the appliance), so this table never becomes a new
    # authentication boundary of its own. `kind` distinguishes the four
    # message types sharing one poll/insert shape; `consumed_at` makes
    # each poll idempotent (a message is delivered to its one intended
    # reader exactly once) without needing a queue service.
    ('20260917_live_view_p2p','''
ALTER TABLE live_view_sessions ADD COLUMN p2p_attempted INTEGER NOT NULL DEFAULT 0;
ALTER TABLE live_view_sessions ADD COLUMN relay_bytes INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS live_view_p2p_signaling(
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    FOREIGN KEY(session_id) REFERENCES live_view_sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_live_view_p2p_signaling_poll ON live_view_p2p_signaling(session_id,kind,consumed_at);
'''),
    # Session-duration instrumentation (2026-09-17): a terminal timestamp
    # distinct from expires_at (the SESSION_DURATION_SECONDS ceiling, not
    # when a viewer actually stopped watching) -- set once, by whichever
    # of stop_live_view()'s explicit customer-initiated stop or
    # _sweep_expired_sessions()'s lazy expiry sweep reaches a 'requested'
    # row first. stopped_at-minus-ready_at is real watched duration,
    # transport-agnostic (works identically for 'p2p' or 'relay').
    ('20260917_live_view_session_stopped_at','''
ALTER TABLE live_view_sessions ADD COLUMN stopped_at TEXT;
'''),
    # Local recording storage management (2026-09-17): RDM-configurable
    # thresholds, one row per customer (NULL meaning "no override, use
    # the system default"), mirroring customer_cloud_policy's exact
    # established shape/upsert-in-place convention -- never a second,
    # silently-conflicting copy of the system defaults
    # (local_storage_policy.DEFAULT_RESERVED_FREE_PERCENT/
    # DEFAULT_WARNING_FREE_PERCENT).
    #
    # local_storage_cleanup_log is the durable, queryable audit trail
    # "record every automatic cleanup action" requires -- one row per
    # deleted local recording file, on the EDGE appliance's own local
    # DB (this feature runs entirely on edge/combined role; the row
    # never needs to leave the appliance for the cleanup action itself
    # to be auditable there). Present, harmlessly always empty, on the
    # cloud role too, since this schema-init code runs identically on
    # both roles -- same pattern customer_cloud_policy/camera_cloud_
    # upload_daily already established.
    #
    # storage_state/storage_free_percent/storage_last_cleanup_at on
    # appliances/appliance_health_history are the RDM-visible surface
    # ("Healthy"/"Warning"/"Cleanup Active"/"Critical") -- populated via
    # the existing heartbeat channel (POST /api/appliance/heartbeat,
    # same one disk_capacity/disk_used already flow through), not a new
    # sync mechanism.
    ('20260917_local_storage_management','''
CREATE TABLE IF NOT EXISTS local_storage_policy(
    customer_id TEXT PRIMARY KEY,
    reserved_free_percent INTEGER,
    warning_free_percent INTEGER,
    updated_at TEXT NOT NULL,
    updated_by TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE TABLE IF NOT EXISTS local_storage_cleanup_log(
    id TEXT PRIMARY KEY,
    camera_number INTEGER NOT NULL,
    camera_id TEXT,
    file_name TEXT NOT NULL,
    recording_started_at TEXT,
    size_bytes INTEGER,
    deleted_at TEXT NOT NULL,
    trigger_free_percent REAL,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_local_storage_cleanup_log_deleted_at ON local_storage_cleanup_log(deleted_at);
'''),
    # Local recording AGE retention (2026-09-17), independent of the free-
    # space reserve/warning percentages added above -- see local_storage_
    # policy.py's own module docstring for why this is a separate
    # customer/appliance-configurable knob, and explicitly NOT the same
    # setting as customer_cloud_policy.retention_days (the existing
    # Hybrid AWS/S3 7/14/30-day cloud entitlement -- that one governs
    # uploaded event clips/thumbnails, this one only ever touches local
    # disk). NULL (the default) means "no age-based limit" -- only the
    # free-space reserve trigger applies until an RDM override sets a
    # real number, first used for the real Ryzen lab appliance's own
    # 7-day target.
    ('20260917_local_storage_retention_days','''
ALTER TABLE local_storage_policy ADD COLUMN local_retention_days INTEGER;
'''),
    # AAC (AnyAiCam facial recognition / access-control analytics),
    # Phase 1 -- see facial_recognition.py (CV/matching, no DB),
    # facial_people.py (enrollment/watchlist DB service),
    # facial_events.py (match-event creation + query, relay-rule
    # evaluation) and facial_recognition_ui.py (routes) for the code
    # that reads/writes these tables. Deliberately reuses the existing
    # detection_events/detection_event_media event system (event_type=
    # 'facial_recognition') rather than a parallel one -- facial_events
    # is an auxiliary detail table keyed 1:1 by detection_event_id,
    # exactly like detection_event_media already is.
    #
    # matched_person_id has no ON DELETE CASCADE: deleting an enrolled
    # person (facial_people.delete_person(), which DOES hard-delete
    # every facial_embeddings row -- the actual biometric templates --
    # for that person) must never delete history of a past match.
    # matched_person_name/matched_watchlist_name are denormalized
    # snapshots taken at match time for exactly this reason: event
    # history stays readable even after the live person/watchlist
    # record is gone.
    ('20260908_facial_recognition','''
CREATE TABLE IF NOT EXISTS facial_people(
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    site_id TEXT,
    external_reference TEXT,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(site_id) REFERENCES sites(id)
);
CREATE INDEX IF NOT EXISTS idx_facial_people_customer ON facial_people(customer_id,status);
CREATE TABLE IF NOT EXISTS facial_embeddings(
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    engine TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    embedding_json TEXT NOT NULL,
    source_image_path TEXT,
    quality REAL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(person_id) REFERENCES facial_people(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_facial_embeddings_person ON facial_embeddings(person_id);
CREATE INDEX IF NOT EXISTS idx_facial_embeddings_customer_engine ON facial_embeddings(customer_id,engine);
CREATE TABLE IF NOT EXISTS facial_watchlists(
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    site_id TEXT,
    name TEXT NOT NULL,
    classification TEXT NOT NULL DEFAULT 'alert',
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_facial_watchlists_customer_name ON facial_watchlists(customer_id,name);
CREATE TABLE IF NOT EXISTS facial_watchlist_members(
    watchlist_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    added_at TEXT NOT NULL,
    added_by TEXT,
    PRIMARY KEY(watchlist_id,person_id),
    FOREIGN KEY(watchlist_id) REFERENCES facial_watchlists(id) ON DELETE CASCADE,
    FOREIGN KEY(person_id) REFERENCES facial_people(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_facial_watchlist_members_person ON facial_watchlist_members(person_id);
CREATE TABLE IF NOT EXISTS facial_events(
    id TEXT PRIMARY KEY,
    detection_event_id TEXT NOT NULL UNIQUE,
    customer_id TEXT NOT NULL,
    site_id TEXT,
    camera_id TEXT NOT NULL,
    match_state TEXT NOT NULL,
    matched_person_id TEXT,
    matched_person_name TEXT,
    matched_watchlist_id TEXT,
    matched_watchlist_name TEXT,
    confidence REAL NOT NULL,
    engine TEXT NOT NULL,
    engine_version TEXT,
    face_bbox_json TEXT,
    face_thumbnail_path TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(detection_event_id) REFERENCES detection_events(id),
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(camera_id) REFERENCES cameras(id)
);
CREATE INDEX IF NOT EXISTS idx_facial_events_customer_created ON facial_events(customer_id,created_at);
CREATE INDEX IF NOT EXISTS idx_facial_events_matched_person ON facial_events(matched_person_id);
CREATE INDEX IF NOT EXISTS idx_facial_events_camera_created ON facial_events(camera_id,created_at);
CREATE TABLE IF NOT EXISTS facial_rules(
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    site_id TEXT,
    camera_id TEXT,
    name TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    watchlist_id TEXT,
    person_id TEXT,
    min_confidence REAL NOT NULL DEFAULT 0.85,
    relay_channel INTEGER NOT NULL,
    pulse_ms INTEGER NOT NULL DEFAULT 3000,
    cooldown_seconds INTEGER NOT NULL DEFAULT 10,
    dry_run INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(camera_id) REFERENCES cameras(id),
    FOREIGN KEY(watchlist_id) REFERENCES facial_watchlists(id),
    FOREIGN KEY(person_id) REFERENCES facial_people(id)
);
CREATE INDEX IF NOT EXISTS idx_facial_rules_customer ON facial_rules(customer_id,enabled);
CREATE TABLE IF NOT EXISTS facial_settings(
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL UNIQUE,
    min_confidence REAL NOT NULL DEFAULT 0.6,
    unknown_person_events_enabled INTEGER NOT NULL DEFAULT 1,
    debounce_seconds INTEGER NOT NULL DEFAULT 30,
    engine TEXT NOT NULL DEFAULT 'haar_intensity',
    updated_at TEXT NOT NULL,
    updated_by TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);
'''),
    # Face Access -- Door/Relay Control (2026-09-17), per the user's own
    # explicit product specification (docs/aac-face-access-door-control-
    # requirements.md). Builds on the facial_rules/relay_control.py
    # foundation the Phase 1 AAC merge above already provides -- this
    # migration adds only what that foundation didn't yet cover:
    #
    # schedule_start/schedule_end on facial_rules: the "current permitted
    # time/schedule" requirement for automatic-entry authorization
    # (mode 1). Nullable HH:MM strings, same wrap-aware convention
    # notification_engine.py's quiet_start/quiet_end already established
    # (NULL/NULL means "no schedule restriction", not "never
    # authorized" -- the fail-closed default matches every other
    # optional-override column in this codebase, e.g. local_storage_
    # policy.reserved_free_percent).
    #
    # door_access_events is the cross-cutting audit table the security
    # requirements explicitly call for: EVERY unlock attempt (manual
    # button press or automatic facial-recognition-triggered), covering
    # both at once, in one place -- distinct from facial_events (which
    # is about a face MATCH, not an unlock attempt; a manual unlock has
    # no face match at all, and an automatic unlock's own facial_events
    # row is linked here via facial_event_id, never duplicated).
    ('20260917_face_access_door_control','''
ALTER TABLE facial_rules ADD COLUMN schedule_start TEXT;
ALTER TABLE facial_rules ADD COLUMN schedule_end TEXT;
CREATE TABLE IF NOT EXISTS door_access_events(
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    door_name TEXT NOT NULL,
    relay_channel INTEGER,
    trigger_type TEXT NOT NULL,
    actor_user_id TEXT,
    actor_email TEXT,
    matched_person_id TEXT,
    matched_person_name TEXT,
    facial_event_id TEXT,
    authorization_result TEXT NOT NULL,
    relay_result TEXT NOT NULL,
    success INTEGER NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(camera_id) REFERENCES cameras(id),
    FOREIGN KEY(facial_event_id) REFERENCES facial_events(id)
);
CREATE INDEX IF NOT EXISTS idx_door_access_events_customer_created ON door_access_events(customer_id,created_at);
CREATE INDEX IF NOT EXISTS idx_door_access_events_camera_created ON door_access_events(camera_id,created_at);
'''),
    # WireGuard direct remote-connectivity, Phase A2 (see
    # docs/wireguard-remote-connectivity-plan.md Sec 10): one row per
    # enrolled appliance/BYO-PC WireGuard peer. Mirrors appliance_
    # credentials deliberately -- multiple rows per appliance are
    # allowed (never enforced to exactly one, so a rotation's overlap
    # window is normal), rows are never deleted on revocation
    # (revoked_at/revoked_reason set instead, matching this project's
    # own never-rewrite-history convention already applied to
    # audit_logs), and only an ACTIVE tunnel address needs to be
    # unique -- a revoked peer's old address can be safely reassigned
    # to a replacement device later. public_key is exactly that: a
    # public key, never a secret, so unlike appliance_credentials.
    # credential_hash it is stored in plain text on purpose -- there is
    # nothing to hash. The corresponding private key is generated on
    # the appliance itself and never transmitted or stored here, in
    # either direction; see wireguard_remote.py's own module docstring.
    ('20260917_wireguard_remote_connectivity','''
CREATE TABLE IF NOT EXISTS appliance_wireguard_peers(
    id TEXT PRIMARY KEY,
    appliance_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    public_key TEXT NOT NULL,
    tunnel_address TEXT NOT NULL,
    gateway_public_key TEXT NOT NULL,
    gateway_endpoint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'enrolled',
    last_handshake_at TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_reason TEXT,
    FOREIGN KEY(appliance_id) REFERENCES appliances(id),
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wireguard_peers_public_key ON appliance_wireguard_peers(public_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wireguard_peers_tunnel_address_active ON appliance_wireguard_peers(tunnel_address) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_wireguard_peers_appliance_status ON appliance_wireguard_peers(appliance_id,status);
'''),
    ('20260919_event_media_fetch_log','''
-- One row per real /api/customer/events/{camera_id}/{event_id}/media/direct
-- resolution (2026-09-19) -- the measurement this Hybrid cost-model pass
-- needs: how many bytes a real customer fetch actually moved over the
-- WireGuard/direct path vs. how many fell back to AWS/S3/CloudFront.
-- bytes_served is always the clip's own stored size_bytes (the same
-- physical file either way) -- for the 'aws' outcomes this route issues a
-- 302 to a presigned CloudFront URL and never sees the bytes itself, so
-- the stored size is the only honest number available, not a real
-- transferred-byte count off the wire.
CREATE TABLE IF NOT EXISTS event_media_fetch_log(
    id TEXT PRIMARY KEY,
    media_id TEXT NOT NULL,
    source TEXT NOT NULL,
    bytes_served INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(media_id) REFERENCES detection_event_media(id)
);
CREATE INDEX IF NOT EXISTS idx_event_media_fetch_log_media ON event_media_fetch_log(media_id,created_at);
CREATE INDEX IF NOT EXISTS idx_event_media_fetch_log_source ON event_media_fetch_log(source,created_at);
'''),
    ('20260921_customer_analytics_rules','''
-- Tenant-safe customer intrusion/line-crossing rule storage (2026-09-21,
-- see docs/intrusion-line-crossing-customer-ui-gap.md and customer_
-- analytics_rules.py's own module docstring for the full trace). The
-- ONLY pre-existing rule storage for this feature -- analytics_rules.json
-- (main.py's ANALYTICS_RULES_FILE), used by both the admin-only rule
-- builder and People Counting's own worker -- has no customer_id/site_id/
-- appliance_id at all and addresses a rule solely by a bare integer
-- camera number, unique only within one appliance. This table is a
-- SEPARATE, additive, real database store with full tenant scoping -- it
-- does not read from, write to, or replace analytics_rules.json, and
-- nothing in this migration touches People Counting's existing worker
-- or that file. appliance_id is nullable (not NOT NULL) to match
-- cameras.appliance_id's own nullability -- a camera can exist with no
-- appliance_id yet, and a rule save must not 500 on that case.
CREATE TABLE IF NOT EXISTS customer_analytics_rules(
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    appliance_id TEXT,
    camera_id TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    name TEXT NOT NULL,
    direction TEXT,
    geometry_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(site_id) REFERENCES sites(id),
    FOREIGN KEY(appliance_id) REFERENCES appliances(id),
    FOREIGN KEY(camera_id) REFERENCES cameras(id)
);
CREATE INDEX IF NOT EXISTS idx_customer_analytics_rules_customer ON customer_analytics_rules(customer_id);
CREATE INDEX IF NOT EXISTS idx_customer_analytics_rules_camera ON customer_analytics_rules(camera_id);
'''),
    # AAC Voice Call, Phase 1 foundation (2026-09-23): residential
    # front-door voice/video interaction. Two tables, deliberately
    # separate concerns:
    #
    # aac_voice_call_entrance_cameras is the explicit opt-in list --
    # "only cameras explicitly configured as an AAC Voice Call entrance
    # camera should participate" is the product requirement, so
    # membership in this table (not a column bolted onto `cameras`,
    # which every other camera-scoped feature already crowds) is the
    # single source of truth a trigger checks before anything else
    # happens. Same one-row-per-camera-per-concern convention as
    # customer_camera_permissions/customer_analytics_rules -- never a
    # hardcoded camera list, never inferred from camera_number/count.
    #
    # aac_voice_call_events is the event/call record itself -- one row
    # per visitor interaction, covering every field the product spec
    # asks for: the analytics trigger it came from (trigger_detection_
    # event_id, nullable FK into the existing detection_events pipeline
    # -- nullable because a simulated/manually-tested trigger has none),
    # the interpreted visitor intent and its confidence, the transcript
    # text if the audio pipeline provided one, a thumbnail reference
    # (reusing detection_event_media's own s3_key convention rather
    # than inventing a new media-reference shape), the notification
    # this event fanned out into (notification_id, nullable until the
    # notification step actually runs), and the full answered/call-
    # timing lifecycle. `state` is a small enum tracked in application
    # code (triggered -> notified -> answered|missed|dismissed ->
    # ended), not a DB CHECK constraint, matching this codebase's own
    # established convention (customer_talk_sessions.state,
    # detection_event_media's own status column) of enforcing enum
    # values in Python rather than at the schema layer.
    #
    # Audit/event history is deliberately NOT a third table here --
    # partner_db.audit() (the audit_logs table) already exists
    # precisely for "every state transition of a customer-scoped
    # action, who did it, when, with what details" and is already used
    # by door_access.py/facial_recognition_ui.py for exactly this kind
    # of trail; aac_voice_call.py calls it at every transition instead
    # of duplicating a parallel history table.
    ('20260923_aac_voice_call','''
CREATE TABLE IF NOT EXISTS aac_voice_call_entrance_cameras(
    camera_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    configured_at TEXT NOT NULL,
    configured_by TEXT,
    FOREIGN KEY(camera_id) REFERENCES cameras(id),
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE INDEX IF NOT EXISTS idx_aac_voice_call_entrance_cameras_customer ON aac_voice_call_entrance_cameras(customer_id);

CREATE TABLE IF NOT EXISTS aac_voice_call_events(
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'triggered',
    trigger_detection_event_id TEXT,
    transcript_text TEXT,
    intent TEXT,
    intent_confidence REAL,
    thumbnail_s3_key TEXT,
    notification_id TEXT,
    answered INTEGER NOT NULL DEFAULT 0,
    answered_at TEXT,
    answered_by_user_id TEXT,
    call_started_at TEXT,
    call_ended_at TEXT,
    event_timestamp TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(site_id) REFERENCES sites(id),
    FOREIGN KEY(camera_id) REFERENCES cameras(id),
    FOREIGN KEY(trigger_detection_event_id) REFERENCES detection_events(id),
    FOREIGN KEY(notification_id) REFERENCES notifications(id),
    FOREIGN KEY(answered_by_user_id) REFERENCES partner_users(id)
);
CREATE INDEX IF NOT EXISTS idx_aac_voice_call_events_customer ON aac_voice_call_events(customer_id,created_at);
CREATE INDEX IF NOT EXISTS idx_aac_voice_call_events_camera ON aac_voice_call_events(camera_id,created_at);
'''),
    # platform_owner.py (2026-09-23): recovery infrastructure for the
    # existing identity_grants(role='administrator', scope_type='global')
    # tier -- see that module's own docstring for the full design. None of
    # these three tables grant access on their own; they only ever let an
    # already-provisioned global-grant holder AUTHENTICATE (prove it's
    # really them) through a second factor, a backup code, or -- only via
    # direct server/shell access, never a web request -- an audited
    # break-glass token. Every one of them is fully inert for every other
    # role in this system; a partner/customer account with no global grant
    # gets nothing from any row existing here.
    ('20260923_platform_owner_recovery','''
CREATE TABLE IF NOT EXISTS platform_owner_mfa(
    user_id TEXT PRIMARY KEY,
    secret_base32 TEXT NOT NULL,
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES partner_users(id)
);
CREATE TABLE IF NOT EXISTS platform_owner_recovery_codes(
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES partner_users(id)
);
CREATE INDEX IF NOT EXISTS idx_platform_owner_recovery_codes_user ON platform_owner_recovery_codes(user_id);
CREATE TABLE IF NOT EXISTS break_glass_tokens(
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    FOREIGN KEY(user_id) REFERENCES partner_users(id)
);
CREATE INDEX IF NOT EXISTS idx_break_glass_tokens_user ON break_glass_tokens(user_id);
CREATE TABLE IF NOT EXISTS pending_mfa_logins(
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL,
    authorization_version_at_login INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    FOREIGN KEY(user_id) REFERENCES partner_users(id)
);
'''),
    # AAC Voice Call -- owner-approved door access (2026-09-23). Fills in
    # request_door_unlock()'s own prepared-but-unimplemented Phase 5
    # interface with a real, explicitly human-confirmed unlock action --
    # never a second door-control mechanism: authorization and dispatch
    # both go through the exact same door_access._authorized_door_camera()/
    # relay_control.get_provider().trigger() a manual Live-page unlock
    # already uses (see aac_voice_call_door.py's own module docstring).
    #
    # aac_voice_call_unlock_confirmations is the server-enforced two-step
    # confirmation gate the product spec requires ("require a clear
    # confirmation before issuing the real unlock command") -- mirrors
    # break_glass_tokens' own shape exactly (token_hash, expires_at,
    # used_at), the same short-lived, single-use, hashed-at-rest pattern
    # already independently reviewed earlier tonight, including its
    # atomic UPDATE...WHERE used_at IS NULL claim discipline (the same
    # review found and fixed a real TOCTOU race in that table's own
    # sibling recovery-code/break-glass consumption -- this table's own
    # consumption function is written with that same atomic claim from
    # the start, not retrofitted after an incident). event_id/camera_id/
    # requested_by_user_id are captured at REQUEST time and re-checked
    # against the event's live state and the approving user's live
    # can_unlock permission again at CONFIRM time -- a token proves "this
    # specific request was made", never substitutes for re-verifying
    # authorization.
    ('20260923_aac_voice_call_door_unlock','''
ALTER TABLE door_access_events ADD COLUMN aac_voice_call_event_id TEXT REFERENCES aac_voice_call_events(id);
CREATE TABLE IF NOT EXISTS aac_voice_call_unlock_confirmations(
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    requested_by_user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    FOREIGN KEY(event_id) REFERENCES aac_voice_call_events(id),
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(camera_id) REFERENCES cameras(id),
    FOREIGN KEY(requested_by_user_id) REFERENCES partner_users(id)
);
CREATE INDEX IF NOT EXISTS idx_aac_voice_call_unlock_confirmations_event ON aac_voice_call_unlock_confirmations(event_id,created_at);
'''),
    # AAC Voice Call -- proactive visitor interaction (2026-09-23). A
    # person detected on an entrance camera should be greeted, notified
    # about, and listened to WITHOUT a customer having to simulate/
    # trigger anything themselves (Phase 1's own "owed" appliance-side
    # wiring, per aac_voice_call.py's own module docstring). This
    # migration adds only additive columns/tables -- no existing column,
    # state, or guard changes shape, so every Phase 1/PR-16 code path
    # (trigger_visitor_event, mark_answered, mark_dismissed, end_call,
    # the door-unlock flow) is untouched and keeps working exactly as
    # before; the new proactive flow layers greeted_at/listening_*/
    # escalated_at timestamps on top of the EXISTING triggered/notified/
    # answered/dismissed/ended state machine rather than adding new
    # states, to keep every already-reviewed state guard correct as-is.
    #
    # aac_voice_call_greeting_cooldowns is the debounce/cooldown table:
    # one row per camera, claimed with the same atomic
    # UPDATE...WHERE-rowcount discipline aac_voice_call_unlock_
    # confirmations above already uses for its own single-use claim, so
    # one person standing at the door cannot re-trigger a flood of
    # repeated greetings/notifications.
    #
    # aac_voice_call_site_greetings holds the per-site DEFAULT greeting
    # text an owner can configure; aac_voice_call_entrance_cameras'
    # new greeting_text column is the per-CAMERA override that wins over
    # the site default when set (see aac_voice_call_events.
    # resolve_greeting_text()'s own precedence: camera > site > a fixed
    # built-in fallback string -- never an unconfigured/blank greeting).
    ('20260923_aac_voice_call_proactive_greeting','''
ALTER TABLE aac_voice_call_entrance_cameras ADD COLUMN greeting_text TEXT;
CREATE TABLE IF NOT EXISTS aac_voice_call_site_greetings(
    customer_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    greeting_text TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT,
    PRIMARY KEY(customer_id,site_id),
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(site_id) REFERENCES sites(id)
);
CREATE TABLE IF NOT EXISTS aac_voice_call_greeting_cooldowns(
    camera_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    last_triggered_at TEXT NOT NULL,
    FOREIGN KEY(camera_id) REFERENCES cameras(id),
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);
ALTER TABLE aac_voice_call_events ADD COLUMN greeted_at TEXT;
ALTER TABLE aac_voice_call_events ADD COLUMN greeting_text_used TEXT;
ALTER TABLE aac_voice_call_events ADD COLUMN listening_opened_at TEXT;
ALTER TABLE aac_voice_call_events ADD COLUMN listening_closed_at TEXT;
ALTER TABLE aac_voice_call_events ADD COLUMN escalated_at TEXT;
ALTER TABLE aac_voice_call_events ADD COLUMN utterance_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE aac_voice_call_events ADD COLUMN trigger_source TEXT NOT NULL DEFAULT 'simulated';
'''),
    # AAC Voice Call cloud/edge split (2026-09-24): an edge person
    # detection reaches the cloud as a detection_events row (the existing
    # analytics-sync channel), and the cloud creates the ONE authoritative
    # aac_voice_call_events row for it (aac_voice_call.ingest_edge_visitor_
    # event()). This index is what makes that ingestion idempotent under
    # retries/replays: one detection can never become two Voice Call
    # events. Partial (NOT NULL only), so simulated/customer-triggered
    # events -- which carry no trigger_detection_event_id -- are unaffected.
    ('20260924_aac_voice_call_edge_trigger','''
CREATE UNIQUE INDEX IF NOT EXISTS idx_aac_voice_call_events_trigger_detection ON aac_voice_call_events(trigger_detection_event_id) WHERE trigger_detection_event_id IS NOT NULL;
'''),
    # Live relay viewer demand (2026-09-24): last_seen_at is the relay
    # viewer's heartbeat -- refreshed (throttled) by every relay playlist
    # fetch in live_playlist.py. live_relay_idle_sweep.py treats a camera
    # as still wanting relay upload only while a live session was either
    # just started or has fetched the relay playlist recently, so a
    # crashed/closed/P2P-connected viewer no longer holds the relay open.
    ('20260924_live_view_session_relay_heartbeat','''
ALTER TABLE live_view_sessions ADD COLUMN last_seen_at TEXT;
'''),
]


def apply_migrations():
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL)')
        applied={item['version'] for item in db.execute('SELECT version FROM schema_migrations').fetchall()}
        for version,script in MIGRATIONS:
            if version in applied: continue
            for statement in [item.strip() for item in script.split(';') if item.strip()]: db.execute(statement)
            db.execute('INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)',(version,datetime.now().isoformat())); logger.info('Applied database migration %s',version)

        # These additive columns are checked independently so both existing SQLite
        # installations and future PostgreSQL deployments receive the same schema.
        from database_backend import backend
        user_columns=({item['name'] for item in db.execute('PRAGMA table_info(partner_users)').fetchall()}
                      if backend()=='sqlite' else
                      {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='partner_users'").fetchall()})
        for name,definition in (
            ('account_status',"TEXT NOT NULL DEFAULT 'active'"),
            ('must_change_password','INTEGER NOT NULL DEFAULT 0'),
            ('terms_accepted_at','TEXT'),
            # Per-camera user permissions: 'selected' (default, fail-closed --
            # see camera_access.py's DEFAULT_ACCESS_MODE/is_camera_authorized())
            # or 'all' (every camera under this user's own customer_id).
            ('camera_access_mode',"TEXT NOT NULL DEFAULT 'selected'"),
        ):
            if name not in user_columns: db.execute(f'ALTER TABLE partner_users ADD COLUMN {name} {definition}')
        permission_columns=({item['name'] for item in db.execute('PRAGMA table_info(customer_camera_permissions)').fetchall()}
                            if backend()=='sqlite' else
                            {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='customer_camera_permissions'").fetchall()})
        # can_unlock (2026-09-17): the "appropriate access permissions"
        # the Face Access requirements call for -- same shape and same
        # fail-closed default (0) as can_settings/can_talk. A customer_
        # viewer with no explicit grant can see a door camera's live tile
        # but never gets the Unlock Door button; a customer_owner's own
        # unlock route below still separately re-checks this at the
        # moment the command is executed, never trusting a cached value.
        for name,definition in (('can_alerts','INTEGER NOT NULL DEFAULT 1'),('can_settings','INTEGER NOT NULL DEFAULT 0'),('can_talk','INTEGER NOT NULL DEFAULT 0'),('can_unlock','INTEGER NOT NULL DEFAULT 0')):
            if name not in permission_columns: db.execute(f'ALTER TABLE customer_camera_permissions ADD COLUMN {name} {definition}')

        camera_columns=({item['name'] for item in db.execute('PRAGMA table_info(cameras)').fetchall()}
                        if backend()=='sqlite' else
                        {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='cameras'").fetchall()})
        if 'camera_number' not in camera_columns: db.execute('ALTER TABLE cameras ADD COLUMN camera_number INTEGER')
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_cameras_appliance_camera_number ON cameras(appliance_id,camera_number) WHERE camera_number IS NOT NULL')
        # talk_down_supported is a tri-state, NOT a plain boolean: NULL means
        # "never probed / capability unverified" (the honest default for
        # every camera until real ONVIF discovery reports otherwise), 0
        # means "probed, confirmed unsupported", 1 means "probed, confirmed
        # supported". The frontend mic button must render differently for
        # NULL vs 0 (same disabled state, different tooltip) -- collapsing
        # this to a boolean would make "never checked" indistinguishable
        # from "checked and it doesn't work".
        if 'talk_down_supported' not in camera_columns: db.execute('ALTER TABLE cameras ADD COLUMN talk_down_supported INTEGER')
        if 'talk_down_metadata' not in camera_columns: db.execute('ALTER TABLE cameras ADD COLUMN talk_down_metadata TEXT')
        if 'talk_down_verified_at' not in camera_columns: db.execute('ALTER TABLE cameras ADD COLUMN talk_down_verified_at TEXT')

        # 2026-09-16: AAC Facial Recognition / Face Access are one connected
        # feature -- identity match, the authorization decision (facial_rules
        # + relay_control.rule_applies()), and the access-control command
        # (relay_control.build_request()/RelayProvider.trigger()) were all
        # already correctly separated and unit-tested, but the outcome of
        # that authorization decision was never persisted anywhere -- only
        # returned in-memory from evaluate_access_rules() and dropped by
        # save_yolo_events()'s hook. NULL means no access-control evaluation
        # ran for this match at all (the common case today, since
        # ANYAICAM_FACIAL_ACCESS_CONTROL_ENABLED defaults to false); '[]'
        # means it ran and no facial_rules row applied; a non-empty JSON
        # array is one entry per rule that applied, each carrying rule_id,
        # channel, activated, dry_run, and suppressed_reason -- see
        # relay_control.RelayResult and facial_events.evaluate_access_rules().
        facial_events_columns=({item['name'] for item in db.execute('PRAGMA table_info(facial_events)').fetchall()}
                               if backend()=='sqlite' else
                               {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='facial_events'").fetchall()})
        if 'access_outcomes_json' not in facial_events_columns: db.execute('ALTER TABLE facial_events ADD COLUMN access_outcomes_json TEXT')
        # cloud_recording_mode has NO hidden default by design: NULL means
        # "not explicitly set for this camera" and every consumer (the
        # GET /api/appliance/configuration route, and the appliance's own
        # recording_uploader.py) must treat NULL exactly like 'continuous'
        # -- upload everything, today's unchanged behavior -- never like
        # 'motion'. Only an explicit 'motion' value (set via
        # POST /api/admin/cameras/{camera_id}/cloud-recording-mode) ever
        # turns on upload-time motion-gating for that camera. This is the
        # only plan-mode signal in the system; recording_mode on
        # AnalyticsRuleModel/quote objects is a separate, unrelated,
        # sales-quote-only field this column does not read from or write to.
        # Restored from commit a7300ee (motion-gated-cloud-upload-cloud-
        # side-20260822 branch), which added the appliance_cloud.py route
        # and this exact idempotent column-add together, but only the
        # route half reached this branch during an earlier consolidation
        # -- the migration hunk was dropped, leaving the route's own SELECT
        # and UPDATE statements referencing a column that never existed on
        # any database created from this branch (confirmed: firing on
        # every Samsung appliance-agent heartbeat as "no such column").
        if 'cloud_recording_mode' not in camera_columns: db.execute('ALTER TABLE cameras ADD COLUMN cloud_recording_mode TEXT')
        # people_counting_enabled follows the exact same no-hidden-default
        # convention as cloud_recording_mode above: NULL/0 means "not
        # entitled/not configured for People Counting" and every consumer
        # (GET /api/appliance/configuration, and the appliance's own
        # people-counting worker) must treat that as "do not run People
        # Counting on this camera" -- the same appliance-wide detection
        # loop this camera already runs for plain person/vehicle detection
        # is completely unaffected either way. Only an explicit 1 (set via
        # POST /api/admin/cameras/{camera_id}/people-counting) turns this
        # on for that camera. This is the first camera-level analytics
        # entitlement flag in the system; see the accompanying report for
        # how the other named analytics (LPR, PPE, etc.) can adopt this
        # same per-camera column pattern later instead of remaining
        # appliance-wide-only toggles.
        # Restored from commit 87fdfe7 (people-counting-cloud-entitlement-
        # 20260824 branch) for the same reason as cloud_recording_mode
        # directly above -- same dropped-migration-hunk gap.
        if 'people_counting_enabled' not in camera_columns: db.execute('ALTER TABLE cameras ADD COLUMN people_counting_enabled INTEGER')
        # 2026-09-16: the "other named analytics" this column's own comment
        # above pointed at, adopting the exact same per-camera pattern.
        # Closes a real, verified gap: RDM's own customer-facing entitlement
        # toggle (customer_analytics_panel.assign_entitlement()/
        # remove_entitlement(), writing camera_analytics_entitlements) never
        # reached the appliance at all for any of the 4 real analytics --
        # confirmed live that even people_counting_enabled itself, despite
        # having this real column and a real edge-side read
        # (main.py's people_counting_worker()), was never actually populated
        # into recording_uploader.py's own in-memory camera map, so it read
        # as permanently unset regardless of entitlement state. Fixed
        # alongside this migration: assign_entitlement()/remove_entitlement()
        # now write all 4 of these columns, GET /api/appliance/configuration
        # exposes all 4, edge_camera_sync.py syncs all 4 into the local
        # cameras table, and recording_uploader._refresh_camera_map() now
        # actually includes all 4 in the map lpr.is_camera_enabled()/
        # ppe.is_camera_enabled()/smart_motion's own caller in main.py read.
        if 'smart_motion_enabled' not in camera_columns: db.execute('ALTER TABLE cameras ADD COLUMN smart_motion_enabled INTEGER')
        if 'lpr_enabled' not in camera_columns: db.execute('ALTER TABLE cameras ADD COLUMN lpr_enabled INTEGER')
        if 'ppe_enabled' not in camera_columns: db.execute('ALTER TABLE cameras ADD COLUMN ppe_enabled INTEGER')
        # Face Access -- Door/Relay Control (2026-09-17): fail-closed like
        # every sibling *_enabled column above -- NULL/0 means this camera
        # is not a door and must never show the live-tile Unlock button or
        # be eligible for automatic-entry evaluation, regardless of
        # facial_recognition/ppe_enabled etc. The door's own display name
        # is deliberately NOT a separate column: it's this camera's
        # existing `name` (see the requirements doc's own example --
        # "Camera 1 is renamed Front Door"), never a second, potentially-
        # diverging copy of it. door_relay_pulse_ms is this door's default
        # activation duration for a MANUAL Unlock press; an AUTOMATIC
        # (facial_rules-driven) unlock uses that rule's own pulse_ms
        # instead, unchanged.
        if 'door_access_enabled' not in camera_columns: db.execute('ALTER TABLE cameras ADD COLUMN door_access_enabled INTEGER')
        if 'door_relay_channel' not in camera_columns: db.execute('ALTER TABLE cameras ADD COLUMN door_relay_channel INTEGER')
        if 'door_relay_pulse_ms' not in camera_columns: db.execute('ALTER TABLE cameras ADD COLUMN door_relay_pulse_ms INTEGER')
        # Local Event-mode recording (2026-09-20): same no-hidden-default
        # convention as cloud_recording_mode above, deliberately kept as a
        # completely separate column/concept -- cloud_recording_mode gates
        # whether an already-recorded LOCAL file gets uploaded to cloud;
        # local_recording_mode gates whether that file gets recorded to
        # local disk in the first place. NULL/'continuous' (every existing
        # row) must be read by every consumer (main.py's process_supervisor)
        # exactly like today's unconditional 5-minute continuous segmenter,
        # never like 'event'. Only an explicit 'event' value (set via
        # POST /api/admin/cameras/{camera_id}/local-recording-mode) switches
        # a camera to motion/activity-triggered recording -- see
        # local_recording_policy.py for the actual decision logic.
        if 'local_recording_mode' not in camera_columns: db.execute('ALTER TABLE cameras ADD COLUMN local_recording_mode TEXT')
        if 'local_recording_pre_roll_seconds' not in camera_columns: db.execute('ALTER TABLE cameras ADD COLUMN local_recording_pre_roll_seconds INTEGER')
        if 'local_recording_post_roll_seconds' not in camera_columns: db.execute('ALTER TABLE cameras ADD COLUMN local_recording_post_roll_seconds INTEGER')
        if 'local_recording_merge_gap_seconds' not in camera_columns: db.execute('ALTER TABLE cameras ADD COLUMN local_recording_merge_gap_seconds INTEGER')
        if 'local_recording_max_event_seconds' not in camera_columns: db.execute('ALTER TABLE cameras ADD COLUMN local_recording_max_event_seconds INTEGER')
        # One-time-per-row backfill, safe to run on every startup: a
        # customer who already toggled an analytic ON via RDM (writing
        # camera_analytics_entitlements) before this fix existed must not
        # suddenly read as "not entitled" on the appliance the moment
        # these columns first appear. Scoped to `IS NULL` only -- a row
        # this session's own assign_entitlement()/remove_entitlement()
        # fix has already explicitly set to 0 (a real "turned off") is
        # never touched or resurrected by this backfill.
        for analytic_key, column in (
            ('smart_motion', 'smart_motion_enabled'),
            ('people_counting', 'people_counting_enabled'),
            ('lpr', 'lpr_enabled'),
            ('ppe', 'ppe_enabled'),
        ):
            entitled_camera_ids = [
                row['camera_id'] for row in db.execute(
                    "SELECT camera_id FROM camera_analytics_entitlements WHERE analytic_key=? AND status='active'",
                    (analytic_key,),
                ).fetchall()
            ]
            for entitled_camera_id in entitled_camera_ids:
                db.execute(f"UPDATE cameras SET {column}=1 WHERE {column} IS NULL AND id=?", (entitled_camera_id,))
        # Phase 3 (dynamic camera provisioning): device_key is the ONVIF
        # endpoint reference UUID -- stable across reboot/DHCP/IP changes,
        # unlike ip_address -- and is how rediscovering an already-
        # provisioned camera updates its existing row instead of creating a
        # duplicate. ip_address/onvif_endpoint/manufacturer/model are non-
        # secret device metadata; camera credentials never live in this
        # table -- see camera_credentials (encrypted) instead.
        for name,definition in (('device_key','TEXT'),('ip_address','TEXT'),('onvif_endpoint','TEXT'),('manufacturer','TEXT'),('model','TEXT')):
            if name not in camera_columns: db.execute(f'ALTER TABLE cameras ADD COLUMN {name} {definition}')
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_cameras_customer_device_key ON cameras(customer_id,device_key) WHERE device_key IS NOT NULL')
        # Restored from production's 20260824_camera_discovery migration
        # (see that migration's own comment below for the full story):
        # production's appliance-scoped, non-unique index on the same
        # column, created here instead of in that migration's raw SQL
        # so it -- and the device_key column add it depends on -- never
        # collide with this same branch's own, separately pre-existing
        # device_key handling directly above. Both this index and the
        # customer-scoped unique one above it coexist by design; see
        # the migration's own comment for why that divergence is left
        # unresolved for now.
        db.execute('CREATE INDEX IF NOT EXISTS idx_cameras_appliance_device_key ON cameras(appliance_id,device_key)')

        # Billing authority for camera-level analytics entitlements:
        # how many camera-seats of this analytic the customer/site
        # actually purchased. assign_entitlement() in
        # customer_analytics_panel.py must refuse to enable an analytic
        # on more cameras than licensed_quantity allows -- subscription
        # = what they bought, camera entitlement = where they use it,
        # and the latter can never exceed the former.
        subscription_columns=({item['name'] for item in db.execute('PRAGMA table_info(analytics_subscriptions)').fetchall()}
                              if backend()=='sqlite' else
                              {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='analytics_subscriptions'").fetchall()})
        if 'licensed_quantity' not in subscription_columns: db.execute('ALTER TABLE analytics_subscriptions ADD COLUMN licensed_quantity INTEGER NOT NULL DEFAULT 1')

        # Stripe TEST analytics wiring: analytics_subscriptions is the
        # pre-existing manual/partner-entered licensing table (see
        # customer_entitlements.py's module docstring, finding #3) --
        # these columns let analytics_entitlements.py's Stripe webhook
        # bridge write to and reconcile against this SAME table (never a
        # second analytics/licensing architecture) with the same
        # traceability customer_entitlements/hardware_orders already have
        # for their own Stripe-driven rows.
        for name,definition in (
            ('stripe_customer_id','TEXT'),
            ('stripe_subscription_id','TEXT'),
            ('stripe_price_id','TEXT'),
            ('updated_at','TEXT'),
        ):
            if name not in subscription_columns: db.execute(f'ALTER TABLE analytics_subscriptions ADD COLUMN {name} {definition}')

        appliance_columns=({item['name'] for item in db.execute('PRAGMA table_info(appliances)').fetchall()}
                           if backend()=='sqlite' else
                           {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='appliances'").fetchall()})
        if 'live_relay_pilot' not in appliance_columns: db.execute('ALTER TABLE appliances ADD COLUMN live_relay_pilot INTEGER NOT NULL DEFAULT 0')

        # WireGuard direct remote connectivity (docs/wireguard-remote-
        # connectivity-plan.md Sec 14): the RDM-visible tunnel-health
        # surface, populated via the existing heartbeat channel exactly
        # like storage_state above it -- not a new sync mechanism. NULL
        # (never reported) is a real, distinct state from 'disabled'
        # (reported, no identity enrolled): an appliance running agent
        # code from before this column's own feature existed simply
        # never sends the field at all, and COALESCE in heartbeat()'s
        # own UPDATE leaves a prior real value alone on any heartbeat
        # that omits it.
        if 'wireguard_status' not in appliance_columns: db.execute('ALTER TABLE appliances ADD COLUMN wireguard_status TEXT')
        if 'wireguard_last_handshake_at' not in appliance_columns: db.execute('ALTER TABLE appliances ADD COLUMN wireguard_last_handshake_at TEXT')

        # Provisioning Phase 3: `product` is a stable category string
        # (e.g. always "camera_slots") so an upgrade/downgrade between
        # fixed tiers updates the SAME row (customer_id,product) rather
        # than fragmenting into one row per tier -- see customer_
        # entitlements.py's module docstring. stripe_price_id records
        # exactly which server-verified tier is currently active, for
        # traceability/support/display, independent of that stable
        # product key.
        entitlement_columns=({item['name'] for item in db.execute('PRAGMA table_info(customer_entitlements)').fetchall()}
                             if backend()=='sqlite' else
                             {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='customer_entitlements'").fetchall()})
        if 'stripe_price_id' not in entitlement_columns: db.execute('ALTER TABLE customer_entitlements ADD COLUMN stripe_price_id TEXT')

        pending_link_columns=({item['name'] for item in db.execute('PRAGMA table_info(pending_customer_links)').fetchall()}
                              if backend()=='sqlite' else
                              {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='pending_customer_links'").fetchall()})
        if 'stripe_price_id' not in pending_link_columns: db.execute('ALTER TABLE pending_customer_links ADD COLUMN stripe_price_id TEXT')

        # RDM4 heartbeat restart-detection (appliance_cloud.py's heartbeat()):
        # a restart is inferred from uptime_seconds dropping, never self-
        # reported, and this counter is what commands.py's diagnostics()
        # comment already documents as tracked "through the separate,
        # existing RDM3 heartbeat/upload-worker pipeline" -- confirmed by
        # heartbeat() unconditionally executing
        # 'UPDATE appliances SET restart_count=...' the moment it detects
        # one, which raised sqlite3.OperationalError: no such column on
        # any database that only ever ran the migrations above this line
        # (live_relay_pilot's own release never added it). Live on real
        # Samsung hardware: every restart-shaped heartbeat -- including an
        # offline-queued heartbeat replayed after connectivity is restored
        # -- 500'd here instead of registering the restart and moving on.
        if 'restart_count' not in appliance_columns: db.execute('ALTER TABLE appliances ADD COLUMN restart_count INTEGER NOT NULL DEFAULT 0')

        # product_mode auto-apply (2026-09-21, see docs/product-mode-
        # local-hybrid-2026-09-21.md and product_mode.py's module
        # docstring): the Local/Hybrid mode this appliance's customer had
        # the LAST time GET /api/appliance/configuration computed and
        # reported it, tracked per-appliance (a customer can have more
        # than one appliance, each independently transitioning). NULL for
        # an appliance that has never had a real (non-empty)
        # product_mode reported yet. appliance_configuration() compares
        # this against a fresh product_mode_for_customer() call on every
        # poll; only a genuine change queues a restart_vms command and
        # updates this column -- an unchanged mode across any number of
        # polls does neither, which is what keeps this a one-time
        # transition trigger instead of a restart loop.
        if 'last_reported_product_mode' not in appliance_columns: db.execute('ALTER TABLE appliances ADD COLUMN last_reported_product_mode TEXT')

        # Appliance identity contract (see appliance_identity.py):
        # authorization_version_at_login records the identity's
        # authorization_version at the moment this session was
        # established, so a later revocation/role-change/re-grant can be
        # detected by comparing integers -- see appliance_identity.
        # sessions_to_revoke(). NULL for a session established before
        # this column existed, or for a legacy-side (admin@local) login
        # this concept never applies to.
        session_columns=({item['name'] for item in db.execute('PRAGMA table_info(user_sessions)').fetchall()}
                         if backend()=='sqlite' else
                         {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='user_sessions'").fetchall()})
        if 'authorization_version_at_login' not in session_columns: db.execute('ALTER TABLE user_sessions ADD COLUMN authorization_version_at_login INTEGER')

        # Phase 1 security-hardening checkpoint (see
        # docs/non-interactive-activation-phase1-security-hardening-report.md):
        # claim_proof_plaintext (the original Phase 1 column) is left in
        # place -- never dropped, per this file's own established
        # convention -- but new code never writes to it again. These
        # three replace it:
        #   claim_proof_encrypted: the confirmed claim_proof, encrypted
        #     at rest (appliance_protocol.encrypt_claim_flow_secret())
        #     instead of stored raw, so a DB-file-level read no longer
        #     hands out a live, redeemable secret the way a plaintext
        #     column would.
        #   completed_credential_encrypted / credential_recovery_expires_at:
        #     the ONE-TIME credential claim/complete mints, held
        #     encrypted for a short recovery window so a retry after a
        #     lost response can recover the SAME credential instead of
        #     permanently losing enrollment or minting a second one.
        claim_columns=({item['name'] for item in db.execute('PRAGMA table_info(appliance_claims)').fetchall()}
                       if backend()=='sqlite' else
                       {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='appliance_claims'").fetchall()})
        for name,definition in (
            ('claim_proof_encrypted','TEXT'),
            ('completed_credential_encrypted','TEXT'),
            ('credential_recovery_expires_at','TEXT'),
        ):
            if name not in claim_columns: db.execute(f'ALTER TABLE appliance_claims ADD COLUMN {name} {definition}')

        # Smart Motion shared-media authorization (2026-09-14 Phase A --
        # see appliance_cloud.py's analytics_event_available() and the
        # new .../media/shared route). A CLOUD id -> CLOUD id foreign
        # key, resolved exactly once at ingestion time from an
        # appliance-submitted LOCAL id and frozen from then on -- never
        # trusted again from any later request. NULL for every event
        # except a real smart_motion one whose claimed Motion parent has
        # already been independently resolved (same camera, same
        # authenticated appliance, parent event_type='motion'). A
        # smart_motion event with this column still NULL is deliberately
        # unresolved and permanently ineligible for the shared-media
        # route until a later resync succeeds -- never inferred from
        # nearby timestamps or filenames, and never silently retrofitted
        # onto a legacy row.
        detection_event_columns=({item['name'] for item in db.execute('PRAGMA table_info(detection_events)').fetchall()}
                                 if backend()=='sqlite' else
                                 {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='detection_events'").fetchall()})
        if 'parent_detection_event_id' not in detection_event_columns: db.execute('ALTER TABLE detection_events ADD COLUMN parent_detection_event_id TEXT REFERENCES detection_events(id)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_detection_events_parent ON detection_events(parent_detection_event_id)')

        # Provenance for a shared (non-primary) detection_event_media
        # row: NULL for a root row (the base Motion event's own real
        # upload), set to that root row's own id for a child row sharing
        # its bytes. This is the reference/owner distinction the
        # retention sweep and daily-usage accounting both depend on --
        # a non-null source_media_id row must never independently
        # trigger an S3 delete and must never be counted as new physical
        # footage (see recording_retention_sweep.py and
        # event_media_policy.py). s3_key itself deliberately carries no
        # uniqueness constraint (confirmed against the live schema) --
        # two independently-owned rows safely referencing one immutable
        # object was already a supported shape before this column
        # existed; this just makes the relationship explicit and
        # queryable instead of only inferable by matching key strings.
        detection_event_media_columns=({item['name'] for item in db.execute('PRAGMA table_info(detection_event_media)').fetchall()}
                                       if backend()=='sqlite' else
                                       {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='detection_event_media'").fetchall()})
        if 'source_media_id' not in detection_event_media_columns: db.execute('ALTER TABLE detection_event_media ADD COLUMN source_media_id TEXT REFERENCES detection_event_media(id)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_detection_event_media_source ON detection_event_media(source_media_id)')

        # WireGuard direct event-clip fetch, prepared (2026-09-18):
        # the appliance's own local /recordings/... URL for this clip,
        # exactly the same string event_media_uploader.py's
        # _safe_recording_url() already computes and validates locally --
        # never re-derived from s3_key, whose date-based prefix does not
        # preserve the original local subfolder. NULL for every
        # already-uploaded clip (this column did not exist when they were
        # registered) and for every future upload from an appliance still
        # running old agent code that never sends this new, optional
        # field -- both are simply ineligible for the direct/WireGuard
        # fetch path (live_view_wireguard.py's own
        # register_event_media_wireguard_routes() 404s cleanly on NULL),
        # never a broken/unknown reference. Purely additive: no existing
        # row, query, or upload behavior changes.
        if 'local_relative_path' not in detection_event_media_columns: db.execute('ALTER TABLE detection_event_media ADD COLUMN local_relative_path TEXT')

        # WireGuard authenticated media-fetch secret (2026-09-19): the
        # real fix for a live staging finding the same day -- a naive
        # fetch of /recordings/... over the tunnel got redirected to
        # Ryzen's own local admin login page, and the caller wrongly
        # counted that HTTP 200 as a successful video fetch. This column
        # holds one shared HMAC key per appliance (see
        # appliance_media_fetch.py's own module docstring for why this
        # must be the raw value, not a one-way hash like appliance_
        # credentials.credential_hash: the cloud actively signs outgoing
        # requests with it, which requires holding the real key). NULL
        # for every appliance until a separate, later, explicitly-
        # authorized enrollment step provisions a real one -- main.py's
        # event-clip direct-fetch route treats a NULL secret as simply
        # ineligible (falls back to S3), never as an error.
        appliance_wireguard_peers_columns=({item['name'] for item in db.execute('PRAGMA table_info(appliance_wireguard_peers)').fetchall()}
                                          if backend()=='sqlite' else
                                          {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='appliance_wireguard_peers'").fetchall()})
        if 'media_fetch_secret' not in appliance_wireguard_peers_columns: db.execute('ALTER TABLE appliance_wireguard_peers ADD COLUMN media_fetch_secret TEXT')

        # Notification external-delivery reliability (2026-09-16): the
        # real address/number a delivery attempt was actually sent to,
        # captured at send time -- never re-derived from the customer's
        # CURRENT preferences on a later retry, which could have changed
        # (a different email/phone saved, or notifications disabled
        # entirely) since the original attempt. recipient is NULL for
        # every pre-existing in_app delivery row (never sent anywhere,
        # nothing to retry) and for any row created before this column
        # existed -- both are simply ineligible for the retry worker
        # below, not treated as a broken/unknown recipient. attempt is
        # the 1-indexed attempt number for this exact (notification_id,
        # channel) pair, so notification_retry_worker() can find the
        # latest attempt and enforce a bounded retry count without a
        # second lookup table -- every attempt is its own permanent
        # row, which is also exactly the delivery audit/history trail
        # this phase's own requirement asks for.
        notification_delivery_columns=({item['name'] for item in db.execute('PRAGMA table_info(notification_deliveries)').fetchall()}
                                       if backend()=='sqlite' else
                                       {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='notification_deliveries'").fetchall()})
        if 'recipient' not in notification_delivery_columns: db.execute('ALTER TABLE notification_deliveries ADD COLUMN recipient TEXT')
        if 'attempt' not in notification_delivery_columns: db.execute('ALTER TABLE notification_deliveries ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1')
        db.execute('CREATE INDEX IF NOT EXISTS idx_notification_deliveries_notification_channel ON notification_deliveries(notification_id,channel,created_at)')
