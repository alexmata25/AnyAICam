import json
import subprocess

from .commands import diagnostics
from .config import AgentConfig
from .queue import OfflineQueue


def status_main():
    config=AgentConfig.load(); queue=OfflineQueue(config.queue_file); result=subprocess.run(['systemctl','is-active','anyaicam-agent.service'],capture_output=True,text=True,check=False); print(json.dumps({'service':result.stdout.strip() or 'unknown','cloud_id':config.cloud_id,'portal_url':config.portal_url,'mode':config.mode,'offline_queue':queue.count(),'credential':config.credential_file.exists()},indent=2))


def diagnostics_main():
    config=AgentConfig.load(); print(json.dumps(diagnostics(config,OfflineQueue(config.queue_file).count()),indent=2))
