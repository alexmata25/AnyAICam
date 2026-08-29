import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from database_backend import backend,connect as database_connect,target_key as database_target_key

DB_FILE = Path(os.getenv('ANYAICAM_PARTNER_DB', '/app/recordings/partner_portal.db'))
REAL_SOURCE = 'real'
DEMO_SOURCE = 'demo'

ROLE_PERMISSIONS = {
    'administrator': {'*'},
    'partner_owner': {'partner.view','customer.create','customer.view','customer.edit','quote.create','user.invite','appliance.assign','appliance.action','pricing.view','pricing.edit','audit.view'},
    'salesperson': {'partner.view','customer.create','customer.view','customer.edit','quote.create','pricing.view'},
    'technician': {'partner.view','customer.view','customer.edit','appliance.assign','appliance.action'},
    'customer_owner': {'customer.self.view','customer.self.edit','user.invite','appliance.self.link','camera.self.configure'},
    'customer_viewer': {'customer.self.view'},
}


_initialized_targets: set = set()
_initialization_lock = threading.Lock()


@contextmanager
def connection():
    ensure_database_initialized()
    with database_connect() as db: yield db


def ensure_database_initialized() -> None:
    """Run schema initialization for the currently configured database target,
    once per target for the life of this process.

    Uses database_connect() (never connection()) during initialization itself,
    since connection() calls this function first - going through connection()
    here would recurse. A target is marked initialized only after
    initialize_database() returns successfully, so a failed attempt is retried
    on the next call instead of being cached as done. The lock makes concurrent
    callers for a not-yet-initialized target block on one real initialization
    rather than racing to create the schema twice."""
    target = database_target_key()
    if target in _initialized_targets: return
    with _initialization_lock:
        if target in _initialized_targets: return  # another thread may have finished while we waited
        initialize_database()
        _initialized_targets.add(target)


def initialize_database() -> None:
    statements = [
        '''CREATE TABLE IF NOT EXISTS partners(id TEXT PRIMARY KEY,name TEXT NOT NULL,approval_status TEXT NOT NULL DEFAULT 'pending',territory TEXT,tax_information TEXT,payout_details TEXT,support_contacts TEXT,source TEXT NOT NULL DEFAULT 'real',created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS partner_users(id TEXT PRIMARY KEY,partner_id TEXT,email TEXT UNIQUE NOT NULL,name TEXT,role TEXT NOT NULL,password_hash TEXT NOT NULL,approved INTEGER NOT NULL DEFAULT 0,customer_id TEXT,created_at TEXT NOT NULL,FOREIGN KEY(partner_id) REFERENCES partners(id))''',
        '''CREATE TABLE IF NOT EXISTS customers(id TEXT PRIMARY KEY,partner_id TEXT NOT NULL,name TEXT NOT NULL,company TEXT,email TEXT UNIQUE NOT NULL,phone TEXT,status TEXT NOT NULL,trial_status TEXT,billing_status TEXT,source TEXT NOT NULL DEFAULT 'real',created_at TEXT NOT NULL,created_by TEXT,FOREIGN KEY(partner_id) REFERENCES partners(id))''',
        '''CREATE TABLE IF NOT EXISTS sites(id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,name TEXT NOT NULL,address TEXT,site_type TEXT,created_at TEXT NOT NULL,FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE CASCADE)''',
        '''CREATE TABLE IF NOT EXISTS appliances(id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,site_id TEXT NOT NULL,cloud_id TEXT UNIQUE NOT NULL,appliance_type TEXT,serial_number TEXT,software_version TEXT,last_check_in TEXT,online_status TEXT,ip_address TEXT,cpu REAL,memory REAL,disk REAL,camera_capacity INTEGER,activation_token_hash TEXT,activation_token_created_at TEXT,shipping_status TEXT,created_at TEXT NOT NULL,FOREIGN KEY(customer_id) REFERENCES customers(id),FOREIGN KEY(site_id) REFERENCES sites(id))''',
        '''CREATE TABLE IF NOT EXISTS cameras(id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,site_id TEXT NOT NULL,appliance_id TEXT,name TEXT,resolution TEXT,status TEXT,created_at TEXT NOT NULL,FOREIGN KEY(customer_id) REFERENCES customers(id),FOREIGN KEY(site_id) REFERENCES sites(id))''',
        '''CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,resolution TEXT,recording_mode TEXT,retention_days INTEGER,camera_quantity INTEGER,retail_monthly REAL,partner_monthly REAL,monthly_recurring_profit REAL,annual_total REAL,status TEXT,created_at TEXT NOT NULL,FOREIGN KEY(customer_id) REFERENCES customers(id))''',
        '''CREATE TABLE IF NOT EXISTS analytics_subscriptions(id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,site_id TEXT,analytic_key TEXT,status TEXT,monthly_retail REAL,monthly_partner REAL,created_at TEXT NOT NULL,FOREIGN KEY(customer_id) REFERENCES customers(id))''',
        '''CREATE TABLE IF NOT EXISTS quotes(id TEXT PRIMARY KEY,customer_id TEXT,partner_id TEXT,status TEXT,selection_json TEXT,totals_json TEXT,created_at TEXT NOT NULL,created_by TEXT,FOREIGN KEY(customer_id) REFERENCES customers(id))''',
        '''CREATE TABLE IF NOT EXISTS invitations(id TEXT PRIMARY KEY,email TEXT NOT NULL,role TEXT NOT NULL,customer_id TEXT,status TEXT,temporary_password_hash TEXT,email_preview TEXT,expires_at TEXT,created_at TEXT NOT NULL,created_by TEXT)''',
        '''CREATE TABLE IF NOT EXISTS audit_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,actor_email TEXT,actor_role TEXT,action TEXT NOT NULL,entity_type TEXT,entity_id TEXT,details_json TEXT,created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS onboarding_drafts(id TEXT PRIMARY KEY,actor_email TEXT NOT NULL,current_step INTEGER NOT NULL DEFAULT 1,data_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'draft',updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS service_history(id INTEGER PRIMARY KEY AUTOINCREMENT,customer_id TEXT NOT NULL,event TEXT NOT NULL,details TEXT,created_at TEXT NOT NULL,created_by TEXT,FOREIGN KEY(customer_id) REFERENCES customers(id))''',
        '''CREATE TABLE IF NOT EXISTS customer_notes(id INTEGER PRIMARY KEY AUTOINCREMENT,customer_id TEXT NOT NULL,note TEXT NOT NULL,created_at TEXT NOT NULL,created_by TEXT,FOREIGN KEY(customer_id) REFERENCES customers(id))''',
        '''CREATE TABLE IF NOT EXISTS camera_scan_jobs(id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,appliance_id TEXT NOT NULL,status TEXT NOT NULL,progress INTEGER NOT NULL DEFAULT 0,results_json TEXT NOT NULL DEFAULT '[]',message TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,FOREIGN KEY(customer_id) REFERENCES customers(id),FOREIGN KEY(appliance_id) REFERENCES appliances(id))''',
        '''CREATE TABLE IF NOT EXISTS customer_setup_drafts(customer_id TEXT PRIMARY KEY,current_step INTEGER NOT NULL DEFAULT 1,data_json TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL,FOREIGN KEY(customer_id) REFERENCES customers(id))''',
        '''CREATE TABLE IF NOT EXISTS appliance_activation_tokens(id TEXT PRIMARY KEY,appliance_id TEXT NOT NULL,token_hash TEXT NOT NULL,expires_at TEXT NOT NULL,used_at TEXT,revoked_at TEXT,created_at TEXT NOT NULL,created_by TEXT,FOREIGN KEY(appliance_id) REFERENCES appliances(id))''',
        '''CREATE TABLE IF NOT EXISTS appliance_credentials(id TEXT PRIMARY KEY,appliance_id TEXT NOT NULL,credential_hash TEXT NOT NULL,created_at TEXT NOT NULL,last_used_at TEXT,revoked_at TEXT,created_by TEXT,FOREIGN KEY(appliance_id) REFERENCES appliances(id))''',
        '''CREATE TABLE IF NOT EXISTS appliance_request_nonces(appliance_id TEXT NOT NULL,nonce TEXT NOT NULL,request_timestamp INTEGER NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(appliance_id,nonce),FOREIGN KEY(appliance_id) REFERENCES appliances(id))''',
        '''CREATE TABLE IF NOT EXISTS appliance_health_history(id INTEGER PRIMARY KEY AUTOINCREMENT,appliance_id TEXT NOT NULL,status TEXT NOT NULL,cpu REAL,memory REAL,disk_capacity REAL,disk_used REAL,recording_used REAL,uptime_seconds INTEGER,camera_count INTEGER,last_error TEXT,created_at TEXT NOT NULL,FOREIGN KEY(appliance_id) REFERENCES appliances(id))''',
        '''CREATE TABLE IF NOT EXISTS appliance_camera_status(appliance_id TEXT NOT NULL,camera_id TEXT NOT NULL,name TEXT,online INTEGER,recording INTEGER,analytics INTEGER,last_recording_at TEXT,last_error TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(appliance_id,camera_id),FOREIGN KEY(appliance_id) REFERENCES appliances(id))''',
        '''CREATE TABLE IF NOT EXISTS appliance_events(appliance_id TEXT NOT NULL,event_id TEXT NOT NULL,event_type TEXT,camera_id TEXT,event_timestamp TEXT,payload_json TEXT NOT NULL,received_at TEXT NOT NULL,PRIMARY KEY(appliance_id,event_id),FOREIGN KEY(appliance_id) REFERENCES appliances(id))''',
        '''CREATE TABLE IF NOT EXISTS appliance_commands(id TEXT PRIMARY KEY,appliance_id TEXT NOT NULL,command TEXT NOT NULL,payload_json TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,delivered_at TEXT,completed_at TEXT,expires_at TEXT NOT NULL,error TEXT,created_by TEXT,FOREIGN KEY(appliance_id) REFERENCES appliances(id))''',
        # admin_partner_bridge.py: an explicit, revocable link from one
        # Admin Portal user (admin_user_id, the legacy JSON-store user id)
        # to one existing Partner Portal user (partner_user_id) -- never a
        # new partner_users row, never a copied/shared password. See that
        # module's own docstring for the full design and every
        # fail-closed check applied on top of this table at resolution
        # time.
        '''CREATE TABLE IF NOT EXISTS admin_partner_links(admin_user_id TEXT PRIMARY KEY,admin_email TEXT NOT NULL,partner_user_id TEXT NOT NULL,partner_email TEXT NOT NULL,linked_at TEXT NOT NULL,linked_by TEXT NOT NULL,revoked_at TEXT,FOREIGN KEY(partner_user_id) REFERENCES partner_users(id))''',
        # notification_preferences.py: one row per portal user -- Email/
        # SMS contact info + channel toggles + verification timestamps +
        # quiet hours + delivery mode. event_types_json/camera_scope
        # decide WHAT and WHERE; the actual authorized-cameras check
        # happens at save time (see save_preferences()'s own docstring),
        # never trusted from this table alone at read time either.
        #
        # Named customer_notification_channels, NOT notification_
        # preferences: db_migrations.py already defines a *different*
        # table under that exact name (one row per user/customer/site/
        # camera/event_type, with in_app/email/web_push/sms channel
        # booleans -- the admin-managed per-event-type rule set
        # notification_engine.fanout_appliance_event() actually reads
        # for real delivery decisions, surfaced via /api/notification-
        # rules). This table is deliberately a separate, additive
        # concept -- WHERE to send (an actual email address/phone
        # number, plus verification state) -- neither of which that
        # existing rule table stores anywhere. It is not yet wired into
        # that live fanout path; see notification_settings_page.py's
        # own module docstring and this session's Notifications
        # milestone report for that follow-up.
        '''CREATE TABLE IF NOT EXISTS customer_notification_channels(user_id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,email_address TEXT,email_enabled INTEGER NOT NULL DEFAULT 0,email_verified_at TEXT,phone_number TEXT,sms_enabled INTEGER NOT NULL DEFAULT 0,phone_verified_at TEXT,event_types_json TEXT NOT NULL DEFAULT '[]',camera_scope TEXT NOT NULL DEFAULT 'all',quiet_hours_enabled INTEGER NOT NULL DEFAULT 0,quiet_start TEXT NOT NULL DEFAULT '22:00',quiet_end TEXT NOT NULL DEFAULT '07:00',delivery_mode TEXT NOT NULL DEFAULT 'immediate',updated_at TEXT NOT NULL,FOREIGN KEY(user_id) REFERENCES partner_users(id))''',
        # Only populated when camera_scope='selected' -- deliberately
        # empty (not a frozen snapshot) when camera_scope='all', so a
        # camera added to the account later is automatically included
        # without the customer having to re-save anything.
        '''CREATE TABLE IF NOT EXISTS customer_notification_channel_cameras(user_id TEXT NOT NULL,camera_id TEXT NOT NULL,PRIMARY KEY(user_id,camera_id),FOREIGN KEY(user_id) REFERENCES partner_users(id),FOREIGN KEY(camera_id) REFERENCES cameras(id))''',
        # Appliance identity contract (see appliance_identity.py's own
        # module docstring for the full design): the explicit grant this
        # whole contract exists to require -- a user is authorized for a
        # given appliance only if a live (revoked_at IS NULL) row here
        # resolves to it, never merely because partner_id matches.
        # scope_type is one of global/partner/customer/site/appliance;
        # scope_id is NULL only for scope_type='global'.
        '''CREATE TABLE IF NOT EXISTS identity_grants(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,role TEXT NOT NULL,scope_type TEXT NOT NULL,scope_id TEXT,granted_at TEXT NOT NULL,granted_by TEXT,revoked_at TEXT,FOREIGN KEY(user_id) REFERENCES partner_users(id))''',
        # v1/dev only: the manifest-signing keypair lives in this same
        # database. A real cloud deployment keeps the private key in a
        # proper KMS/secrets store, never a queryable table -- see
        # appliance_identity.py's module docstring.
        '''CREATE TABLE IF NOT EXISTS identity_signing_keys(key_id TEXT PRIMARY KEY,public_key_b64 TEXT NOT NULL,private_key_b64 TEXT NOT NULL,created_at TEXT NOT NULL,revoked_at TEXT)''',
    ]
    with database_connect() as db:
        for statement in statements: db.execute(statement)
        appliance_columns={item['name'] for item in db.execute('PRAGMA table_info(appliances)').fetchall()} if backend()=='sqlite' else {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='appliances'").fetchall()}
        if 'activation_status' not in appliance_columns: db.execute("ALTER TABLE appliances ADD COLUMN activation_status TEXT NOT NULL DEFAULT 'pending'")
        for column,definition in [('partner_id','TEXT'),('uptime_seconds','INTEGER NOT NULL DEFAULT 0'),('disk_capacity','REAL NOT NULL DEFAULT 0'),('recording_used','REAL NOT NULL DEFAULT 0'),('last_error','TEXT'),('state',"TEXT NOT NULL DEFAULT 'offline'"),('credential_revoked_at','TEXT')]:
            if column not in appliance_columns: db.execute(f'ALTER TABLE appliances ADD COLUMN {column} {definition}')
        # authorization_version: bumped on any grant/role/enabled change
        # for this user -- the deterministic (never clock-based) staleness
        # signal an appliance compares against its cached manifest. See
        # appliance_identity.py.
        partner_user_columns={item['name'] for item in db.execute('PRAGMA table_info(partner_users)').fetchall()} if backend()=='sqlite' else {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='partner_users'").fetchall()}
        if 'authorization_version' not in partner_user_columns: db.execute('ALTER TABLE partner_users ADD COLUMN authorization_version INTEGER NOT NULL DEFAULT 1')
    bootstrap_admin()
    from db_migrations import apply_migrations
    apply_migrations()


def password_hash(password: str) -> str:
    salt=secrets.token_bytes(16); digest=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,310000)
    return f'pbkdf2_sha256$310000${salt.hex()}${digest.hex()}'


def verify_password(password: str, encoded: str) -> bool:
    try:
        _,rounds,salt,digest=encoded.split('$'); actual=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),int(rounds)); return hmac.compare_digest(actual.hex(),digest)
    except (ValueError,TypeError): return False


def bootstrap_admin() -> None:
    """TEMPORARY LOCAL BOOTSTRAP PATH -- validation/emergency-access
    scaffolding only, not the production onboarding design. The real
    first-identity flow will come from the website -> AWS -> VMS
    pipeline; when that lands, this function (and its ANYAICAM_ADMIN_
    EMAIL/ANYAICAM_ADMIN_PASSWORD env vars) should be removed cleanly
    rather than left running alongside it as a second, parallel
    authentication path.

    Confirmed live on Samsung: a bootstrap admin created before this
    fix could log in fine right up until its own appliance activated --
    at that exact point POST /api/portal-login (main.py) switches from
    the simple local password check (partner_db.authenticate_detailed())
    to the cloud-delegated one (appliance_identity.authenticate_operator()),
    which requires a live identity_grants row resolving to the activated
    appliance's scope, not just a correct password. This account never
    had one, so every login attempt failed with a generic "Invalid email
    or password" regardless of how many times the password was reset.

    scope_type='global' -- corrected from an earlier version of this fix
    that used scope_type='partner'. That first attempt let POST /api/
    portal-login succeed again, but confirmed live on Samsung it left
    the Admin Portal itself effectively empty ("Your current role does
    not include manage_settings"): cloud_administrator_bridge() (main.py)
    -- the sole path any cloud-delegated session uses to reach the
    legacy Admin Portal's manage_settings-gated pages -- deliberately
    only recognizes a scope_type='global' administrator grant
    (has_global_administrator_grant()), by design excluding a partner-
    scoped (company-level) administrator from ever silently becoming a
    true platform administrator (see that function's own docstring, and
    test_cloud_administrator_bridge.py's test_partner_scoped_
    administrator_cannot_reach_admin_portal). This bootstrap account
    represents the one true AnyAiCam operator identity, not a scoped
    company admin, and needs exactly that reach to match what its
    pre-activation local-password check granted it (unrestricted
    access, no scope concept at all) -- so scope_type='global' (scope_id
    NULL, per the schema's own convention) is the correct, already-
    documented choice here, not an over-broad one: it is the one
    designed-in scope 'administrator' role legitimately has for a true
    top-level operator (see VALID_ROLE_SCOPES's own comment in
    appliance_identity.py), and grant_resolves() already treats
    scope_type='global' as authorized for every appliance unconditionally,
    same as 'partner' was for appliances under one partner_id -- so this
    correction does not narrow appliance-login access at all, only
    restores the Admin Portal reach the account needs. Checking for an
    existing live (revoked_at IS NULL) match before creating one makes
    this idempotent: rerunning bootstrap_admin() (it runs on every
    initialize_database() call, i.e. every container start) never
    creates a duplicate grant, and an admin that already has the correct
    grant is left completely untouched.

    ANYAICAM_ADMIN_PASSWORD is required ONLY to create a brand-new
    account -- an already-existing bootstrap admin (found by email
    alone) never has its password_hash touched here, and no password
    needs to be present in the environment at all for its missing grant
    to be backfilled. This matters in exactly the scenario that caused
    the live failure above: the temporary password env var gets removed
    from vms.env once the account is confirmed created (never left
    sitting in a persistent file), but this function keeps running on
    every container start regardless -- it must be able to backfill a
    still-missing grant for that already-created account without ever
    needing a password (real or placeholder) put back into the
    environment just to reach this code."""
    email=os.getenv('ANYAICAM_ADMIN_EMAIL','').strip().lower()
    if not email: return
    now=datetime.now().isoformat(); partner_id='anyaicam-primary'
    with database_connect() as db:
        existing=db.execute('SELECT id FROM partner_users WHERE email=?',(email,)).fetchone()
        if existing:
            user_id=existing['id']
        else:
            password=os.getenv('ANYAICAM_ADMIN_PASSWORD','')
            if not password: return
            db.execute('INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)',(partner_id,'AnyAiCam','approved',REAL_SOURCE,now))
            user_id=secrets.token_hex(5)
            db.execute('INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,1,?)',(user_id,partner_id,email,'Administrator','administrator',password_hash(password),now))
        has_grant=db.execute("SELECT 1 FROM identity_grants WHERE user_id=? AND role='administrator' AND scope_type='global' AND revoked_at IS NULL",(user_id,)).fetchone()
        if not has_grant:
            from appliance_identity import create_grant
            create_grant(db,user_id=user_id,role='administrator',scope_type='global',scope_id=None,granted_by='system:bootstrap_admin',now=now)


class FirstAdminAlreadyExists(Exception):
    """Raised by create_first_admin() when at least one partner_users
    row already exists. The web setup path (partner_portal.py's
    GET/POST /setup) is the only caller -- it must never create a
    second bootstrap admin once any account exists, on a genuinely
    fresh install (RUNTIME_ROLE=edge, no ANYAICAM_ADMIN_EMAIL/PASSWORD
    env vars set) where bootstrap_admin() above never ran."""


def create_first_admin(email: str, password: str) -> str:
    """The interactive counterpart to bootstrap_admin() above: the
    operator enters the email/password themselves, once, through the
    browser (see partner_portal.py's /setup), instead of an operator
    having to set ANYAICAM_ADMIN_EMAIL/ANYAICAM_ADMIN_PASSWORD in the
    server environment (which would otherwise be the ONLY way to seed
    a first identity on a fresh edge appliance -- and would mean the
    plaintext password sitting in a persistent env file, exactly what
    this function exists to avoid). password_hash() runs immediately;
    the plaintext is never written anywhere, not even transiently to a
    file. The "does an admin already exist" check happens INSIDE this
    same connection/transaction, immediately before the insert -- not
    only at the caller's earlier GET-time check -- so this can never
    create a second admin even under a race between two concurrent
    attempts hitting the endpoint at once. Raises FirstAdminAlreadyExists
    (never silently no-ops, and never overwrites anything) if any
    partner_users row already exists. Returns the new user's id."""
    now=datetime.now().isoformat(); partner_id='anyaicam-primary'
    with connection() as db:
        if db.execute('SELECT 1 FROM partner_users LIMIT 1').fetchone() is not None:
            raise FirstAdminAlreadyExists('An administrator account already exists.')
        db.execute('INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)',(partner_id,'AnyAiCam','approved',REAL_SOURCE,now))
        user_id=secrets.token_hex(5)
        db.execute('INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,1,?)',(user_id,partner_id,email,'Administrator','administrator',password_hash(password),now))
    return user_id


def authenticate(email: str,password: str):
    user,reason=authenticate_detailed(email,password)
    return user if reason=='ok' else None


def authenticate_detailed(email: str,password: str):
    """Authenticate without collapsing account-state failures into bad passwords."""
    with connection() as db:
        record=db.execute('''SELECT u.*,p.approval_status AS partner_approval_status,
            (SELECT expires_at FROM invitations i WHERE lower(i.email)=lower(u.email) AND i.status='pending' ORDER BY i.created_at DESC LIMIT 1) AS invitation_expires_at
            FROM partner_users u LEFT JOIN partners p ON p.id=u.partner_id
            WHERE lower(u.email)=?''',(email.strip().lower(),)).fetchone()
    if not record or not verify_password(password,record['password_hash']): return None,'invalid'
    user=dict(record); status=str(user.get('account_status') or 'active').lower()
    if user.get('must_change_password') and user.get('invitation_expires_at') and user['invitation_expires_at']<datetime.now().isoformat(): return None,'invitation_expired'
    if status in {'suspended','revoked'}: return None,status
    if status=='pending' or not user.get('approved'): return None,'pending'
    if user.get('role') in {'administrator','partner_owner','salesperson','technician'}:
        approval=str(user.get('partner_approval_status') or 'pending').lower()
        if approval in {'rejected','revoked','suspended'}: return None,'revoked' if approval=='rejected' else approval
        if approval!='approved': return None,'pending'
    return user,'ok'


def allowed(identity: dict, permission: str) -> bool:
    permissions=ROLE_PERMISSIONS.get(identity.get('role'),set()); return '*' in permissions or permission in permissions


def require_permission(identity: dict, permission: str) -> None:
    if not allowed(identity,permission): raise PermissionError(f'Permission required: {permission}')


def audit(actor: dict,action: str,entity_type: str='',entity_id: str='',details=None) -> None:
    with connection() as db: db.execute('INSERT INTO audit_logs(actor_email,actor_role,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)',(actor.get('email'),actor.get('role'),action,entity_type,entity_id,json.dumps(details or {}),datetime.now().isoformat()))


def rows(query: str,params=()) -> list[dict]:
    with connection() as db: return [dict(row) for row in db.execute(query,params).fetchall()]


def row(query: str,params=()):
    with connection() as db: result=db.execute(query,params).fetchone(); return dict(result) if result else None


def execute(query: str,params=()) -> None:
    with connection() as db: db.execute(query,params)


ensure_database_initialized()
