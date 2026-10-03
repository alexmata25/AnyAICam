"""Offline Windows build preparation; deliberately never installs an EXE."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / 'windows-candidate'
spec = importlib.util.spec_from_file_location('stage_payload', ROOT / 'stage_payload.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
COMMIT = 'a' * 40


def payload(tmp_path):
    root = tmp_path / 'input'
    root.mkdir()
    for name in module.REQUIRED:
        (root / name).write_bytes(b'MZ local test fixture; not executable')
    manifest = {'platform': 'windows-x64', 'source_commit': COMMIT, 'version': '0.1.0',
                'files': [{'path': p.name, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                          for p in root.iterdir()]}
    (root / 'payload.json').write_text(json.dumps(manifest))
    return root, manifest


def test_stages_exact_pinned_offline_bytes(tmp_path):
    root, manifest = payload(tmp_path)
    output = tmp_path / 'stage'
    assert module.stage_payload(root, output, COMMIT) == manifest
    assert (output / 'ConnectAnyAiCam.exe').read_bytes() == (root / 'ConnectAnyAiCam.exe').read_bytes()


@pytest.mark.parametrize('case', ['checksum', 'commit', 'platform', 'missing', 'extra', 'traversal', 'duplicate', 'ubuntu'])
def test_invalid_payload_fails_before_output_is_created(tmp_path, case):
    root, manifest = payload(tmp_path)
    if case == 'checksum':
        (root / 'ConnectAnyAiCam.exe').write_bytes(b'tampered')
    elif case in ('commit', 'platform'):
        manifest['source_commit' if case == 'commit' else 'platform'] = 'wrong'
    elif case == 'missing':
        manifest['files'] = manifest['files'][:1]
        (root / manifest['files'][0]['path']).unlink()
    elif case == 'extra':
        (root / 'unlisted.exe').write_bytes(b'MZ')
    elif case == 'traversal':
        manifest['files'][0]['path'] = '../secret'
    elif case == 'duplicate':
        manifest['files'].append(manifest['files'][0])
    elif case == 'ubuntu':
        p = root / 'ConnectAnyAiCam.exe'
        p.write_bytes(b'#!/bin/bash')
        next(entry for entry in manifest['files'] if entry['path'] == p.name)['sha256'] = hashlib.sha256(p.read_bytes()).hexdigest()
    (root / 'payload.json').write_text(json.dumps(manifest))
    output = tmp_path / 'stage'
    with pytest.raises(ValueError):
        module.stage_payload(root, output, COMMIT)
    assert not output.exists()


def test_refuses_output_inside_payload_or_existing_directory(tmp_path):
    root, _ = payload(tmp_path)
    with pytest.raises(ValueError):
        module.stage_payload(root, root / 'stage', COMMIT)
    with pytest.raises(ValueError):
        module.stage_payload(root, root, COMMIT)



def test_local_inno_scaffold_compiles_without_running_payload(tmp_path):
    import os
    import subprocess
    compiler = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/Inno Setup 7/ISCC.exe'
    if os.name != 'nt' or not compiler.is_file():
        pytest.skip('Local Windows Inno compiler unavailable')
    root, _ = payload(tmp_path)
    output = tmp_path / 'output'
    command = ['pwsh', '-NoProfile', '-File', str(ROOT/'build.ps1'),
               '-Payload', str(root), '-OutputRoot', str(output),
               '-SourceCommit', COMMIT, '-PythonPath', __import__('sys').executable,
               '-Compile', '-InnoCompiler', str(compiler)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (output/'AnyAiCam-test.exe').is_file()
    assert 'Clean-machine' in result.stdout
