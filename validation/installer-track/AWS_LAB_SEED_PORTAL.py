import datetime,os,pathlib,secrets,sys
sys.path.insert(0,'/app')
from partner_db import initialize_database,connection,password_hash
from appliance_identity import create_grant
from customer_entitlements import upsert_entitlement
from customer_downloads import publish_vms_installer
initialize_database()
now=datetime.datetime.now(datetime.timezone.utc).isoformat()
password=secrets.token_urlsafe(24)+'Aa1!'
with connection() as db:
    db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)",('installer-test-partner','Temporary Installer Lab','approved','real',now))
    db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)",('installer-test-customer','installer-test-partner','Disposable Installer Customer','installer-owner@example.test','active','real',now))
    db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)",('installer-test-site','installer-test-customer','Simulated Camera Lab',now))
    db.execute("INSERT INTO partner_users(id,email,name,role,password_hash,approved,customer_id,created_at) VALUES(?,?,?,?,?,?,?,?)",('installer-test-owner','installer-owner@example.test','Installer Test Owner','customer_owner',password_hash(password),1,'installer-test-customer',now))
    create_grant(db,user_id='installer-test-owner',role='customer_owner',scope_type='customer',scope_id='installer-test-customer',granted_by='isolated-lab-provisioning',now=now)
for product in ('camera_slots_local','vms_license'):
    upsert_entitlement(customer_id='installer-test-customer',product=product,camera_slot_quantity=8,status='active')
# This registers private download bytes in ONLY the new lab's local object store.
# It does not publish an installer to S3, the public website or any other portal.
publish_vms_installer(pathlib.Path('/home/ubuntu/anyaicam-lab/incoming/anyaicam-appliance-installer-1.1.1-vms-a1c81e0f4926.tar.gz'),commit='a1c81e0f492633a80198503e721fc6c277e6a8b3',version='1.1.1')
credential=pathlib.Path('/var/lib/anyaicam-installer-lab/owner-login.txt')
fd=os.open(credential,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as output:
    output.write('Disposable test portal ONLY\nEmail: installer-owner@example.test\nPassword: '+password+'\n')
print('Created one test owner, one customer-scoped grant, one location and local 8-camera fixture license. No external APIs called.')
