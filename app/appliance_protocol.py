import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ALLOWED_COMMANDS = {'restart_service','refresh_cameras','run_diagnostics','install_update'}
FORBIDDEN_KEYS = {'username','password','camera_username','camera_password','rtsp_url','credentials','secret'}
LIVE_RELAY_SESSION_DURATION_SECONDS = 900  # V1 credential lifetime (docs/AI_HANDOFF.md Phase 1 decision 4)
_SESSION_NAME_SAFE = re.compile(r'[^\w+=,.@-]')


def validate_request_time(timestamp: int, now: int | None = None, window_seconds: int = 300) -> bool:
    return abs((now or int(time.time())) - int(timestamp)) <= window_seconds


def sanitize_appliance_payload(value):
    if isinstance(value, dict):
        return {key: sanitize_appliance_payload(item) for key,item in value.items() if key.lower() not in FORBIDDEN_KEYS}
    if isinstance(value, list): return [sanitize_appliance_payload(item) for item in value]
    return value


def health_state(payload: dict) -> tuple[str,list[str]]:
    warnings=[]
    if float(payload.get('disk_capacity',0)) and float(payload.get('disk_used',0))/float(payload['disk_capacity']) >= .9: warnings.append('low_disk')
    if float(payload.get('cpu',0)) >= 90: warnings.append('high_cpu')
    for camera in payload.get('cameras',[]):
        if not camera.get('online',False): warnings.append('camera_offline')
        elif not camera.get('recording',False): warnings.append('recording_stopped')
    if payload.get('outdated_software'): warnings.append('outdated_software')
    return ('degraded' if warnings else 'online'),sorted(set(warnings))


class RateLimiter:
    def __init__(self,limit=120,window_seconds=60): self.limit=limit; self.window=window_seconds; self.events={}; self.lock=threading.Lock()
    def allow(self,key: str,now: float | None=None) -> bool:
        now=now or time.time(); cutoff=now-self.window
        with self.lock:
            recent=[item for item in self.events.get(key,[]) if item>=cutoff]
            if len(recent)>=self.limit: self.events[key]=recent; return False
            recent.append(now); self.events[key]=recent; return True


class OfflineQueue:
    def __init__(self,path: str | Path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        with self._db() as db: db.execute('CREATE TABLE IF NOT EXISTS queue(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload_json TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL,created_at REAL NOT NULL)')
    @contextmanager
    def _db(self):
        db=sqlite3.connect(self.path)
        try: yield db; db.commit()
        finally: db.close()
    def enqueue(self,item_id: str,kind: str,payload: dict) -> bool:
        with self._db() as db:
            cursor=db.execute('INSERT OR IGNORE INTO queue(id,kind,payload_json,next_attempt,created_at) VALUES(?,?,?,?,?)',(item_id,kind,json.dumps(sanitize_appliance_payload(payload)),time.time(),time.time())); return cursor.rowcount==1
    def ready(self,limit=100,now=None) -> list[dict]:
        with self._db() as db:
            db.row_factory=sqlite3.Row; return [dict(item) for item in db.execute('SELECT * FROM queue WHERE next_attempt<=? ORDER BY created_at LIMIT ?',(now or time.time(),limit))]
    def succeeded(self,item_id: str) -> None:
        with self._db() as db: db.execute('DELETE FROM queue WHERE id=?',(item_id,))
    def failed(self,item_id: str,now=None) -> float:
        with self._db() as db:
            row=db.execute('SELECT attempts FROM queue WHERE id=?',(item_id,)).fetchone(); attempts=(row[0] if row else 0)+1; delay=min(3600,2**min(attempts,11)); next_attempt=(now or time.time())+delay; db.execute('UPDATE queue SET attempts=?,next_attempt=? WHERE id=?',(attempts,next_attempt,item_id)); return delay


def cloud_settings() -> dict:
    mode=os.getenv('ANYAICAM_CLOUD_MODE','local').lower()
    return {'mode':mode,'base_url':os.getenv('ANYAICAM_CLOUD_URL','http://host.docker.internal:8000' if mode in {'local','mock'} else ''),'mock':mode=='mock'}


def live_relay_s3_prefix(customer_id: str,site_id: str,appliance_id: str,camera_id: str) -> str:
    # docs/AI_HANDOFF.md Phase 1 template C: the exact prefix every issued session is scoped to.
    return f'live/{customer_id}/{site_id}/{appliance_id}/{camera_id}/'


def live_relay_session_policy(bucket: str,customer_id: str,site_id: str,appliance_id: str,camera_id: str) -> dict:
    # docs/AI_HANDOFF.md Phase 1 template C, filled in per-request. Passed as the `Policy`
    # parameter on sts:assume_role -- narrows the shared live-upload role down to exactly
    # one camera's prefix for this session. Verified in AWS during Phase 1 execution:
    # PutObject to this prefix succeeds, PutObject to any other camera's prefix is denied.
    prefix=live_relay_s3_prefix(customer_id,site_id,appliance_id,camera_id)
    return {'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'s3:PutObject','Resource':f'arn:aws:s3:::{bucket}/{prefix}*'}]}


def live_relay_session_name(appliance_id: str,camera_id: str,now: int | None=None) -> str:
    # CloudTrail-readable and within AWS's RoleSessionName rules ([\w+=,.@-]{2,64}).
    raw=f'{appliance_id}-{camera_id}-{now or int(time.time())}'
    return _SESSION_NAME_SAFE.sub('-',raw)[:64]
