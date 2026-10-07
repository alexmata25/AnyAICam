import json
import subprocess

from .commands import diagnostics
from .config import AgentConfig
from . import windows
from .queue import OfflineQueue


def status_main():
    config=AgentConfig.load(); queue=OfflineQueue(config.queue_file); service=windows.service_state('AnyAiCamAgent') if windows.IS_WINDOWS else subprocess.run(['systemctl','is-active','anyaicam-agent.service'],capture_output=True,text=True,check=False).stdout.strip() or 'unknown'; print(json.dumps({'service':service,'cloud_id':config.cloud_id,'portal_url':config.portal_url,'mode':config.mode,'offline_queue':queue.count(),'credential':config.credential_file.exists()},indent=2))


def diagnostics_main():
    config=AgentConfig.load(); print(json.dumps(diagnostics(config,OfflineQueue(config.queue_file).count()),indent=2))
