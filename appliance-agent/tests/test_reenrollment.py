import io,json,logging,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from anyaicam_agent.config import AgentConfig
from anyaicam_agent import reenrollment

class Tests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory(); r=Path(self.t.name); self.r=r; self.e=r/'etc'; self.s=r/'state'; self.v=r/'vms'
  for p in (self.e,self.s,self.v):p.mkdir()
  self.c=AgentConfig(cloud_id='AIC-OLD',config_dir=str(self.e),state_dir=str(self.s),log_dir=str(r/'log'),vms_recordings_path=str(self.v)); self.ap=self.e/'agent.json'; self.cp=self.s/'credential.json'; self.vp=self.v/'appliance_identity.json'
  self.ap.write_text(json.dumps(self.c.__dict__)); self.cp.write_text(json.dumps({'appliance_id':'old','credential_id':'old-id','credential':'old-secret'})); self.vp.write_text(json.dumps({'appliance_id':'old','cloud_id':'AIC-OLD','credential':'old-secret','customer_id':'old-c','site_id':'old-s','partner_id':'old-p','activated_at':'old','activation_version':2}))
  self.orig={p:p.read_bytes() for p in (self.ap,self.cp,self.vp)}; self.a={'appliance_id':'new','cloud_id':'AIC-NEW','credential':'new-secret','credential_id':'new-id','customer_id':'new-c','site_id':'new-s','partner_id':'new-p'}; self.restarts=0
 def tearDown(self):self.t.cleanup()
 def restart(self):self.restarts+=1
 def perform(self,**kw):
  x={'expected_cloud_id':'AIC-NEW','vms_identity_path':self.vp,'restart_service':self.restart,'verify_authentication':lambda _:True,'backup_root':self.r/'backup'};x.update(kw);return reenrollment.coordinated_reenroll(self.c,self.a,**x)
 def rolled(self):
  for p,b in self.orig.items():self.assertEqual(p.read_bytes(),b)
 def test_success_all_three_agree(self):
  z=self.perform();a=json.loads(self.ap.read_text());c=json.loads(self.cp.read_text());v=json.loads(self.vp.read_text());self.assertEqual(a['cloud_id'],'AIC-NEW');self.assertEqual(c['credential'],'new-secret');self.assertEqual(v['cloud_id'],'AIC-NEW');self.assertEqual(v['customer_id'],'new-c');self.assertEqual(v['site_id'],'new-s');self.assertEqual(v['activation_version'],3);self.assertEqual(z['appliance_id'],'new')
 def test_backups(self):
  z=self.perform();b=Path(z['backup_dir']);[self.assertEqual((b/p.name).read_bytes(),x) for p,x in self.orig.items()]
 def test_validation_failure(self):
  a=dict(self.a);a.pop('credential')
  with self.assertRaises(ValueError):reenrollment.coordinated_reenroll(self.c,a,expected_cloud_id='AIC-NEW',vms_identity_path=self.vp,restart_service=self.restart,verify_authentication=lambda _:True)
  self.rolled()
 def test_staged_validation_failure(self):
  real=reenrollment._read
  with patch.object(reenrollment,'_read',side_effect=lambda p:{'bad':1} if '.credential.json.reenroll-' in p.name else real(p)),self.assertRaises(reenrollment.ReenrollmentError):self.perform()
  self.rolled()
 def fail_write(self,target):
  real=os.replace;hit=False
  def replace(src,dst):
   nonlocal hit
   if Path(dst)==target and not hit:hit=True;raise OSError('fail')
   return real(src,dst)
  with self.assertRaises(reenrollment.ReenrollmentError):self.perform(replace_file=replace)
  self.rolled()
 def test_agent_write_failure(self):self.fail_write(self.ap)
 def test_credential_write_failure(self):self.fail_write(self.cp)
 def test_vms_write_failure(self):self.fail_write(self.vp)
 def test_restart_failure(self):
  n=0
  def restart():
   nonlocal n;n+=1
   if n==1:raise RuntimeError('fail')
  with self.assertRaises(reenrollment.ReenrollmentError):self.perform(restart_service=restart)
  self.rolled();self.assertEqual(n,2)
 def test_auth_failure(self):
  with self.assertRaises(reenrollment.ReenrollmentError):self.perform(verify_authentication=lambda _:False)
  self.rolled();self.assertEqual(self.restarts,2)
 def test_mismatch(self):
  with self.assertRaises(ValueError):self.perform(expected_cloud_id='AIC-X')
  self.rolled()
 def test_no_secret_logs(self):
  s=io.StringIO();l=logging.getLogger(str(id(self)));l.handlers=[logging.StreamHandler(s)];l.setLevel(logging.INFO);self.perform(logger=l);self.assertNotIn('new-secret',s.getvalue());self.assertNotIn('old-secret',s.getvalue())

# ================================================== first-run bootstrap (ensure_identity_files_exist)
#
# Confirmed live during the physical Samsung activation task: a
# genuinely fresh appliance (real first-ever anyaicam-setup run, none
# of the three identity files created yet) hit coordinated_reenroll()'s
# own "Every existing identity file must be present before
# re-enrollment." guard immediately -- every test above pre-seeds all
# three files for exactly this reason, and none of them covers a truly
# fresh device. ensure_identity_files_exist() is the fix: called once,
# before coordinated_reenroll(), it creates whatever's missing with a
# minimal, inert placeholder coordinated_reenroll() already treats as
# activation_version 0, and never touches a file that already exists.

class FirstRunBootstrapTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();r=Path(self.t.name);self.r=r;self.e=r/'etc';self.s=r/'state';self.v=r/'vms'
  for p in (self.e,self.s,self.v):p.mkdir()
  self.c=AgentConfig(cloud_id='',portal_url='https://portal-staging.anyaicam.test',config_dir=str(self.e),state_dir=str(self.s),log_dir=str(r/'log'),vms_recordings_path=str(self.v))
  self.ap=self.e/'agent.json';self.cp=self.s/'credential.json';self.vp=self.v/'appliance_identity.json'
  self.a={'appliance_id':'new','cloud_id':'AIC-2FC1F54F','credential':'new-secret','credential_id':'new-id','customer_id':'new-c','site_id':'new-s','partner_id':'new-p'};self.restarts=0
 def tearDown(self):self.t.cleanup()
 def restart(self):self.restarts+=1

 def test_a_genuinely_fresh_device_can_now_complete_reenrollment(self):
  """The exact confirmed-live failure: none of the three files exist
  yet. Without the bootstrap step this raises ValueError immediately;
  with it, first activation succeeds like any other."""
  self.assertFalse(self.ap.exists());self.assertFalse(self.cp.exists());self.assertFalse(self.vp.exists())
  reenrollment.ensure_identity_files_exist(self.c,self.vp)
  z=reenrollment.coordinated_reenroll(self.c,self.a,expected_cloud_id='AIC-2FC1F54F',vms_identity_path=self.vp,restart_service=self.restart,verify_authentication=lambda _:True)
  a=json.loads(self.ap.read_text());c=json.loads(self.cp.read_text());v=json.loads(self.vp.read_text())
  self.assertEqual(a['cloud_id'],'AIC-2FC1F54F');self.assertEqual(c['credential'],'new-secret')
  self.assertEqual(v['cloud_id'],'AIC-2FC1F54F');self.assertEqual(v['activation_version'],1)  # first-ever activation
  self.assertEqual(z['appliance_id'],'new')

 def test_bootstrap_placeholder_carries_the_configured_portal_url(self):
  """The portal URL the operator configured before activation (e.g.
  the staging endpoint) must survive into agent.json's placeholder,
  not get silently reset to AgentConfig's own bare default."""
  reenrollment.ensure_identity_files_exist(self.c,self.vp)
  agent=json.loads(self.ap.read_text())
  self.assertEqual(agent['portal_url'],'https://portal-staging.anyaicam.test')

 def test_bootstrap_never_touches_a_file_that_already_exists(self):
  """A device with a real prior identity is completely unaffected --
  re-enrollment there must behave exactly as it always has."""
  self.ap.write_text(json.dumps({'sentinel':'do-not-touch'}))
  reenrollment.ensure_identity_files_exist(self.c,self.vp)
  self.assertEqual(json.loads(self.ap.read_text()),{'sentinel':'do-not-touch'})
  # The other two, genuinely missing, still get created.
  self.assertTrue(self.cp.exists());self.assertTrue(self.vp.exists())

 def test_bootstrap_is_idempotent(self):
  reenrollment.ensure_identity_files_exist(self.c,self.vp)
  before=(self.ap.read_bytes(),self.cp.read_bytes(),self.vp.read_bytes())
  reenrollment.ensure_identity_files_exist(self.c,self.vp)
  after=(self.ap.read_bytes(),self.cp.read_bytes(),self.vp.read_bytes())
  self.assertEqual(before,after)

 def test_existing_reenrollment_scenario_is_completely_unaffected(self):
  """The bootstrap step must be a true no-op whenever a real prior
  identity already exists -- second activation (moving to a new
  customer, credential refresh, etc.) behaves byte-for-byte as it did
  before this fix existed."""
  self.ap.write_text(json.dumps({'cloud_id':'AIC-OLD'}))
  self.cp.write_text(json.dumps({'appliance_id':'old','credential_id':'old-id','credential':'old-secret'}))
  self.vp.write_text(json.dumps({'appliance_id':'old','cloud_id':'AIC-OLD','credential':'old-secret','customer_id':'old-c','site_id':'old-s','partner_id':'old-p','activated_at':'old','activation_version':2}))
  reenrollment.ensure_identity_files_exist(self.c,self.vp)
  v=json.loads(self.vp.read_text())
  self.assertEqual(v['activation_version'],2)  # untouched -- coordinated_reenroll() itself bumps this, not the bootstrap
  self.assertEqual(v['cloud_id'],'AIC-OLD')
