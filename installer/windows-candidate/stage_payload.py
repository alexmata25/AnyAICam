"""Offline build preparation only. Never installs, starts services, or downloads."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

REQUIRED = {'InstallRuntime.exe', 'ConnectAnyAiCam.exe'}


def stage_payload(payload, output, source_commit):
    payload, output = Path(payload), Path(output)
    if not re.fullmatch(r'[0-9a-f]{40}', source_commit):
        raise ValueError('A full pinned source commit is required.')
    manifest = json.loads((payload / 'payload.json').read_text(encoding='utf-8'))
    if manifest.get('platform') != 'windows-x64' or manifest.get('source_commit') != source_commit:
        raise ValueError('Payload platform or pinned source commit does not match.')
    if not re.fullmatch(r'\d+\.\d+\.\d+', str(manifest.get('version', ''))):
        raise ValueError('A numeric three-part version is required.')
    if output.exists() or output.resolve().is_relative_to(payload.resolve()):
        raise ValueError('Use a new output directory outside the payload.')
    entries = manifest.get('files')
    if not isinstance(entries, list) or not entries:
        raise ValueError('Missing file integrity manifest.')
    paths = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('Invalid file entry.')
        relative = entry.get('path', '')
        if not isinstance(relative, str) or not re.fullmatch(r'[A-Za-z0-9_./-]+', relative):
            raise ValueError('Unsafe payload path.')
        path = payload / relative
        if (relative in paths or Path(relative).is_absolute() or '..' in Path(relative).parts
                or relative == 'payload.json' or path.is_symlink()
                or not path.resolve().is_relative_to(payload.resolve()) or not path.is_file()):
            raise ValueError('Unsafe or duplicate payload file.')
        with path.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        if digest != entry.get('sha256'):
            raise ValueError('Payload checksum mismatch.')
        paths.add(relative)
    if not REQUIRED <= paths:
        raise ValueError('Native runtime and secure connection adapters are both required.')
    actual = set()
    for path in payload.rglob('*'):
        if path.is_symlink():
            raise ValueError('Payload symlinks are forbidden.')
        if path.is_file() and path.relative_to(payload).as_posix() != 'payload.json':
            actual.add(path.relative_to(payload).as_posix())
    if actual != paths:
        raise ValueError('Every payload file must be covered by the manifest.')
    for name in REQUIRED:
        with (payload / name).open('rb') as handle:
            if handle.read(2) != b'MZ':
                raise ValueError('Adapters must be native Windows executables, not Ubuntu scripts.')
    output.mkdir(parents=True)
    for relative in sorted(paths):
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(payload / relative, destination)
    (output / 'payload.json').write_text(json.dumps(manifest, sort_keys=True, indent=2), encoding='utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--source-commit', required=True)
    args = parser.parse_args()
    stage_payload(args.payload, args.output, args.source_commit)
