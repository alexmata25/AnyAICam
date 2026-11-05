"""Runs the product's LPR functions over prepared frames, one pass at a time.

NOT TO BE RUN until the owner approves the field test. Test tooling, never
packaged. Run from the installed app folder, so lpr.py and its neighbours import:
  python field_probe.py <manifest.json> <frames dir> <results.jsonl> [alternate lpr.py]

Uses detect_plates_full_frame() and read_detected_plate(), then confirm_plate()
with the clip's own time, reset for each pass so one pass cannot help another.
This is the product's no-vehicle-box path. The vehicle-crop path needs the
vehicle detector and is not measured.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time

import containment

os.environ.setdefault('ANYAICAM_LPR_ENABLED', 'true')


def load_lpr(alternate: str | None):
    if not alternate:
        import lpr
        return lpr
    spec = importlib.util.spec_from_file_location('lpr', alternate)
    module = importlib.util.module_from_spec(spec)
    sys.modules['lpr'] = module
    spec.loader.exec_module(module)
    return module


def read_frame(lpr, frame) -> tuple[list, float]:
    started = time.perf_counter()
    plates = lpr.detect_plates_full_frame(frame)
    detect_ms = (time.perf_counter() - started) * 1000
    detections = []
    for plate in plates:
        x, y, w, h = plate['region']
        started = time.perf_counter()
        read = lpr.read_detected_plate(frame[y:y + h, x:x + w], plate['detector_confidence'])
        item = {'region': [x, y, w, h], 'detector_confidence': round(plate['detector_confidence'], 3),
                'ocr_ms': round((time.perf_counter() - started) * 1000, 1), 'plate_width_px': w}
        if read:
            item['text'], item['confidence'] = read
        detections.append(item)
    return detections, round(detect_ms, 1)


def engine_identity(lpr) -> dict:
    """Which engine produced these numbers. A field result never stands in for the OCR acceptance gate."""
    import hashlib
    import shutil

    adapter = lpr._get_pytesseract()
    command = adapter.pytesseract.tesseract_cmd
    path = shutil.which(command) or command
    with open(path, 'rb') as handle:
        engine = hashlib.sha256(handle.read()).hexdigest()
    with open(lpr.__file__, 'rb') as handle:
        source = hashlib.sha256(handle.read()).hexdigest()
    return {'engine_identity': True, 'tesseract_version': str(adapter.get_tesseract_version()),
            'tesseract_sha256': engine, 'lpr_sha256': source}


def forget_votes(lpr) -> None:
    # Not reset_pipeline_state(): that also unloads the model, and reloading it
    # for each pass would be counted as processing time.
    with lpr._votes_lock:
        lpr._votes.clear()
        lpr._emitted.clear()


def run(lpr, manifest: dict, frames_dir: str, out, imread) -> None:
    first = next((e['frames'][0]['file'] for e in manifest['passes'] if e['frames']), None)
    if first:
        lpr.detect_plates_full_frame(imread(os.path.join(frames_dir, first)))  # load the model before timing
    for entry in manifest['passes']:
        forget_votes(lpr)
        for frame_entry in entry['frames']:
            frame = imread(os.path.join(frames_dir, frame_entry['file']))
            detections, detect_ms = read_frame(lpr, frame)
            confirmed = []
            for item in detections:
                if item.get('text'):
                    result = lpr.confirm_plate(entry['pass_id'], {'plate_number': item['text'], 'confidence': item['confidence']},
                                               now=float(frame_entry['seconds']))
                    if result:
                        confirmed.append(result['plate_number'])
            out.write(json.dumps({'pass_id': entry['pass_id'], 'file': frame_entry['file'], 'detect_ms': detect_ms,
                                  'detections': detections, 'confirmed': confirmed}) + '\n')
            out.flush()


def main(argv: list[str]) -> int:
    import cv2
    lpr = load_lpr(argv[4] if len(argv) > 4 else None)
    available = lpr.capability()
    if not available.get('available'):
        raise SystemExit(f'LPR not available: {available}')
    with open(argv[1], encoding='utf-8') as handle:
        manifest = json.load(handle)
    with containment.open_new(argv[3], encoding='utf-8') as out:  # results hold plate text: new, and only in the private root
        out.write(json.dumps(engine_identity(lpr)) + '\n')
        run(lpr, manifest, argv[2], out, cv2.imread)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
