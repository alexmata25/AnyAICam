import json
import os
import shutil
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

# Local recording storage management (2026-09-17): the VMS app (a
# separate, containerized process from this agent) publishes its own
# port to the host -- reachable over plain localhost HTTP, same host,
# no container-network translation needed. Configurable for the rare
# case a future deployment maps a different host port; the VMS
# container's own installer-provisioned mapping is 8000 by default.
VMS_LOCAL_URL = os.environ.get("ANYAICAM_VMS_LOCAL_URL", "http://127.0.0.1:8000").rstrip("/")


def _cpu_percent(sample=.15):
    def read():
        values=[int(item) for item in Path('/proc/stat').read_text().splitlines()[0].split()[1:]]; return sum(values),values[3]+values[4]
    try:
        total1,idle1=read(); time.sleep(sample); total2,idle2=read(); return round(100*(1-(idle2-idle1)/max(1,total2-total1)),1)
    except (OSError,ValueError): return 0.0


def _memory_percent():
    try:
        values={line.split(':')[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}; return round(100*(1-values.get('MemAvailable',0)/max(1,values['MemTotal'])),1)
    except (OSError,ValueError,KeyError): return 0.0


def local_ip():
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    try: sock.connect(('8.8.8.8',80)); return sock.getsockname()[0]
    except OSError: return '127.0.0.1'
    finally: sock.close()


def disk_summary(config):
    # Factored out of collect() (RDM4) so run_diagnostics' on-demand
    # snapshot uses the exact same disk-accounting logic as the regular
    # heartbeat, instead of a second, potentially-drifting copy of it.
    disk=shutil.disk_usage('/'); recording=shutil.disk_usage(config.recording_path) if Path(config.recording_path).exists() else disk
    return {'disk_capacity':round(disk.total/1073741824,2),'disk_used':round(disk.used/1073741824,2),'recording_used':round(recording.used/1073741824,2)}


def local_storage_state(config):
    """Local recording storage management (2026-09-17): polls the VMS
    app's own local status route (GET /api/appliance/local-storage-state,
    main.py) over plain localhost HTTP -- returns {} (no keys added to
    the heartbeat payload at all) whenever that call fails for any
    reason (VMS app down/restarting, feature not enabled there yet, old
    VMS build predating this route), which is the normal, expected state
    for any appliance that hasn't enabled
    ANYAICAM_LOCAL_STORAGE_MANAGEMENT_ENABLED yet. Never raises.

    A prior design had this read a small state FILE
    local_storage_manager.py wrote into STATE_DIR -- replaced after
    confirming live on Ryzen that STATE_DIR (/var/lib/anyaicam) is
    mounted READ-ONLY inside the VMS container by design (it may read
    the appliance's own credential/identity files there, but must never
    write into that directory), so that file write failed on every
    single tick. An HTTP status call has no such conflict and is always
    live, never stale."""
    try:
        with urllib.request.urlopen(f"{VMS_LOCAL_URL}/api/appliance/local-storage-state", timeout=3) as response:
            data=json.loads(response.read().decode() or "{}")
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data,dict):
        return {}
    result={}
    if data.get('storage_state') in ('healthy','warning','cleanup_active','critical'):
        result['storage_state']=data['storage_state']
    if isinstance(data.get('free_percent'),(int,float)):
        result['storage_free_percent']=data['free_percent']
    if isinstance(data.get('last_cleanup_at'),str):
        result['storage_last_cleanup_at']=data['last_cleanup_at']
    return result


def collect(config,cameras):
    return {'software_version':config.software_version,'uptime_seconds':int(float(Path('/proc/uptime').read_text().split()[0])) if Path('/proc/uptime').exists() else 0,'cpu':_cpu_percent(),'memory':_memory_percent(),**disk_summary(config),**local_storage_state(config),'ip_address':local_ip(),'camera_capacity':config.camera_capacity,'camera_count':len(cameras),'cameras':cameras,'last_error':None}
