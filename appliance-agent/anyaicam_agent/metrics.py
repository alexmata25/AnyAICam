import os
import shutil
import socket
import time
from pathlib import Path

from .updater import installer


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


def collect(config,cameras):
    # RDM-2 (device-side integration, Group 2A): config.software_version
    # is only the baseline recorded at agent-install time -- once an
    # update has ever been activated (RDM-1's installer.current_version()
    # pointer file exists), that is the true running version and must be
    # what every heartbeat reports. Falls back to config.software_version
    # only for a device that has never had an update activated.
    software_version=installer.current_version(config.current_version_pointer_file) or config.software_version
    disk=shutil.disk_usage('/'); recording=shutil.disk_usage(config.recording_path) if Path(config.recording_path).exists() else disk
    return {'software_version':software_version,'uptime_seconds':int(float(Path('/proc/uptime').read_text().split()[0])) if Path('/proc/uptime').exists() else 0,'cpu':_cpu_percent(),'memory':_memory_percent(),'disk_capacity':round(disk.total/1073741824,2),'disk_used':round(disk.used/1073741824,2),'recording_used':round(recording.used/1073741824,2),'ip_address':local_ip(),'camera_capacity':config.camera_capacity,'camera_count':len(cameras),'cameras':cameras,'last_error':None}
