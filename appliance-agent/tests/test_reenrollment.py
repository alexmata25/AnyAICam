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

 # ---- camera-binding lifecycle fix (2026-09-13): a genuine identity swap
 # (this whole class's own AIC-OLD -> AIC-NEW scenario) must reset
 # camera_bindings.json atomically alongside agent.json/credential.json/
 # appliance_identity.json -- confirmed live: an appliance re-enrolled
 # under a brand-new cloud_id kept its PRIOR identity's bindings forever,
 # permanently blocking a new customer's camera from binding to the same
 # physical hardware (see camera_binding.py's own self-healing fix for
 # the other half of this).

 def _seed_bindings(self,content=None):
  self.bp=Path(self.c.camera_bindings_file); self.bp.parent.mkdir(parents=True,exist_ok=True)
  content=content if content is not None else {'version':1,'bindings':[{'cloud_camera_id':'old-cam-1','camera_number':1,'mac_address':'14:2f:fd:a2:f6:af','approved_at':'old'}]}
  self.bp.write_text(json.dumps(content)); self.orig[self.bp]=self.bp.read_bytes(); return content

 def test_successful_reenroll_clears_old_camera_bindings(self):
  self._seed_bindings()
  self.perform()
  self.assertEqual(json.loads(self.bp.read_text()),{'version':1,'bindings':[]})

 def test_successful_reenroll_backs_up_the_prior_bindings(self):
  original=self._seed_bindings()
  z=self.perform()
  backed_up=json.loads((Path(z['backup_dir'])/self.bp.name).read_text())
  self.assertEqual(backed_up,original)

 def test_failed_reenroll_restores_previous_bindings(self):
  original=self._seed_bindings()
  with self.assertRaises(reenrollment.ReenrollmentError):self.perform(verify_authentication=lambda _:False)
  self.assertEqual(json.loads(self.bp.read_text()),original)
  self.rolled()  # identity files also correctly rolled back -- atomicity is not weakened

 def test_failed_reenroll_with_no_prior_bindings_leaves_none_behind(self):
  """No camera_bindings.json existed before the attempt -- on failure,
  the file this attempt staged must be removed, not left behind as a
  stray empty binding store."""
  self.bp=Path(self.c.camera_bindings_file)
  self.assertFalse(self.bp.exists())
  with self.assertRaises(reenrollment.ReenrollmentError):self.perform(verify_authentication=lambda _:False)
  self.assertFalse(self.bp.exists())

 def test_discovered_cameras_file_survives_identity_replacement(self):
  """discovered_cameras.json is physical-network evidence, not identity-
  scoped -- it must never be touched by a reenroll, successful or not."""
  dp=Path(self.c.discovered_cameras_file); dp.parent.mkdir(parents=True,exist_ok=True)
  content={'version':1,'cameras':[{'device_key':'urn:uuid:b3464000-5074-11b4-82cc-142ffda2f6af','mac_address':'14:2f:fd:a2:f6:af','ip':'192.168.0.38'}]}
  dp.write_text(json.dumps(content))
  self.perform()
  self.assertEqual(json.loads(dp.read_text()),content)

 def test_reenroll_with_the_same_cloud_id_preserves_existing_bindings(self):
  """A bare credential refresh for the SAME cloud_id (not a genuine
  identity swap) must never discard bindings that are still completely
  valid."""
  original=self._seed_bindings()
  same_cloud_activation=dict(self.a); same_cloud_activation['cloud_id']='AIC-OLD'
  z=reenrollment.coordinated_reenroll(self.c,same_cloud_activation,expected_cloud_id='AIC-OLD',vms_identity_path=self.vp,restart_service=self.restart,verify_authentication=lambda _:True,backup_root=self.r/'backup2')
  self.assertFalse(z['camera_bindings_reset'])
  self.assertEqual(json.loads(self.bp.read_text()),original)


class FirstEnrollTests(unittest.TestCase):
 """coordinated_reenroll() requires all three identity files to already
 exist (there must be something to roll back to) -- a truly fresh
 appliance has none of them, so it always raised ValueError before this
 counterpart existed. first_enroll() is what setup_wizard.main() now
 calls instead on that path; these tests seed NOTHING (unlike Tests
 above, which always seeds an 'old' identity first)."""
 def setUp(self):
  self.t=tempfile.TemporaryDirectory(); r=Path(self.t.name); self.r=r; self.e=r/'etc'; self.s=r/'state'; self.v=r/'vms'
  for p in (self.e,self.s,self.v):p.mkdir()
  self.c=AgentConfig(cloud_id='AIC-NEW',config_dir=str(self.e),state_dir=str(self.s),log_dir=str(r/'log'),vms_recordings_path=str(self.v))
  self.ap=self.e/'agent.json'; self.cp=self.s/'credential.json'; self.vp=self.v/'appliance_identity.json'
  self.a={'appliance_id':'new','cloud_id':'AIC-NEW','credential':'new-secret','credential_id':'new-id','customer_id':'new-c','site_id':'new-s','partner_id':'new-p'}; self.restarts=0
 def tearDown(self):self.t.cleanup()
 def restart(self):self.restarts+=1
 def perform(self,**kw):
  x={'expected_cloud_id':'AIC-NEW','vms_identity_path':self.vp,'restart_service':self.restart,'verify_authentication':lambda _:True};x.update(kw);return reenrollment.first_enroll(self.c,self.a,**x)
 def none_left(self):
  for p in (self.ap,self.cp,self.vp):self.assertFalse(p.exists())
 def test_success_creates_all_three_fresh(self):
  z=reenrollment.first_enroll(self.c,self.a,expected_cloud_id='AIC-NEW',vms_identity_path=self.vp,restart_service=self.restart,verify_authentication=lambda _:True)
  a=json.loads(self.ap.read_text());c=json.loads(self.cp.read_text());v=json.loads(self.vp.read_text())
  self.assertEqual(a['cloud_id'],'AIC-NEW');self.assertEqual(c['credential'],'new-secret');self.assertEqual(v['cloud_id'],'AIC-NEW');self.assertEqual(v['activation_version'],1);self.assertEqual(z['appliance_id'],'new');self.assertEqual(self.restarts,1)
 def test_refuses_if_agent_file_already_exists(self):
  self.ap.write_text('{}')
  with self.assertRaises(ValueError):self.perform()
 def test_refuses_if_credential_file_already_exists(self):
  self.cp.write_text('{}')
  with self.assertRaises(ValueError):self.perform()
 def test_refuses_if_vms_identity_already_exists(self):
  self.vp.write_text('{}')
  with self.assertRaises(ValueError):self.perform()
 def test_cleans_up_without_error_on_auth_failure(self):
  with self.assertRaises(reenrollment.ReenrollmentError):self.perform(verify_authentication=lambda _:False)
  self.none_left()
 def test_cleans_up_on_restart_failure(self):
  def restart():raise RuntimeError('fail')
  with self.assertRaises(reenrollment.ReenrollmentError):self.perform(restart_service=restart)
  self.none_left()
 def test_no_secret_logs(self):
  s=io.StringIO();l=logging.getLogger(str(id(self)));l.handlers=[logging.StreamHandler(s)];l.setLevel(logging.INFO);self.perform(logger=l);self.assertNotIn('new-secret',s.getvalue())
