"""step3-smoke.sh asks the running app for its role through /health, on the build
the script is pinned to. Nothing here starts Docker or calls a real server: the
check's own shell line runs with curl replaced.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'deploy' / 'samsung-step3' / 'step3-smoke.sh'
PINNED = 'f5a6d875c5cc9bd313e85ac7a330359f39255150'
# The shape f5a6d875's own /health handler returns.
EDGE = '{"status":"ok","service":"AnyAiCam VMS","version":"1.2.5","build_id":"%s","environment":"production","runtime_role":"edge","uptime_seconds":5}' % PINNED


def _shell():
    # Never the System32 bash, which is WSL here.
    found = shutil.which('sh')
    if found and 'system32' not in found.lower():
        return found
    git_sh = Path(r'C:\Program Files\Git\bin\sh.exe')
    return str(git_sh) if git_sh.exists() else None


def _role_line():
    lines = [line for line in SCRIPT.read_text(encoding='utf-8').splitlines() if 'runtime_role' in line]
    assert len(lines) == 1
    return lines[0]


def _inner_command():
    found = re.fullmatch(r'check "/health runtime_role edge" sh -c "(.*)"', _role_line())
    assert found
    return found.group(1).replace('\\"', '"')


def _run_check(tmp_path, body, curl_exit=0):
    shell = _shell()
    if shell is None:
        pytest.skip('no POSIX shell other than WSL')
    answer = tmp_path / 'body.json'
    answer.write_text(body, encoding='utf-8')
    fake = f'curl() {{ cat "{answer.as_posix()}"; return {curl_exit}; }}\nURL=http://127.0.0.1:8000\n'
    return subprocess.run([shell, '-c', fake + _inner_command()], capture_output=True, timeout=30,
                          env=dict(os.environ, MSYS_NO_PATHCONV='1')).returncode


def test_the_script_keeps_its_original_role_line():
    assert _role_line() == 'check "/health runtime_role edge" sh -c "curl -fsS $URL/health | grep -q \'\\"runtime_role\\":\\"edge\\"\'"'


def test_no_container_environment_check_replaced_it():
    text = SCRIPT.read_text(encoding='utf-8')
    assert 'printenv' not in text and 'ANYAICAM_RUNTIME_ROLE' not in text and '/proc/1/environ' not in text


def test_the_script_is_still_pinned_to_the_build_that_reports_the_role():
    assert f'COMMIT={PINNED}\n' in SCRIPT.read_text(encoding='utf-8')


def test_the_pinned_builds_answer_passes(tmp_path):
    assert _run_check(tmp_path, EDGE) == 0


@pytest.mark.parametrize('body', [
    EDGE.replace('"edge"', '"cloud"'),
    EDGE.replace('"edge"', '"combined"'),
    EDGE.replace(',"runtime_role":"edge"', ''),
    '{}',
    '',
])
def test_any_other_answer_fails(tmp_path, body):
    assert _run_check(tmp_path, body) != 0


def test_it_does_not_read_a_variable_named_like_the_role(tmp_path):
    assert _run_check(tmp_path, EDGE.replace('"runtime_role":"edge"', '"ANYAICAM_RUNTIME_ROLE":"edge"')) != 0
