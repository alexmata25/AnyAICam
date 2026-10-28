import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'installer/windows/installer-size.ps1'
PS = shutil.which('powershell.exe')


@pytest.mark.skipif(not PS, reason='Windows PowerShell required')
@pytest.mark.parametrize('size,accepted', [(0, False), (9, True), (10, True), (11, False)])
def test_size_boundary_fails_closed(tmp_path, size, accepted):
    setup = tmp_path / 'candidate.exe'
    setup.write_bytes(b'x' * size)
    command = f". '{SCRIPT}'; Assert-InstallerSize '{setup}' -MaxBytes 10"
    result = subprocess.run([PS, '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, text=True, timeout=30)
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr


def test_build_checks_the_actual_output_after_compilation():
    build = SCRIPT.with_name('build.ps1').read_text()
    assert build.index('& $iscc @compilerArguments') < build.index('Assert-InstallerSize $setupPath')
    assert '[long]$MaxBytes = 750000000' in SCRIPT.read_text()
