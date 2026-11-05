"""Scores an LPR field run. Standard library only; touches no camera or engine.

Inputs: ground-truth.csv (owner, by eye) and results.jsonl (field_probe.py).
Output never contains plate text: a report can leave the private folder, the
ground truth and results cannot.

Ground truth, one row per vehicle pass or empty pass:
  pass_id,camera,light,plate,human_legible,authorized
  plate: exact text, several as A|B, empty for a pass with no vehicle plate
  light: day, dusk or night. human_legible, authorized: y or n
"""
from __future__ import annotations

import csv
import json
import math
from collections import defaultdict


def normalize(text: str) -> str:
    return ''.join(ch for ch in str(text or '').upper() if ch.isalnum())


def edits(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def wilson(hits: int, total: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% interval for a rate. A small field sample is wide; report it."""
    if total == 0:
        return None
    p = hits / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return round(max(0.0, centre - margin), 3), round(min(1.0, centre + margin), 3)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(fraction * len(ordered)))], 1)


def load_truth(path) -> dict:
    truth = {}
    with open(path, newline='', encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            if row['pass_id'] in truth:
                raise ValueError(f"duplicate pass_id {row['pass_id']}")
            if row['authorized'].strip().lower() != 'y':
                raise ValueError(f"unauthorized pass {row['pass_id']} must be purged, not scored")
            truth[row['pass_id']] = {
                'camera': row['camera'], 'light': row['light'].strip().lower(),
                'plates': {normalize(p) for p in row['plate'].split('|') if normalize(p)},
                'legible': row['human_legible'].strip().lower() == 'y',
            }
    return truth


def load_results(path) -> dict:
    grouped = defaultdict(list)
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            if line.strip():
                record = json.loads(line)
                if not record.get('engine_identity'):
                    grouped[record['pass_id']].append(record)
    return grouped


def score_pass(plates: set, frames: list) -> dict:
    """One pass. Reads come per frame; confirmed reads went through the
    product's own confirm_plate()."""
    reads = {normalize(d.get('text')) for f in frames for d in f.get('detections', []) if d.get('text')}
    confirmed = {normalize(c) for f in frames for c in f.get('confirmed', [])}
    detected = any(f.get('detections') for f in frames)
    if not plates:
        return {'detected': detected, 'read_exact': False, 'confirmed_exact': False, 'near': False,
                'wrong_confirmed': len(confirmed), 'char_accuracy': None}
    best = min((edits(read, plate) / max(len(plate), 1) for read in reads for plate in plates), default=1.0)
    return {
        'detected': detected,
        'read_exact': bool(reads & plates),
        'confirmed_exact': bool(confirmed & plates),
        'near': any(edits(read, plate) <= 1 for read in reads for plate in plates),
        'wrong_confirmed': len(confirmed - plates),
        'char_accuracy': round(max(0.0, 1.0 - best), 3),
    }


def _rate(hits: int, total: int) -> dict:
    return {'hits': hits, 'total': total, 'rate': round(hits / total, 3) if total else None, 'ci95': wilson(hits, total)}


def summarize(truth: dict, results: dict) -> dict:
    missing = sorted(set(truth) - set(results))
    extra = sorted(set(results) - set(truth))
    if extra:
        raise ValueError(f'results without ground truth: {len(extra)} passes')
    scored = {pass_id: score_pass(truth[pass_id]['plates'], results.get(pass_id, [])) for pass_id in truth}

    def block(ids: list) -> dict:
        positive = [i for i in ids if truth[i]['plates'] and truth[i]['legible']]
        negative = [i for i in ids if not truth[i]['plates']]
        accuracy = [scored[i]['char_accuracy'] for i in positive]
        return {
            'legible_passes': len(positive), 'empty_passes': len(negative),
            'illegible_passes_excluded': sum(1 for i in ids if truth[i]['plates'] and not truth[i]['legible']),
            'plate_found': _rate(sum(scored[i]['detected'] for i in positive), len(positive)),
            'missed_detection': _rate(sum(not scored[i]['detected'] for i in positive), len(positive)),
            'exact_any_frame': _rate(sum(scored[i]['read_exact'] for i in positive), len(positive)),
            'exact_confirmed': _rate(sum(scored[i]['confirmed_exact'] for i in positive), len(positive)),
            'within_one_character': _rate(sum(scored[i]['near'] for i in positive), len(positive)),
            'mean_character_accuracy': round(sum(accuracy) / len(accuracy), 3) if accuracy else None,
            'wrong_plate_confirmed': _rate(sum(scored[i]['wrong_confirmed'] > 0 for i in ids), len(ids)),
            'false_detection_on_empty': _rate(sum(scored[i]['detected'] for i in negative), len(negative)),
        }

    frames = [f for records in results.values() for f in records]
    detect = [f['detect_ms'] for f in frames if 'detect_ms' in f]
    ocr = [d['ocr_ms'] for f in frames for d in f.get('detections', []) if 'ocr_ms' in d]
    by = defaultdict(list)
    for pass_id, item in truth.items():
        by[f"camera {item['camera']}"].append(pass_id)
        by[f"light {item['light']}"].append(pass_id)
    return {
        'overall': block(list(truth)),
        'by_group': {name: block(ids) for name, ids in sorted(by.items())},
        'frames': len(frames),
        'passes_without_results': len(missing),
        'detect_ms': {'p50': percentile(detect, 0.5), 'p95': percentile(detect, 0.95)},
        'ocr_ms': {'p50': percentile(ocr, 0.5), 'p95': percentile(ocr, 0.95)},
    }


def main(argv: list[str]) -> int:
    report = summarize(load_truth(argv[1]), load_results(argv[2]))
    print(json.dumps(report, indent=1))
    return 0


if __name__ == '__main__':
    import sys
    raise SystemExit(main(sys.argv))
