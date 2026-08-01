import json
import platform
import subprocess
from datetime import datetime
from pathlib import Path

from .discovery import scan

ALLOWED={'restart_service','refresh_cameras','run_diagnostics','install_update'}


def diagnostics(config,queue_count=0):
    return {'timestamp':datetime.now().isoformat(),'hostname':platform.node(),'platform':platform.platform(),'python':platform.python_version(),'portal_url':config.portal_url,'mode':config.mode,'queue_depth':queue_count,'config_file_exists':(Path(config.config_dir)/'agent.json').exists(),'credential_exists':config.credential_file.exists()}


def execute(command,payload,config,stop_event=None):
    if command not in ALLOWED: return 'failed',{},'Unsupported command; arbitrary shell execution is disabled.'
    if command=='refresh_cameras': return 'completed',{'cameras':scan(config.discovery_networks)},''
    if command=='run_diagnostics': return 'completed',diagnostics(config),''
    if command=='restart_service':
        if stop_event: stop_event.set()
        return 'completed',{'restart_requested':True},''
    return 'failed',{},'Secure updater is not configured for this agent version.'
