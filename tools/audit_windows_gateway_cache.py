"""Bounded, read-only audit of pinned filenames/manifests and explicit local caches."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.audit_windows_gateway_prerequisites import locked_wheels, pinned_inputs


def normalize(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def requirements(source):
    result = {}
    for line in source.splitlines():
        match = re.match(r'^([A-Za-z0-9_.-]+)==([^\s;#]+)', line.strip())
        if match:
            result[normalize(match[1])] = match[2]
    return result


def inventory(roots, wanted, max_dirs=3000, max_depth=6):
    found = {name: [] for name in wanted}
    scans = []
    for root in roots:
        root = Path(root)
        record = {'root': str(root), 'exists': root.is_dir(), 'directories_scanned': 0, 'errors': [], 'truncated': False}
        if record['exists']:
            def failed(error):
                record['errors'].append(type(error).__name__)
            for folder, dirs, files in os.walk(root, followlinks=False, onerror=failed):
                record['directories_scanned'] += 1
                if record['directories_scanned'] > max_dirs:
                    record['truncated'] = True
                    break
                depth = len(Path(folder).relative_to(root).parts)
                if depth >= max_depth:
                    if dirs:
                        record['truncated'] = True
                    dirs[:] = []
                # No archive extraction or unpacked-package reconstruction.
                for name in set(files) & wanted:
                    found[name].append(str(Path(folder) / name))
        scans.append(record)
    return found, scans


def bounded_match(path, expected, max_bytes=16 * 1024 * 1024):
    try:
        if Path(path).stat().st_size > max_bytes:
            return 'hash_deferred_size_limit'
        digest = hashlib.sha256()
        with open(path, 'rb') as handle:
            total = 0
            while chunk := handle.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    return 'hash_deferred_size_limit'
                digest.update(chunk)
        return 'hash_matches' if digest.hexdigest() == expected else 'hash_mismatch'
    except OSError:
        return 'unreadable'


def audit(root, cache_roots):
    win = root / 'installer/windows'
    windows = requirements((win / 'requirements-windows.txt').read_text(encoding='utf-8-sig'))
    top = {}
    for name in ('requirements.txt', 'requirements-cpu.txt', 'requirements-push.txt'):
        top.update(requirements((root / name).read_text(encoding='utf-8-sig')))
    locks = {}
    lock_source = (win / 'wheels.lock.sha256').read_text(encoding='utf-8-sig')
    locked_wheels(lock_source)
    for line in lock_source.splitlines():
        digest, name = line.split('  ', 1)
        locks[name] = digest
    build_source = (win / 'build.ps1').read_text(encoding='utf-8-sig')
    vendor_names = pinned_inputs(build_source)
    vendor_pins = {name: re.search(r"^\s*'" + re.escape(name) + r"'\s*=\s*'([0-9a-f]{64})'", build_source, re.MULTILINE)[1]
                   for name in vendor_names}
    found, scans = inventory(cache_roots, set(locks) | set(vendor_names))
    vendor_rows = [{'filename': name, 'sha256': pin,
                    'candidates': [{'path': path, 'verification': bounded_match(path, pin)} for path in found[name]]}
                   for name, pin in vendor_pins.items()]
    rows = []
    for filename, pin in locks.items():
        dist, version = filename.split('-')[:2]
        name = normalize(dist)
        top_version = top.get(name, '')
        cpu_variant = name in {'torch', 'torchvision'} and version.split('+')[0] == top_version
        rows.append({'filename': filename, 'sha256': pin, 'windows_version': windows.get(name, ''),
            'windows_manifest_matches': windows.get(name) == version, 'top_level_version': top_version,
            'top_level_matches': not top_version or top_version == version or cpu_variant,
            'candidates': [{'path': path, 'verification': bounded_match(path, pin)} for path in found[filename]]})
    locked_names = {normalize(name.split('-')[0]) for name in locks}
    return {'kind': 'bounded_local_cache_and_manifest_audit', 'cache_scans': scans, 'wheel_count': len(rows),
        'windows_requirement_count': len(windows), 'requirements_without_wheel': sorted(set(windows) - locked_names),
        'windows_manifest_mismatches': sum(not row['windows_manifest_matches'] for row in rows),
        'top_level_mismatches': sum(not row['top_level_matches'] for row in rows), 'wheels': rows,
        'vendor_inputs': vendor_rows, 'copied_files': 0, 'downloads': 0,
        'limits': 'Exact filenames only; no symlink following or archive reconstruction; 16 MiB hash limit per candidate. Partial scans do not prove global absence.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-root', action='append', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--csv', type=Path, required=True)
    args = parser.parse_args()
    report = audit(ROOT, args.cache_root)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    with args.csv.open('w', newline='', encoding='utf-8') as handle:
        columns = ['filename', 'sha256', 'windows_version', 'windows_manifest_matches', 'top_level_version', 'top_level_matches', 'candidate_count']
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in report['wheels']:
            writer.writerow({**{k: row[k] for k in columns if k != 'candidate_count'}, 'candidate_count': len(row['candidates'])})
    print(json.dumps({k: report[k] for k in ('wheel_count', 'windows_requirement_count', 'requirements_without_wheel', 'windows_manifest_mismatches', 'top_level_mismatches')}))
    print('Exact-name wheel candidates:', sum(len(row['candidates']) for row in report['wheels']))
    print('Exact-name vendor candidates:', sum(len(row['candidates']) for row in report['vendor_inputs']))


if __name__ == '__main__':
    main()
