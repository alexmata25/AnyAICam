import logging
from datetime import datetime

from database_backend import connect

logger=logging.getLogger('anyaicam.migrations')

MIGRATIONS=[
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
    ('20260815_rdm2_update_history','''
CREATE TABLE IF NOT EXISTS appliance_update_history(appliance_id TEXT NOT NULL,update_id TEXT NOT NULL,from_version TEXT NOT NULL,to_version TEXT NOT NULL,state TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',rollback_from TEXT,duration_seconds REAL NOT NULL DEFAULT 0,created_at TEXT NOT NULL,PRIMARY KEY(appliance_id,update_id,state),FOREIGN KEY(appliance_id) REFERENCES appliances(id));
'''),
    ('20260816_live_relay_idle_tracking','''
CREATE TABLE IF NOT EXISTS live_relay_idle_tracking(camera_id TEXT PRIMARY KEY,appliance_id TEXT NOT NULL,idle_since TEXT NOT NULL,stop_queued_at TEXT,FOREIGN KEY(camera_id) REFERENCES cameras(id));
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
        ):
            if name not in user_columns: db.execute(f'ALTER TABLE partner_users ADD COLUMN {name} {definition}')
        permission_columns=({item['name'] for item in db.execute('PRAGMA table_info(customer_camera_permissions)').fetchall()}
                            if backend()=='sqlite' else
                            {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='customer_camera_permissions'").fetchall()})
        for name,definition in (('can_alerts','INTEGER NOT NULL DEFAULT 1'),('can_settings','INTEGER NOT NULL DEFAULT 0')):
            if name not in permission_columns: db.execute(f'ALTER TABLE customer_camera_permissions ADD COLUMN {name} {definition}')

        # Phase 6a (docs/AI_HANDOFF.md Sec 8): nullable, additive camera_number
        # mapping column -- never inferred, only ever set via an explicit
        # technician/customer assignment (see camera_mapping.py). The partial
        # unique index enforces "unique per appliance" (not global, not per
        # customer) and is the authoritative backstop against a race between
        # two concurrent assignments; camera_mapping.assign_camera_number()
        # checks this same invariant first for a clean error message.
        camera_columns=({item['name'] for item in db.execute('PRAGMA table_info(cameras)').fetchall()}
                        if backend()=='sqlite' else
                        {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='cameras'").fetchall()})
        if 'camera_number' not in camera_columns: db.execute('ALTER TABLE cameras ADD COLUMN camera_number INTEGER')
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_cameras_appliance_camera_number ON cameras(appliance_id,camera_number) WHERE camera_number IS NOT NULL')

        # Phase 6f (docs/AI_HANDOFF.md Sec 8): nullable-by-default, fail-closed
        # per-appliance live-relay pilot eligibility. Effective enablement
        # requires BOTH this flag AND the existing global
        # ANYAICAM_LIVE_RELAY_ENABLED kill switch (see appliance_cloud.py's
        # live_relay_session()) -- this column alone never enables anything.
        appliance_columns=({item['name'] for item in db.execute('PRAGMA table_info(appliances)').fetchall()}
                           if backend()=='sqlite' else
                           {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='appliances'").fetchall()})
        if 'live_relay_pilot' not in appliance_columns: db.execute('ALTER TABLE appliances ADD COLUMN live_relay_pilot INTEGER NOT NULL DEFAULT 0')
