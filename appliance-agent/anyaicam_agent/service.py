import json
import logging
import logging.handlers
import signal
import threading
import time
import uuid

from .commands import execute
from .config import AgentConfig,load_credential
from .discovery import scan
from .metrics import collect
from .portal import PortalClient,PortalError,sanitize
from .queue import OfflineQueue
from .updater.factory import build_update_state_machine
from .updater.restart import make_restart_signal


class ApplianceAgent:
    def __init__(self,config):
        self.config=config; credential=load_credential(config) or {}; self.client=PortalClient(config.portal_url,credential.get('appliance_id'),credential.get('credential')); self.queue=OfflineQueue(config.queue_file); self.stop_event=threading.Event(); self.log=logging.getLogger('anyaicam.agent')
        # RDM-2 Group 2A/2B: health_check is still left as
        # build_update_state_machine()'s own safe placeholder here --
        # Group 2F wires the real one. restart_signal is now the real
        # wrapper (Group 2B) around this same agent's own stop_event --
        # exactly the mechanism commands.py's existing restart_service
        # handler already uses. state_machine itself is constructed
        # unconditionally so resolve_update_state() can always run at
        # startup, regardless of whether this agent has ever processed
        # an install_update command.
        self.state_machine=build_update_state_machine(config,restart_signal=make_restart_signal(self.stop_event))
        self.update_resume_failed=False
    def resolve_update_state(self):
        # RDM-2 Group 2A: runs once at startup, before any command
        # processing -- per UpdateStateMachine.resume_if_pending()'s own
        # documented call-order requirement (resume_if_pending() first,
        # then sweep_orphaned_state()).
        #
        # If resume_if_pending() raises, this process must never allow a
        # NEW install_update to begin until a future restart re-evaluates
        # cleanly -- self.update_resume_failed is the interlock flag a
        # later group (2C) reads before dispatching install_update.
        # Nothing else about normal agent operation is affected: this
        # method never re-raises, so a broken update-resume can never
        # prevent heartbeat/camera/discovery/command-polling from
        # starting.
        try:
            result=self.state_machine.resume_if_pending()
            if result is not None:
                # Group 2E will replace this log line with a real report
                # call to the cloud (POST .../updates/{id}/result). Group
                # 2A only needs the sequencing and the interlock flag to
                # be correct -- reporting is a later group's concern.
                self.log.info('Update resume concluded: %s',result.as_dict())
        except Exception:
            self.log.exception('resume_if_pending() failed; blocking new install_update commands until next restart')
            self.update_resume_failed=True
        try:
            self.state_machine.sweep_orphaned_state()
        except Exception:
            self.log.exception('sweep_orphaned_state() failed; continuing startup')
    def cameras(self):
        try: return json.loads(self.config.cameras_file.read_text(encoding='utf-8'))
        except (OSError,json.JSONDecodeError): return []
    def send_or_queue(self,path,payload,item_id=None):
        try: self.client.request('POST',path,payload); return True
        except PortalError as error: self.queue.put(item_id or str(uuid.uuid4()),'POST',path,sanitize(payload)); self.log.warning('Queued offline update path=%s error=%s',path,error); return False
    def flush(self):
        for item in self.queue.ready():
            try: self.client.request(item['method'],item['path'],json.loads(item['payload_json'])); self.queue.success(item['id'])
            except PortalError: self.queue.fail(item['id'])
    def poll_discovery(self):
        try:
            response=self.client.request('GET',f'/api/appliance/{self.config.cloud_id}/scan-jobs')
            for job in response.get('jobs',[]):
                results=scan(self.config.discovery_networks); payload={'status':'complete','progress':100,'results':results,'message':f'Discovered {len(results)} compatible camera endpoints.'}; self.send_or_queue(f'/api/appliance/{self.config.cloud_id}/scan-jobs/{job["id"]}',payload,'scan-'+job['id'])
        except PortalError as error: self.log.debug('Discovery poll unavailable: %s',error)
    def poll_commands(self):
        try:
            for item in self.client.request('GET','/api/appliance/commands').get('commands',[]):
                status,result,error=execute(item['command'],item.get('payload',{}),self.config,self.stop_event,state_machine=self.state_machine,update_resume_failed=self.update_resume_failed); self.send_or_queue(f'/api/appliance/commands/{item["id"]}',{'status':status,'result':result,'error':error},'command-'+item['id'])
        except PortalError as error: self.log.debug('Command poll unavailable: %s',error)
    def sync_configuration(self):
        try:
            configuration=self.client.request('GET','/api/appliance/configuration'); existing={item.get('id'):item for item in self.cameras()}; merged=[]
            for item in configuration.get('cameras',[]):
                local=existing.get(item.get('id'),{}); merged.append({**local,**item,'online':local.get('online',False),'recording':local.get('recording',False),'analytics':local.get('analytics',False),'last_recording_at':local.get('last_recording_at'),'last_error':local.get('last_error')})
            if merged: self.config.cameras_file.parent.mkdir(parents=True,exist_ok=True); self.config.cameras_file.write_text(json.dumps(merged,indent=2),encoding='utf-8')
        except PortalError as error: self.log.debug('Configuration sync unavailable: %s',error)
    def cycle(self):
        self.sync_configuration(); cameras=self.cameras(); heartbeat=collect(self.config,cameras); self.send_or_queue('/api/appliance/heartbeat',heartbeat,'heartbeat-'+str(int(time.time())//self.config.checkin_seconds)); self.send_or_queue('/api/appliance/cameras',{'cameras':cameras},'cameras-'+str(int(time.time())//self.config.checkin_seconds)); self.flush(); self.poll_commands(); self.poll_discovery()
    def run(self):
        if not self.client.credential: raise RuntimeError('Appliance is not activated. Run anyaicam-setup first.')
        self.log.info('AnyAiCam appliance agent started cloud_id=%s mode=%s',self.config.cloud_id,self.config.mode)
        self.resolve_update_state()
        while not self.stop_event.is_set():
            try: self.cycle()
            except Exception: self.log.exception('Unhandled agent cycle error')
            self.stop_event.wait(max(10,self.config.checkin_seconds))
        self.log.info('AnyAiCam appliance agent stopped')


def configure_logging(config):
    config.log_dir and __import__('pathlib').Path(config.log_dir).mkdir(parents=True,exist_ok=True); handler=logging.handlers.RotatingFileHandler(__import__('pathlib').Path(config.log_dir)/'agent.log',maxBytes=5_000_000,backupCount=5); handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')); logging.basicConfig(level=logging.INFO,handlers=[handler,logging.StreamHandler()])


def main():
    config=AgentConfig.load(); configure_logging(config); agent=ApplianceAgent(config); signal.signal(signal.SIGTERM,lambda *_:agent.stop_event.set()); signal.signal(signal.SIGINT,lambda *_:agent.stop_event.set()); agent.run()


if __name__=='__main__': main()
