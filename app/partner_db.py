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
    ]
    with database_connect() as db:
        for statement in statements: db.execute(statement)
        appliance_columns={item['name'] for item in db.execute('PRAGMA table_info(appliances)').fetchall()} if backend()=='sqlite' else {item['column_name'] for item in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='appliances'").fetchall()}
        if 'activation_status' not in appliance_columns: db.execute("ALTER TABLE appliances ADD COLUMN activation_status TEXT NOT NULL DEFAULT 'pending'")
        for column,definition in [('partner_id','TEXT'),('uptime_seconds','INTEGER NOT NULL DEFAULT 0'),('disk_capacity','REAL NOT NULL DEFAULT 0'),('recording_used','REAL NOT NULL DEFAULT 0'),('last_error','TEXT'),('state',"TEXT NOT NULL DEFAULT 'offline'"),('credential_revoked_at','TEXT')]:
            if column not in appliance_columns: db.execute(f'ALTER TABLE appliances ADD COLUMN {column} {definition}')
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
    email=os.getenv('ANYAICAM_ADMIN_EMAIL','').strip().lower(); password=os.getenv('ANYAICAM_ADMIN_PASSWORD','')
    if not email or not password: return
    now=datetime.now().isoformat(); partner_id='anyaicam-primary'
    with database_connect() as db:
        db.execute('INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)',(partner_id,'AnyAiCam','approved',REAL_SOURCE,now))
        existing=db.execute('SELECT id FROM partner_users WHERE email=?',(email,)).fetchone()
        if not existing: db.execute('INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,1,?)',(secrets.token_hex(5),partner_id,email,'Administrator','administrator',password_hash(password),now))


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
