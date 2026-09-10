import getpass
import json
import shutil
import subprocess
from pathlib import Path

from .commands import _queue_privileged_action
from .config import AgentConfig
from .discovery import scan
from .portal import PortalClient,PortalError
from .reenrollment import ReenrollmentError,coordinated_reenroll,first_enroll


def _upsert_vms_env_key(config:AgentConfig,key:str,value:str) -> None:
    """Python-side mirror of installer/06-deploy-vms.sh's own
    upsert_env_key(): replace an existing `key=` line in the VMS env file
    or append one, then rewrite atomically. Used for ANYAICAM_CLOUD_URL,
    which -- like ANYAICAM_VMS_COMMIT/ANYAICAM_BUILD_ID in that script,
    and unlike the generate-once secrets there -- reflects this
    activation's own current portal_url and is meant to be refreshed on
    every (re-)activation, not preserved from an earlier install."""
    path=Path(config.config_dir)/'vms.env'
    lines=[line for line in path.read_text(encoding='utf-8').splitlines() if not line.startswith(key+'=')] if path.is_file() else []
    lines.append(f'{key}={value}')
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    temporary.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    temporary.replace(path)


def qr_payload():
    value=input('Paste/scan provisioning QR value, or press Enter for manual entry: ').strip()
    if value: return (value.split('|',1)+[''])[:2]
    image=input('Optional QR image path (requires zbarimg), or press Enter: ').strip()
    if image and shutil.which('zbarimg'):
        output=subprocess.run(['zbarimg','--quiet','--raw',image],capture_output=True,text=True,timeout=15,check=True).stdout.strip(); return (output.split('|',1)+[''])[:2]
    return input('Cloud ID: ').strip(),getpass.getpass('Activation token: ').strip()


def main():
    print('\nAnyAiCam first-run appliance setup\n')
    config=AgentConfig.load(); config.portal_url=input(f'Portal URL [{config.portal_url}]: ').strip() or config.portal_url; config.mode=input(f'Mode (development/production) [{config.mode}]: ').strip() or config.mode
    cloud_id,token=qr_payload(); config.cloud_id=cloud_id.upper()
    client=PortalClient(config.portal_url)
    try: info=client.test(); print('Portal connectivity: OK',info.get('mode'))
    except PortalError as error: raise SystemExit(f'Portal connectivity failed: {error}')
    try: activated=client.activate(config.cloud_id,token)
    except PortalError as error: raise SystemExit(f'Activation failed: {error}')
    print('Assigned customer:',activated.get('customer_id')); print('Assigned site:',activated.get('site_id'))
    def restart_service(): subprocess.run(['systemctl','restart','anyaicam-agent.service'],check=True)
    def verify_authentication(identity):
        check=PortalClient(config.portal_url,identity['appliance_id'],identity['credential'])
        check.request('GET','/api/appliance/commands')
        return True
    vms_identity_path=Path(config.vms_recordings_path)/'appliance_identity.json'
    # A truly fresh appliance has none of agent.json/credential.json/the VMS's
    # own appliance_identity.json yet -- coordinated_reenroll() requires all
    # three to already exist (it exists to REPLACE a prior identity, with
    # something to roll back to). first_enroll() is the counterpart for that
    # case; picking the wrong one here would either fail closed for a fresh
    # box (coordinated_reenroll) or refuse to touch an already-activated one
    # (first_enroll) -- both fail loud, never silently do the wrong thing.
    already_enrolled=all(p.is_file() for p in (Path(config.config_dir)/'agent.json',config.credential_file,vms_identity_path))
    enroll=coordinated_reenroll if already_enrolled else first_enroll
    try:
        enroll(config,activated,expected_cloud_id=config.cloud_id,
            vms_identity_path=vms_identity_path,
            restart_service=restart_service,verify_authentication=verify_authentication)
    except ReenrollmentError as error: raise SystemExit(str(error)) from error
    # The VMS container reads portal_url from its own ANYAICAM_CLOUD_URL env
    # var at process start (recording_uploader.py/live_relay_uploader.py/
    # analytics_sync.py/event_media_uploader.py all read it the same way) --
    # an env var only takes effect on container (re)start, unlike the
    # identity file above, which those same modules read fresh off disk via
    # the shared /var/lib/anyaicam mount on every call. Written here, after
    # enrollment succeeds, so a fresh or re-pointed appliance's VMS container
    # always ends up configured with the SAME portal this agent just
    # authenticated against -- never a stale or guessed value.
    try:
        _upsert_vms_env_key(config,'ANYAICAM_CLOUD_URL',config.portal_url)
        status,_,error=_queue_privileged_action(config,'restart_vms',{'confirmed':True})
        if status!='completed': print(f'WARNING: could not queue a VMS restart automatically ({error}); restart the anyaicam-vms container manually to apply the new cloud URL.')
    except OSError as error:
        print(f'WARNING: could not update the VMS environment file ({error}); set ANYAICAM_CLOUD_URL={config.portal_url} in it manually and restart anyaicam-vms.')
    if input('Run camera discovery now? [Y/n]: ').strip().lower()!='n':
        cameras=scan(config.discovery_networks); config.cameras_file.parent.mkdir(parents=True,exist_ok=True); config.cameras_file.write_text(json.dumps(cameras,indent=2),encoding='utf-8'); print(f'Discovered {len(cameras)} compatible endpoints.')
    print('Configuration saved securely.')
    print('AnyAiCam service restarted and authenticated during identity commit.')


if __name__=='__main__': main()
