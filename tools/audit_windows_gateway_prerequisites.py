"""Read-only build inventory. Never executes tools, hashes large inputs or contacts services."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
SDK = Path(r'C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64')


def pinned_inputs(build_source: str) -> list[str]:
    block = build_source.split('$pinned = [ordered]@{', 1)[1].split('\n}', 1)[0]
    names = re.findall(r"^\s*'([^']+)'\s*=\s*'[0-9a-f]{64}'", block, re.MULTILINE)
    if not names or len(names) != len(set(names)):
        raise ValueError('Build input pins are missing or duplicated')
    return names


def locked_wheels(lock_source: str) -> list[str]:
    names = []
    for line in lock_source.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r'[0-9a-f]{64}  ([^/\\]+\.whl)', line)
        if not match or match[1] in names:
            raise ValueError('Wheel lock contains an invalid or duplicate entry')
        names.append(match[1])
    if not names:
        raise ValueError('Wheel lock is empty')
    return names


def audit(root=ROOT, env=None, which=shutil.which, build_python=None, keyset=None) -> dict:
    env = os.environ if env is None else env
    win = root / 'installer/windows'
    pinned = pinned_inputs((win / 'build.ps1').read_text(encoding='utf-8-sig'))
    wheels = locked_wheels((win / 'wheels.lock.sha256').read_text(encoding='utf-8-sig'))
    compiler = Path(env.get('LOCALAPPDATA', 'missing-localappdata')) / 'Programs/Inno Setup 7/ISCC.exe'
    makecat = Path(env.get('ANYAICAM_MAKECAT_PATH') or SDK / 'makecat.exe')
    signtool = Path(env.get('ANYAICAM_SIGNTOOL_PATH') or SDK / 'signtool.exe')
    azure_dlib = Path(env.get('ANYAICAM_AZURE_SIGNING_DLIB') or
                      Path(env.get('LOCALAPPDATA', 'missing-localappdata')) / 'Microsoft/MicrosoftArtifactSigningClientTools/Azure.CodeSigning.Dlib.dll')
    azure_metadata = Path(env.get('ANYAICAM_AZURE_SIGNING_METADATA') or r'C:\AnyAiCamSigning\metadata.json')
    missing_inputs = [name for name in pinned if not (win / 'vendor' / name).is_file()]
    missing_wheels = [name for name in wheels if not (win / 'vendor/wheels' / name).is_file()]
    path_git = which('git')
    installed_git = Path(env.get('ProgramFiles', 'missing-programfiles')) / 'Git/cmd/git.exe'
    resolved_git = str(path_git) if path_git else (str(installed_git) if installed_git.is_file() else None)
    checks = {
        'inno_setup_7': {'path': str(compiler), 'present': compiler.is_file(),
                         'required_version': '7.1.0', 'version_verified': False},
        'git_on_path': {'present': bool(path_git)},
        'build_git': {'path': resolved_git, 'present': bool(resolved_git), 'resolution': 'PATH' if path_git else 'installed_fallback'},
        'makecat': {'path': str(makecat), 'present': makecat.is_file()},
        'signtool': {'path': str(signtool), 'present': signtool.is_file()},
        'build_python': {'provided': bool(build_python), 'present': bool(build_python and Path(build_python).is_file()), 'version_verified': False},
        'approved_public_keyset': {'provided': bool(keyset), 'present': bool(keyset and Path(keyset).is_file()), 'pin_verified': False},
        'signing_modes': {
            'selected': 'not_selected',
            'azure': {'dlib_present': azure_dlib.is_file(), 'metadata_present': azure_metadata.is_file(), 'identity_verified': False},
            'certificate': {'signtool_path_configured': bool(env.get('ANYAICAM_SIGNTOOL_PATH')),
                'certificate_configured': bool(env.get('ANYAICAM_SIGN_CERT_SHA1')),
                'timestamp_configured': bool(env.get('ANYAICAM_TIMESTAMP_URL')), 'certificate_store_inspected': False},
        },
        'vendor_inputs': {'required_count': len(pinned), 'missing': missing_inputs, 'hashes_verified': False},
        'wheels': {'required_count': len(wheels), 'missing': missing_wheels, 'hashes_verified': False},
    }
    missing = [name for name in ('inno_setup_7', 'build_git', 'makecat', 'signtool') if not checks[name]['present']]
    if missing_inputs:
        missing.append('vendor_inputs')
    if missing_wheels:
        missing.append('wheels')
    return {
        'recorded_at_utc': datetime.now(timezone.utc).isoformat(), 'machine': platform.node(),
        'checkout': str(root), 'kind': 'presence_only', 'build_readiness': 'NOT_VALIDATED',
        'missing_dependencies': missing, 'checks': checks,
        'remaining_gates': ['Approved setup and release versions', 'Verified Inno Setup 7.1.0 metadata and approved provenance', 'Verified build interpreter version',
            'Approved public keyset and independently pinned hash', 'All vendor/wheel hashes and signatures',
            'Packaged source integrity and signing configuration', 'Licence/distribution review from DEPENDENCIES.md',
            'Separate resource-window approval; no build or VM operation has run'],
        'r430_resource_window': {'current_ram': 'not_measured', 'running_vms': 'not_queried', 'approval_requested': False},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Optional inventory JSON; does not start a build')
    parser.add_argument('--build-python', type=Path, help='Presence check only; interpreter is not executed')
    parser.add_argument('--entitlement-keyset', type=Path, help='Approved public keyset path; content is not displayed')
    args = parser.parse_args()
    report = audit(build_python=args.build_python, keyset=args.entitlement_keyset)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'build_readiness': report['build_readiness'], 'missing_dependencies': report['missing_dependencies'],
                      'vendor_inputs_missing': len(report['checks']['vendor_inputs']['missing']),
                      'wheels_missing': len(report['checks']['wheels']['missing'])}))
    return 0  # inventory completed; NOT_VALIDATED is never a build acceptance result


if __name__ == '__main__':
    raise SystemExit(main())
