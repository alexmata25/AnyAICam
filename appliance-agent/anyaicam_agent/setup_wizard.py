import getpass
import json
import shutil
import subprocess
from pathlib import Path

from .config import AgentConfig
from .discovery import scan
from .portal import PortalClient,PortalError
from .reenrollment import ReenrollmentError,coordinated_reenroll


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
    try:
        coordinated_reenroll(config,activated,expected_cloud_id=config.cloud_id,
            vms_identity_path=Path(config.vms_recordings_path)/'appliance_identity.json',
            restart_service=restart_service,verify_authentication=verify_authentication)
    except ReenrollmentError as error: raise SystemExit(str(error)) from error
    if input('Run camera discovery now? [Y/n]: ').strip().lower()!='n':
        cameras=scan(config.discovery_networks); config.cameras_file.parent.mkdir(parents=True,exist_ok=True); config.cameras_file.write_text(json.dumps(cameras,indent=2),encoding='utf-8'); print(f'Discovered {len(cameras)} compatible endpoints.')
    print('Configuration saved securely.')
    print('AnyAiCam service restarted and authenticated during identity commit.')


if __name__=='__main__': main()
