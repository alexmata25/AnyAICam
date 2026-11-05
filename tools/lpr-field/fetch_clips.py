"""Copies owner-selected closed clips from the Ryzen, read-only, one at a time.

NOT TO BE RUN until the owner approves the copy. Test tooling, never packaged.
  python fetch_clips.py selection.csv <private folder> --owner-approved

selection.csv: pass_id,camera,clip,start_seconds,end_seconds
Reads through `docker exec`, because the host folders are root-only. Nothing on
the Ryzen is written. Each clip is hash-checked on both sides. The local Ryzen
logger gates every copy, so a hot or silent machine stops the run.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import containment

HOST = 'ryzen-tailscale'
CONTAINER = 'anyaicam-vms'
LOGGER = Path(r'C:\anyaicam-monitor\ryzen\ryzen-monitor.jsonl')
CLIP = re.compile(r'^camera(\d+)_(\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d)\.(mkv|mp4)$')
CAMERAS = {'2', '3'}
MAX_CLIPS = 40
MAX_BYTES = 500_000_000
MAX_CLIP_BYTES = 60_000_000
KEEP_FREE_BYTES = 8 * 1024 ** 3  # Red's D-2 needs this on C:
COPY_TIMEOUT_SECONDS = 300
MIN_AGE = timedelta(minutes=10)
MAX_LOGGER_AGE = timedelta(minutes=5)
START_AT_MOST_C = 70.0
STOP_AT_C = 80.0


def check_selection(rows: list[dict], now: datetime, cameras: set = CAMERAS) -> list[tuple[str, str]]:
    if not rows or len(rows) > MAX_CLIPS:
        raise ValueError(f'{len(rows)} clips selected; 1 to {MAX_CLIPS} allowed')
    clips = []
    for row in rows:
        camera, clip = row['camera'].strip(), row['clip'].strip()
        match = CLIP.match(clip)
        if not match or match.group(1) != camera:  # This text reaches a remote shell, so it must be exact.
            raise ValueError(f'not a clip of camera {camera}: {clip!r}')
        if camera not in cameras:
            raise ValueError(f'camera {camera} is not approved for this test')
        stamp = datetime.strptime(match.group(2), '%Y-%m-%d_%H-%M-%S').replace(tzinfo=timezone.utc)
        if now - stamp < MIN_AGE:
            raise ValueError(f'{clip} may still be open')
        if (camera, clip) not in clips:
            clips.append((camera, clip))
    return clips


def receive(command: list[str], out, limit: int, popen) -> int:
    """Writes the stream to out and stops it as soon as it passes the limit."""
    process = popen(command, stdout=subprocess.PIPE)
    watchdog = threading.Timer(COPY_TIMEOUT_SECONDS, process.kill)
    watchdog.start()
    written = 0
    try:
        while chunk := process.stdout.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                raise RuntimeError(f'stopped: the stream passed the {limit} byte limit')
            out.write(chunk)
        if process.wait() != 0:
            raise RuntimeError('the copy did not finish')
    finally:
        watchdog.cancel()
        process.kill()
        process.wait()
    return written


def last_line(path: Path, size: int = 8192) -> str:
    with open(path, 'rb') as handle:
        handle.seek(0, 2)
        handle.seek(max(0, handle.tell() - size))
        return handle.read().decode('utf-8', 'replace').strip().splitlines()[-1]


def temperature_problem(record: dict, now: datetime) -> str | None:
    seen = datetime.strptime(record['at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    if now - seen > MAX_LOGGER_AGE:
        return 'the Ryzen logger is stale'
    tctl = record.get('cpu_tctl_c')
    if tctl is None:
        return 'the Ryzen logger has no temperature'
    return f'Tctl {tctl} C is above the {START_AT_MOST_C} C start limit' if tctl > START_AT_MOST_C else None


def remote(*command: str) -> list[str]:
    return ['ssh', '-o', 'BatchMode=yes', HOST, 'docker', 'exec', CONTAINER, 'nice', '-n', '19', *command]


def copy_clip(camera: str, clip: str, folder: Path, run, popen, budget: int, root: Path | None) -> dict:
    source = f'/app/recordings/camera{camera}/{clip}'
    target, partial = folder / clip, folder / f'{clip}.part'
    if target.exists():
        raise ValueError(f'{clip} is already in the folder')
    partial.unlink(missing_ok=True)
    try:
        with containment.open_new(partial, root, 'xb') as out:
            receive(remote('cat', source), out, min(MAX_CLIP_BYTES, budget), popen)
        wanted = run(remote('sha256sum', source), capture_output=True, text=True, check=True, timeout=120).stdout.split()[0]
        digest = hashlib.sha256(partial.read_bytes()).hexdigest()
        if digest != wanted:
            raise ValueError(f'{clip}: hash differs; the clip may have changed')
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return {'clip': clip, 'sha256': digest, 'bytes': target.stat().st_size}


def fetch(rows: list[dict], folder: Path, *, run=subprocess.run, popen=subprocess.Popen, read_temperature=None, now=None,
          cameras=CAMERAS, root=None, free_bytes=lambda path: shutil.disk_usage(path).free) -> list[dict]:
    now = now or (lambda: datetime.now(timezone.utc))
    read_temperature = read_temperature or (lambda: json.loads(last_line(LOGGER)))
    clips = check_selection(rows, now(), cameras)
    folder = containment.prepare_folder(folder, root)
    if free_bytes(folder) < MAX_BYTES + KEEP_FREE_BYTES:
        raise RuntimeError(f'stopped: under {MAX_BYTES + KEEP_FREE_BYTES} bytes free')
    copied, total = [], 0
    for camera, clip in clips:
        problem = temperature_problem(read_temperature(), now())
        if problem:
            raise RuntimeError(f'stopped before {clip}: {problem}; {len(copied)} copied')
        copied.append(copy_clip(camera, clip, folder, run, popen, MAX_BYTES - total, root))
        total += copied[-1]['bytes']
    containment.seal(folder, root)
    return copied


def main(argv: list[str]) -> int:
    if '--owner-approved' not in argv:
        raise SystemExit('Refused: this copy needs the owner\'s approval of the clip list.')
    with open(argv[1], newline='', encoding='utf-8') as handle:
        rows = list(csv.DictReader(handle))
    folder = Path(argv[2])
    cameras = CAMERAS | ({'5'} if '--with-camera-5' in argv else set())
    copied = fetch(rows, folder, cameras=cameras)
    with containment.open_new(folder / 'clips-copied.json', encoding='utf-8') as out:
        out.write(json.dumps(copied, indent=2))
    print(f'{len(copied)} clips, {sum(item["bytes"] for item in copied)} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
