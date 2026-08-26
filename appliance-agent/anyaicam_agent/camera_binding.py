import json
import os
from datetime import datetime, timezone
from pathlib import Path


LOCAL_ONLY_KEYS = {
    'ip', 'ip_address', 'host', 'mac', 'mac_address', 'rtsp_url', 'rtsp_urls',
    'stream_url', 'stream_urls', 'username', 'password', 'camera_username',
    'camera_password', 'credentials', 'secret', 'onvif_xaddrs',
}


def atomic_write_json(path: str | Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


def normalize_mac(value: str) -> str:
    compact = ''.join(character for character in str(value).lower() if character in '0123456789abcdef')
    if len(compact) != 12:
        raise ValueError('A valid 12-digit camera MAC address is required.')
    return ':'.join(compact[index:index + 2] for index in range(0, 12, 2))


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, TypeError):
        return default


class DiscoveredCameraStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def cameras(self) -> list[dict]:
        payload = _load_json(self.path, {'version': 1, 'cameras': []})
        return payload.get('cameras', []) if isinstance(payload, dict) else []

    def save_scan(self, cameras: list[dict]) -> None:
        by_mac = {}
        for camera in cameras:
            try:
                mac = normalize_mac(camera.get('mac_address', ''))
            except ValueError:
                continue
            by_mac[mac] = {**camera, 'mac_address': mac}
        atomic_write_json(self.path, {
            'version': 1,
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'cameras': list(by_mac.values()),
        })


class CameraBindingStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def bindings(self) -> list[dict]:
        payload = _load_json(self.path, {'version': 1, 'bindings': []})
        return payload.get('bindings', []) if isinstance(payload, dict) else []

    def bind(self, cloud_camera_id: str, camera_number: int, mac_address: str) -> dict:
        cloud_camera_id = str(cloud_camera_id).strip()
        if not cloud_camera_id:
            raise ValueError('cloud_camera_id is required.')
        if isinstance(camera_number, bool) or not isinstance(camera_number, int) or not 1 <= camera_number <= 256:
            raise ValueError('camera_number must be an integer between 1 and 256.')
        mac_address = normalize_mac(mac_address)
        retained = []
        for item in self.bindings():
            if item.get('cloud_camera_id') == cloud_camera_id:
                continue
            if normalize_mac(item.get('mac_address', '')) == mac_address:
                raise ValueError('This physical camera is already bound to another cloud camera.')
            if item.get('camera_number') == camera_number:
                raise ValueError('This local camera number is already bound to another cloud camera.')
            retained.append(item)
        binding = {
            'cloud_camera_id': cloud_camera_id,
            'camera_number': camera_number,
            'mac_address': mac_address,
            'approved_at': datetime.now(timezone.utc).isoformat(),
        }
        retained.append(binding)
        atomic_write_json(self.path, {'version': 1, 'bindings': retained})
        return binding


class LocalVmsStatusReader:
    """Read camera truth from VMS output files, never from RTSP discovery."""

    def __init__(self, hls_path: str | Path, recordings_path: str | Path,
                 stream_freshness_seconds: int = 20, recording_freshness_seconds: int = 360):
        self.hls_path = Path(hls_path)
        self.recordings_path = Path(recordings_path)
        self.stream_freshness_seconds = stream_freshness_seconds
        self.recording_freshness_seconds = recording_freshness_seconds

    @staticmethod
    def _fresh(path: Path, threshold: int, now: float) -> bool:
        try:
            return path.is_file() and now - path.stat().st_mtime <= threshold
        except OSError:
            return False

    def status(self, camera_number: int, now: float | None = None) -> dict:
        now = datetime.now(timezone.utc).timestamp() if now is None else now
        manifest = self.hls_path / f'camera{camera_number}.m3u8'
        recordings = []
        if self.recordings_path.exists():
            recordings = list(self.recordings_path.rglob(f'camera{camera_number}*.mkv'))
        fresh_recordings = [item for item in recordings if self._fresh(item, self.recording_freshness_seconds, now)]
        newest = max(fresh_recordings, key=lambda item: item.stat().st_mtime, default=None)
        return {
            'online': self._fresh(manifest, self.stream_freshness_seconds, now),
            'recording': newest is not None,
            'last_recording_at': (
                datetime.fromtimestamp(newest.stat().st_mtime, timezone.utc).isoformat()
                if newest else None
            ),
        }


def reconcile_cloud_cameras(cloud_cameras: list[dict], discovered_cameras: list[dict],
                            bindings: list[dict], status_reader: LocalVmsStatusReader) -> list[dict]:
    discovered_by_mac = {}
    for camera in discovered_cameras:
        try:
            discovered_by_mac[normalize_mac(camera.get('mac_address', ''))] = camera
        except ValueError:
            continue
    bindings_by_cloud_id = {item.get('cloud_camera_id'): item for item in bindings}
    reconciled = []
    for cloud_camera in cloud_cameras:
        cloud_id = str(cloud_camera.get('id', '')).strip()
        binding = bindings_by_cloud_id.get(cloud_id)
        safe = {key: value for key, value in cloud_camera.items() if key.lower() not in LOCAL_ONLY_KEYS}
        safe.update({'online': False, 'recording': False, 'analytics': False,
                     'last_recording_at': None, 'last_error': 'camera_not_bound'})
        if binding:
            camera_number = binding.get('camera_number')
            safe['camera_number'] = camera_number
            configured_number = cloud_camera.get('camera_number')
            if configured_number is not None and configured_number != camera_number:
                safe['last_error'] = 'binding_camera_number_mismatch'
                reconciled.append(safe)
                continue
            try:
                physical = discovered_by_mac.get(normalize_mac(binding.get('mac_address', '')))
            except ValueError:
                physical = None
            if not physical:
                safe['last_error'] = 'camera_not_discovered'
            else:
                vms_status = status_reader.status(camera_number)
                safe.update(vms_status)
                safe['last_error'] = None if vms_status['online'] else 'vms_stream_offline'
        reconciled.append(safe)
    return reconciled


def redact_discovery_for_cloud(cameras: list[dict]) -> list[dict]:
    redacted = []
    for index, camera in enumerate(cameras, 1):
        item = {
            key: value for key, value in camera.items()
            if key.lower() not in LOCAL_ONLY_KEYS and key.lower() not in {'id', 'name'}
        }
        item['id'] = f'candidate-{index}'
        item['name'] = f'Discovered camera {index}'
        redacted.append(item)
    return redacted
