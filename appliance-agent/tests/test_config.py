"""Regression coverage for the configuration-precedence defect found during
real-hardware Phase 2A claim validation: an installer bootstrap placeholder
(agent.env's ANYAICAM_PORTAL_URL=http://127.0.0.1:8000 /
ANYAICAM_AGENT_MODE=development, sourced by anyaicam-agent.service on every
start/restart/reboot) was silently overriding a portal URL a successful
claim/activation had already persisted into agent.json, because
AgentConfig.load() let any set environment variable win unconditionally.
See config.py's ACTIVATION_SCOPED_FIELDS/load() comments for the fix.

Also covers PROJECT_CHECKPOINT.md defect #3, found real on Ryzen
(2026-09-11): the first fix above only protected against an environment
value that still equals the field's own untouched default. It did nothing
for a real, non-default value left over in agent.env/vms.env from a PRIOR
activation -- indistinguishable, by a "does it equal the default?" check,
from a deliberate admin override, so it could still silently clobber a
value a NEW activation had just persisted. The fix removes that escape
hatch entirely: once agent.json holds a real value for an
activation-scoped field, no environment variable overrides it, full stop.
The only supported way to change cloud_id/portal_url/mode on an already-
activated appliance is to re-run setup (first_enroll()/
coordinated_reenroll()), which writes the new value directly into
agent.json -- never through this environment-variable merge."""
import json, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

from anyaicam_agent.config import AgentConfig
from anyaicam_agent import reenrollment

BOOTSTRAP_PORTAL_URL='http://127.0.0.1:8000'  # matches agent.env's literal default, and AgentConfig.portal_url's own dataclass default
BOOTSTRAP_MODE='development'                   # matches agent.env's literal default, and AgentConfig.mode's own dataclass default
CLAIMED_PORTAL_URL='https://claimed-control-plane.example:8000'  # deliberately non-loopback, standing in for a real disposable/staging/production portal


class LoadPrecedenceTests(unittest.TestCase):
 """Unit coverage directly on AgentConfig.load(), no enrollment involved."""
 def setUp(self):
  self.t=tempfile.TemporaryDirectory(); self.path=Path(self.t.name)/'agent.json'
  self._env=dict(os.environ)
 def tearDown(self):
  self.t.cleanup(); os.environ.clear(); os.environ.update(self._env)
 def _write(self,**fields):
  base=AgentConfig(); data={**base.__dict__,**fields}; self.path.write_text(json.dumps(data))

 def test_pre_activation_bootstrap_env_seeds_default(self):
  """No agent.json yet: the installer's bootstrap env var must still flow
  through unchanged -- this is what lets the interactive/claim wizard
  display it as the prompt's bracketed default. Unaffected by this fix."""
  with patch.dict(os.environ,{'ANYAICAM_PORTAL_URL':BOOTSTRAP_PORTAL_URL}):
   self.assertEqual(AgentConfig.load(self.path).portal_url,BOOTSTRAP_PORTAL_URL)

 def test_claimed_portal_survives_bootstrap_env_after_activation(self):
  """The core fix: agent.json already holds a real, claimed portal URL;
  agent.env still has the installer's untouched placeholder. The
  persisted, activated value must win."""
  self._write(portal_url=CLAIMED_PORTAL_URL)
  with patch.dict(os.environ,{'ANYAICAM_PORTAL_URL':BOOTSTRAP_PORTAL_URL}):
   self.assertEqual(AgentConfig.load(self.path).portal_url,CLAIMED_PORTAL_URL)

 def test_stale_real_env_value_no_longer_wins_over_a_newer_activation(self):
  """Defect #3 (docs/PROJECT_CHECKPOINT.md), reproduced: agent.env/
  vms.env holds a real, non-default value left over from a PRIOR
  activation (not the installer's untouched placeholder -- this is
  exactly what made it look like a deliberate admin override before
  this fix). agent.json has since been updated by a NEWER activation.
  The newer persisted value must win; the stale env value must not."""
  stale_env_value='https://old-activation-control-plane.example:8000'
  self._write(portal_url=CLAIMED_PORTAL_URL)
  with patch.dict(os.environ,{'ANYAICAM_PORTAL_URL':stale_env_value}):
   self.assertEqual(AgentConfig.load(self.path).portal_url,CLAIMED_PORTAL_URL)

 def test_administrator_cannot_repoint_an_activated_appliance_via_env_alone(self):
  """An environment variable is no longer a way to change cloud_id/
  portal_url/mode on an already-activated appliance, even when someone
  intends it as a deliberate override -- config.py's load() cannot tell
  that apart from the stale-leftover-value case above by inspecting the
  value alone. The only supported channel is re-running setup, which
  goes through first_enroll()/coordinated_reenroll() and writes agent.
  json directly (see the EnrollmentIntegrationTests below)."""
  self._write(portal_url=CLAIMED_PORTAL_URL)
  intended_override='https://migrated-control-plane.example:8000'
  with patch.dict(os.environ,{'ANYAICAM_PORTAL_URL':intended_override}):
   self.assertEqual(AgentConfig.load(self.path).portal_url,CLAIMED_PORTAL_URL)

 def test_claimed_mode_survives_bootstrap_env_after_activation(self):
  self._write(mode='production')
  with patch.dict(os.environ,{'ANYAICAM_AGENT_MODE':BOOTSTRAP_MODE}):
   self.assertEqual(AgentConfig.load(self.path).mode,'production')

 def test_claimed_cloud_id_survives_bootstrap_env_after_activation(self):
  """No installer script bootstraps ANYAICAM_CLOUD_ID today, but cloud_id
  is the third activation-established field first_enroll()/
  coordinated_reenroll() write -- protected for the same reason, so a
  future installer version cannot reintroduce this defect class for it."""
  self._write(cloud_id='AIC-REAL-CLAIMED')
  with patch.dict(os.environ,{'ANYAICAM_CLOUD_ID':''}):
   self.assertEqual(AgentConfig.load(self.path).cloud_id,'AIC-REAL-CLAIMED')

 def test_non_activation_field_env_override_unaffected(self):
  """Ordinary runtime tuning knobs are not activation identity -- an
  administrator (or the installer) must still be able to set them via
  environment variable at any time, activated or not."""
  self._write(checkin_seconds=30)
  with patch.dict(os.environ,{'ANYAICAM_CHECKIN_SECONDS':'120'}):
   self.assertEqual(AgentConfig.load(self.path).checkin_seconds,120)

 def test_missing_persisted_key_falls_back_to_env(self):
  """Defensive: if agent.json exists but happens to lack this key, there
  is nothing persisted to protect, so the environment value applies
  normally."""
  data=AgentConfig().__dict__; data.pop('portal_url'); self.path.write_text(json.dumps(data))
  with patch.dict(os.environ,{'ANYAICAM_PORTAL_URL':BOOTSTRAP_PORTAL_URL}):
   self.assertEqual(AgentConfig.load(self.path).portal_url,BOOTSTRAP_PORTAL_URL)

 def test_restart_then_reboot_equivalent_repeated_loads(self):
  """AgentConfig.load() is re-invoked fresh by every anyaicam-agent.service
  start -- restart and reboot are indistinguishable from its perspective.
  Two independent loads in a row (simulating restart, then a later
  reboot) must both still see the claimed value, not just the first."""
  self._write(portal_url=CLAIMED_PORTAL_URL)
  with patch.dict(os.environ,{'ANYAICAM_PORTAL_URL':BOOTSTRAP_PORTAL_URL}):
   self.assertEqual(AgentConfig.load(self.path).portal_url,CLAIMED_PORTAL_URL)
   self.assertEqual(AgentConfig.load(self.path).portal_url,CLAIMED_PORTAL_URL)


class EnrollmentIntegrationTests(unittest.TestCase):
 """Reproduces the exact reported scenario end-to-end: a clean install's
 bootstrap env vars, a real first_enroll()/coordinated_reenroll() call
 against a non-loopback portal, then a fresh AgentConfig.load() with the
 bootstrap env vars still set -- exactly what anyaicam-agent.service's
 EnvironmentFile does on every subsequent start."""
 def setUp(self):
  self.t=tempfile.TemporaryDirectory(); r=Path(self.t.name); self.e=r/'etc'; self.s=r/'state'; self.v=r/'vms'
  for p in (self.e,self.s,self.v): p.mkdir()
  self.ap=self.e/'agent.json'
  self.a={'appliance_id':'new','cloud_id':'AIC-NEW','credential':'new-secret','credential_id':'new-id','customer_id':'new-c','site_id':'new-s','partner_id':'new-p'}
  self._env=dict(os.environ)
 def tearDown(self):
  self.t.cleanup(); os.environ.clear(); os.environ.update(self._env)

 def test_first_enroll_claim_survives_bootstrap_env_on_next_load(self):
  """clean install -> installer writes bootstrap/default config -> claim
  to a non-local portal -> persisted portal URL -> systemd agent
  restart -> agent still contacts the claimed portal."""
  config=AgentConfig(portal_url=CLAIMED_PORTAL_URL,config_dir=str(self.e),state_dir=str(self.s),log_dir=str(self.e/'log'),vms_recordings_path=str(self.v))
  vms_path=self.v/'appliance_identity.json'
  reenrollment.first_enroll(config,self.a,expected_cloud_id='AIC-NEW',vms_identity_path=vms_path,restart_service=lambda:None,verify_authentication=lambda _:True)
  self.assertEqual(json.loads(self.ap.read_text())['portal_url'],CLAIMED_PORTAL_URL)
  # Simulate anyaicam-agent.service restarting: a fresh process re-invokes
  # AgentConfig.load() while agent.env still holds the installer's
  # untouched bootstrap placeholder.
  with patch.dict(os.environ,{'ANYAICAM_PORTAL_URL':BOOTSTRAP_PORTAL_URL}):
   self.assertEqual(AgentConfig.load(self.ap).portal_url,CLAIMED_PORTAL_URL)
   self.assertEqual(AgentConfig.load(self.ap).portal_url,CLAIMED_PORTAL_URL)  # reboot-equivalent: a second independent load

 def test_reenrollment_legacy_activation_claim_survives_bootstrap_env(self):
  """Same protection for the already-enrolled/legacy-admin-driven
  re-activation path (coordinated_reenroll()), not just first_enroll()."""
  old=AgentConfig(cloud_id='AIC-OLD',portal_url=BOOTSTRAP_PORTAL_URL,config_dir=str(self.e),state_dir=str(self.s),log_dir=str(self.e/'log'),vms_recordings_path=str(self.v))
  vms_path=self.v/'appliance_identity.json'; credential_path=old.credential_file
  self.ap.write_text(json.dumps(old.__dict__))
  credential_path.write_text(json.dumps({'appliance_id':'old','credential_id':'old-id','credential':'old-secret'}))
  vms_path.write_text(json.dumps({'appliance_id':'old','cloud_id':'AIC-OLD','credential':'old-secret','customer_id':'old-c','site_id':'old-s','partner_id':'old-p','activated_at':'old','activation_version':1}))
  new_config=AgentConfig(cloud_id='AIC-OLD',portal_url=CLAIMED_PORTAL_URL,config_dir=str(self.e),state_dir=str(self.s),log_dir=str(self.e/'log'),vms_recordings_path=str(self.v))
  reenrollment.coordinated_reenroll(new_config,self.a,expected_cloud_id='AIC-NEW',vms_identity_path=vms_path,restart_service=lambda:None,verify_authentication=lambda _:True)
  self.assertEqual(json.loads(self.ap.read_text())['portal_url'],CLAIMED_PORTAL_URL)
  with patch.dict(os.environ,{'ANYAICAM_PORTAL_URL':BOOTSTRAP_PORTAL_URL}):
   self.assertEqual(AgentConfig.load(self.ap).portal_url,CLAIMED_PORTAL_URL)

 def test_reenrollment_to_a_new_portal_survives_a_stale_real_env_value(self):
  """Reproduces Ryzen's exact 2026-09-11 failure end-to-end: agent.env/
  vms.env holds a REAL, non-default portal URL left over from a prior
  activation (not the installer's untouched placeholder -- a stale value
  is what actually broke on Ryzen). A fresh coordinated_reenroll() then
  activates against a DIFFERENT, newer portal. The next agent restart
  must see the new activation's portal, not the stale env leftover --
  and the only correct way to have changed it was the re-enrollment
  call itself, not the environment."""
  stale_env_value='https://old-activation-control-plane.example:8000'
  old=AgentConfig(cloud_id='AIC-OLD',portal_url=stale_env_value,config_dir=str(self.e),state_dir=str(self.s),log_dir=str(self.e/'log'),vms_recordings_path=str(self.v))
  vms_path=self.v/'appliance_identity.json'; credential_path=old.credential_file
  self.ap.write_text(json.dumps(old.__dict__))
  credential_path.write_text(json.dumps({'appliance_id':'old','credential_id':'old-id','credential':'old-secret'}))
  vms_path.write_text(json.dumps({'appliance_id':'old','cloud_id':'AIC-OLD','credential':'old-secret','customer_id':'old-c','site_id':'old-s','partner_id':'old-p','activated_at':'old','activation_version':1}))
  new_config=AgentConfig(cloud_id='AIC-OLD',portal_url=CLAIMED_PORTAL_URL,config_dir=str(self.e),state_dir=str(self.s),log_dir=str(self.e/'log'),vms_recordings_path=str(self.v))
  reenrollment.coordinated_reenroll(new_config,self.a,expected_cloud_id='AIC-NEW',vms_identity_path=vms_path,restart_service=lambda:None,verify_authentication=lambda _:True)
  self.assertEqual(json.loads(self.ap.read_text())['portal_url'],CLAIMED_PORTAL_URL)
  # agent.env/vms.env was never touched by re-enrollment -- it still has
  # the stale real value from the OLD activation. This is Ryzen's exact
  # condition: a real, non-default, but outdated env value coexisting
  # with a freshly-persisted, newer agent.json.
  with patch.dict(os.environ,{'ANYAICAM_PORTAL_URL':stale_env_value}):
   self.assertEqual(AgentConfig.load(self.ap).portal_url,CLAIMED_PORTAL_URL)


if __name__=='__main__':
 unittest.main()
