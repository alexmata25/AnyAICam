import getpass
import json
import shutil
import subprocess
from pathlib import Path

from .config import AgentConfig,save_credential
from .discovery import scan
from .portal import PortalClient,PortalError


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
    config.save(); save_credential(config,{'appliance_id':activated['appliance_id'],'credential_id':activated['credential_id'],'credential':activated['credential']})
    if input('Run camera discovery now? [Y/n]: ').strip().lower()!='n':
        cameras=scan(config.discovery_networks); config.cameras_file.parent.mkdir(parents=True,exist_ok=True); config.cameras_file.write_text(json.dumps(cameras,indent=2),encoding='utf-8'); print(f'Discovered {len(cameras)} compatible endpoints.')
    print('Configuration saved securely.')
    if input('Start AnyAiCam service now? [Y/n]: ').strip().lower()!='n': subprocess.run(['systemctl','start','anyaicam-agent.service'],check=False)


if __name__=='__main__': main()
