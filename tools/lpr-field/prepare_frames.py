"""Turns copied event clips into frames for the field run. Offline; no engine.

windows.csv: pass_id,clip,start_seconds,end_seconds
Writes <out>/frames/<pass_id>_<index>.png and <out>/manifest.json with each
frame's time and SHA-256. Names carry no plate text. Never opens a camera.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from pathlib import Path

import containment

PASS_ID = re.compile(r'^[A-Za-z0-9_-]{1,40}$')
MAX_FRAMES = 400


def sample_times(start: float, end: float, per_second: float) -> list[float]:
    if end <= start or per_second <= 0:
        raise ValueError('window must have end > start and a positive rate')
    count = int((end - start) * per_second)
    return [round(start + index / per_second, 3) for index in range(count)]


def extract(clip: Path, times: list[float]):
    import cv2  # lazily: the planning and scoring paths need no OpenCV
    capture = cv2.VideoCapture(str(clip))
    if not capture.isOpened():
        raise ValueError(f'cannot open {clip.name}')
    try:
        for moment in times:
            capture.set(cv2.CAP_PROP_POS_MSEC, moment * 1000)
            ok, frame = capture.read()
            if ok:
                yield moment, frame
    finally:
        capture.release()


def prepare(windows_csv: Path, clips: Path, out: Path, per_second: float = 2.0, root: Path | None = None) -> dict:
    import cv2
    with open(windows_csv, newline='', encoding='utf-8') as handle:
        windows = list(csv.DictReader(handle))
    planned = sum(len(sample_times(float(w['start_seconds']), float(w['end_seconds']), per_second)) for w in windows)
    if planned > MAX_FRAMES:
        raise ValueError(f'{planned} frames planned; limit {MAX_FRAMES}')
    out, clips = containment.prepare_folder(out, root), containment.prepare_folder(clips, root)
    frames_dir = containment.prepare_folder(out / 'frames', root)
    manifest = {'frames_per_second': per_second, 'passes': []}
    for window in windows:
        if not PASS_ID.match(window['pass_id']):
            raise ValueError('bad pass_id')
        clip = (clips / window['clip']).resolve()
        if clip.parent != clips.resolve():
            raise ValueError('clip must be directly in the clips folder')
        entries = []
        times = sample_times(float(window['start_seconds']), float(window['end_seconds']), per_second)
        for index, (moment, frame) in enumerate(extract(clip, times)):
            target = frames_dir / f"{window['pass_id']}_{index:03d}.png"
            if not cv2.imwrite(str(target), frame):
                raise OSError(f'could not write {target.name}')
            entries.append({'file': target.name, 'seconds': moment,
                            'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
        manifest['passes'].append({'pass_id': window['pass_id'], 'clip': clip.name,
                                   'clip_sha256': hashlib.sha256(clip.read_bytes()).hexdigest(), 'frames': entries})
    containment.seal(frames_dir, root)  # the image encoder wrote these, so only now are they proven private
    with containment.open_new(out / 'manifest.json', root, encoding='utf-8') as handle:
        handle.write(json.dumps(manifest, indent=1))
    return manifest


if __name__ == '__main__':
    prepare(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
