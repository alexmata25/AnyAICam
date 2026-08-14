import json
import os
import platform
import subprocess
from datetime import datetime
from pathlib import Path

from .discovery import scan

ALLOWED={'restart_service','refresh_cameras','run_diagnostics','install_update','start_live_relay','stop_live_relay'}


def diagnostics(config,queue_count=0):
    return {'timestamp':datetime.now().isoformat(),'hostname':platform.node(),'platform':platform.platform(),'python':platform.python_version(),'portal_url':config.portal_url,'mode':config.mode,'queue_depth':queue_count,'config_file_exists':(Path(config.config_dir)/'agent.json').exists(),'credential_exists':config.credential_file.exists()}


def _validate_relay_camera_number(value):
    # Phase 4 (docs/AI_HANDOFF.md §8): bool is a subclass of int in Python --
    # explicitly reject True/False so a stray boolean can never masquerade as
    # camera_number 0/1. appliance-agent has no access to the real
    # CAMERA_COUNT (that config lives only in the separate app/main.py
    # process), so this is a generic sanity bound, not an authoritative range.
    if isinstance(value,bool): return None
    try: number=int(value)
    except (TypeError,ValueError): return None
    return number if 1<=number<=256 else None


def _validate_relay_camera_id(value):
    if not isinstance(value,str): return None
    trimmed=value.strip()
    return trimmed if trimmed and len(trimmed)<=128 else None


def _set_relay_command(config,payload,active):
    # Phase 4: durably records desired relay state for the VMS app process (a
    # separate process -- see app/live_relay_uploader.py) to reconcile on its
    # own next tick. This function never calls set_relay_active() itself --
    # it cannot, it runs in a different process. Never raises: any problem is
    # reported back as a normal 'failed' result, matching every other command
    # here. Never includes camera_id, credentials, or secrets in error text.
    if not isinstance(payload,dict): payload={}
    camera_number=_validate_relay_camera_number(payload.get('camera_number'))
    camera_id=_validate_relay_camera_id(payload.get('camera_id'))
    if camera_number is None or camera_id is None:
        return 'failed',{},'camera_number and camera_id are required and must be valid.'
    path=config.live_relay_commands_file
    try:
        commands=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(commands,dict): commands={}
    except (OSError,json.JSONDecodeError):
        commands={}
    commands[str(camera_number)]={'camera_id':camera_id,'active':bool(active)}
    try:
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(commands),encoding='utf-8')
        os.chmod(temporary,0o600)
        temporary.replace(path)
        os.chmod(path,0o600)
    except OSError:
        return 'failed',{},'Could not record live relay command due to a local storage error.'
    return 'completed',{'camera_number':camera_number,'active':bool(active)},''


def execute(command,payload,config,stop_event=None):
    if command not in ALLOWED: return 'failed',{},'Unsupported command; arbitrary shell execution is disabled.'
    if command=='refresh_cameras': return 'completed',{'cameras':scan(config.discovery_networks)},''
    if command=='run_diagnostics': return 'completed',diagnostics(config),''
    if command=='start_live_relay': return _set_relay_command(config,payload,True)
    if command=='stop_live_relay': return _set_relay_command(config,payload,False)
    if command=='restart_service':
        if stop_event: stop_event.set()
        return 'completed',{'restart_requested':True},''
    return 'failed',{},'Secure updater is not configured for this agent version.'
