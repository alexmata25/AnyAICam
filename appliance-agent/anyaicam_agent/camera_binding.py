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


def _normalized_mac_or_raw(value):
    """Normalize a MAC when it's a real 12-digit address; otherwise keep
    the raw value (e.g. 'Unknown', '', None) as metadata rather than
    raising -- MAC is no longer required to identify a discovery record,
    only to key legacy records that predate device_key."""
    try:
        return normalize_mac(value)
    except ValueError:
        return value


def _is_missing(value) -> bool:
    return value is None or value == '' or value == 'Unknown'


def _record_key(camera: dict):
    """The stable identity for one discovered-camera record. device_key
    (the ONVIF endpoint UUID, or discovery.py's own MAC-hash/IP-hash
    fallback) is primary and, per discovery.py's scan(), always present
    on real scan output. MAC is used only as a fallback key for records
    that genuinely have no device_key at all (pre-device_key legacy
    entries) -- never as a requirement for saving a candidate. Returns
    None only when a record can't be identified by either, meaning
    there is nothing safe to key or merge it by."""
    device_key = str(camera.get('device_key') or '').strip()
    if device_key:
        return ('device_key', device_key)
    mac = _normalized_mac_or_raw(camera.get('mac_address', ''))
    if not _is_missing(mac):
        return ('mac', mac)
    return None


def _merge_camera(existing: dict, incoming: dict) -> dict:
    """Enrich, never regress: a later scan's freshly-resolved MAC/IP (or
    any other field) overwrites a previously-unknown value, but a field
    this scan couldn't resolve (e.g. ARP hadn't caught up yet) doesn't
    blank out a value an earlier scan already established."""
    merged = dict(existing)
    for key, value in incoming.items():
        if _is_missing(value) and not _is_missing(merged.get(key)):
            continue
        merged[key] = value
    return merged


class DiscoveredCameraStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def cameras(self) -> list[dict]:
        payload = _load_json(self.path, {'version': 1, 'cameras': []})
        return payload.get('cameras', []) if isinstance(payload, dict) else []

    def save_scan(self, cameras: list[dict]) -> None:
        # Keyed primarily by device_key (the ONVIF UUID / discovery.py's
        # own stable-identity fallback chain), not by MAC -- a candidate
        # with a real device_key but an ARP-timing "Unknown" MAC must
        # survive here, not be silently dropped. MAC/IP are optional
        # metadata: present when resolved, carried through as-is
        # (including the 'Unknown' sentinel) when not, and enriched in
        # place by a later scan via _merge_camera() rather than ever
        # creating a second record for the same device_key. Falling back
        # to MAC-as-key only covers records with no device_key at all
        # (pre-device_key legacy entries already on disk) -- never
        # required for a fresh scan result, since discovery.py's scan()
        # always emits one.
        by_key = {}
        order = []
        for camera in self.cameras():
            key = _record_key(camera)
            if key is None:
                continue  # cannot identify this stored record at all -- nothing to merge onto or re-key
            if key not in by_key:
                order.append(key)
            by_key[key] = {**camera, 'mac_address': _normalized_mac_or_raw(camera.get('mac_address', ''))}
        for camera in cameras:
            key = _record_key(camera)
            if key is None:
                continue  # neither a device_key nor a resolvable MAC -- no safe identity to store this candidate under
            candidate = {**camera, 'mac_address': _normalized_mac_or_raw(camera.get('mac_address', ''))}
            if key in by_key:
                by_key[key] = _merge_camera(by_key[key], candidate)
            else:
                by_key[key] = candidate
                order.append(key)
        atomic_write_json(self.path, {
            'version': 1,
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'cameras': [by_key[key] for key in order],
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


def auto_bind_discovered_cameras(cloud_cameras: list[dict], discovered_cameras: list[dict],
                                  binding_store: 'CameraBindingStore') -> list[str]:
    """Closes the other half of the camera_not_bound gap: once the cloud
    has assigned a camera_number to a provisioned camera (see app/
    appliance_cloud.py's appliance_submit_provisioning()), this creates
    the local binding automatically from data the appliance already has
    -- no rediscovery, no re-provisioning, no operator action. device_key
    is the sole matching key between a cloud camera and a discovered
    physical candidate (never MAC/IP/name), consistent with every other
    identity decision in this module.

    Idempotent by construction: bind() itself replaces (never
    duplicates) any prior binding for the same cloud_camera_id, and the
    up-front comparison against the current binding skips the write
    entirely when camera_number and MAC already match, so a binding
    that's already correct is never rewritten (no churned
    approved_at timestamp) and a binding for a camera not in
    cloud_cameras this cycle is never touched at all.

    Returns the list of cloud_camera_ids that were newly bound or
    re-bound this call, for logging -- never raises on a single
    camera's conflict (already-claimed MAC or camera_number); that
    camera is simply skipped and surfaces to the cloud via
    reconcile_cloud_cameras()'s own last_error instead.
    """
    discovered_by_device_key = {}
    for camera in discovered_cameras:
        device_key = str(camera.get('device_key') or '').strip()
        if device_key:
            discovered_by_device_key[device_key] = camera
    existing_by_cloud_id = {item.get('cloud_camera_id'): item for item in binding_store.bindings()}
    bound = []
    for cloud_camera in cloud_cameras:
        cloud_id = str(cloud_camera.get('id', '')).strip()
        camera_number = cloud_camera.get('camera_number')
        device_key = str(cloud_camera.get('device_key') or '').strip()
        if not cloud_id or camera_number is None or not device_key:
            continue  # nothing to bind yet -- no relay slot, or no physical identity to match against
        physical = discovered_by_device_key.get(device_key)
        if not physical:
            continue  # not (or not yet) seen on this appliance's own network scan
        try:
            mac_address = normalize_mac(physical.get('mac_address', ''))
        except ValueError:
            continue  # discovered record has no resolved MAC yet -- nothing safe to bind
        existing = existing_by_cloud_id.get(cloud_id)
        if existing and existing.get('camera_number') == camera_number and existing.get('mac_address') == mac_address:
            continue  # already correctly bound
        try:
            binding_store.bind(cloud_id, camera_number, mac_address)
            bound.append(cloud_id)
        except ValueError:
            continue  # camera_number or MAC already claimed by a different cloud camera's binding
    return bound


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
