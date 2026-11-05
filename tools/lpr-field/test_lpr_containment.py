"""The LPR output policy, against real Windows ACLs, links and folders in pytest's temp."""
import _winapi
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import containment
import field_probe
import prepare_frames

if sys.platform != 'win32':
    pytest.skip('Windows ACLs', allow_module_level=True)

EVERYONE, USERS = 'S-1-1-0', 'S-1-5-32-545'


@pytest.fixture
def root(tmp_path):
    folder = tmp_path / 'private'
    containment.prepare_folder(folder, folder)
    return folder


def grant(path, sid=EVERYONE, rights='(R)'):
    subprocess.run(['icacls', str(path), '/grant', f'*{sid}:{rights}'], check=True, capture_output=True, timeout=60)


def test_a_new_root_is_locked_to_this_account_from_the_start(root):
    found = containment.read_acl(root)
    assert found['protected'] and found['owner'] == containment.current_sid()
    assert [(kind, sid) for kind, sid, _ in found['entries']] == [(0, containment.current_sid())]


def test_the_real_acl_reader_sees_what_icacls_grants(root):
    grant(root)
    assert EVERYONE in [sid for _, sid, _ in containment.read_acl(root)['entries']]


def test_a_new_child_file_inherits_only_this_account(root):
    with containment.open_new(root / 'results.jsonl', root, encoding='utf-8') as out:
        out.write('{}')
    assert [sid for _, sid, _ in containment.read_acl(root / 'results.jsonl')['entries']] == [containment.current_sid()]


@pytest.mark.parametrize('sid', [EVERYONE, USERS])
def test_an_existing_folder_with_a_wider_acl_is_refused_not_repaired(root, sid):
    (root / 'old').mkdir()  # inherits this temp folder's ACL at most; widened below
    subprocess.run(['icacls', str(root / 'old'), '/inheritance:r', '/grant:r', f'*{containment.current_sid()}:(OI)(CI)F', f'*{sid}:(OI)(CI)R'],
                   check=True, capture_output=True, timeout=60)
    before = containment.read_acl(root / 'old')
    with pytest.raises(containment.Refused, match='outside the policy'):
        containment.prepare_folder(root / 'old', root)
    assert containment.read_acl(root / 'old') == before


def test_a_folder_that_only_inherits_is_refused(root):
    subprocess.run(['icacls', str(root), '/inheritance:e'], check=True, capture_output=True, timeout=60)
    with pytest.raises(containment.Refused):
        containment.prepare_folder(root / 'x', root)


def test_a_root_made_by_mkdir_is_refused(tmp_path):
    plain = tmp_path / 'plain'
    plain.mkdir()
    with pytest.raises(containment.Refused):
        containment.prepare_folder(plain, plain)


def test_a_child_file_with_a_wider_acl_stops_every_tool(root):
    (root / 'old.jsonl').write_text('{}', encoding='utf-8')
    grant(root / 'old.jsonl')
    with pytest.raises(containment.Refused, match='outside the policy'):
        containment.prepare_folder(root / 'new', root)
    with pytest.raises(containment.Refused, match='outside the policy'):
        containment.open_new(root / 'new.jsonl', root, encoding='utf-8')


def test_a_file_in_a_deeper_folder_is_seen_too(root):
    deep = containment.prepare_folder(root / 'a' / 'b', root)
    (deep / 'frame.png').write_bytes(b'x')
    grant(deep / 'frame.png', USERS)
    with pytest.raises(containment.Refused):
        containment.seal(root, root)


def test_a_deny_entry_is_outside_the_policy_too(root):
    subprocess.run(['icacls', str(root), '/deny', f'*{EVERYONE}:(W)'], check=True, capture_output=True, timeout=60)
    assert 'outside the policy' in containment.acl_problem(root, {containment.current_sid()}, True)


def test_an_owner_outside_the_policy_is_refused(root):
    assert 'owned by' in containment.acl_problem(root, {'S-1-5-18'}, True)


def test_a_junction_inside_the_root_is_refused_even_to_a_safe_target(root):
    inner = containment.prepare_folder(root / 'inner', root)
    _winapi.CreateJunction(str(inner), str(root / 'way'))
    with pytest.raises(containment.Refused, match='link'):
        containment.check_path(root / 'way' / 'results.jsonl', root)


def test_a_junction_out_of_the_root_is_refused(root, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    _winapi.CreateJunction(str(outside), str(root / 'way'))
    for call in (lambda: containment.open_new(root / 'way' / 'r.jsonl', root, encoding='utf-8'),
                 lambda: containment.prepare_folder(root / 'way', root)):
        with pytest.raises(containment.Refused, match='link'):
            call()
    assert not list(outside.iterdir())


def test_a_junction_left_in_the_tree_stops_the_tree_check(root, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    _winapi.CreateJunction(str(outside), str(root / 'way'))
    with pytest.raises(containment.Refused, match='link'):
        containment.prepare_folder(root / 'clips', root)


def test_a_root_reached_through_a_junction_is_refused(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    _winapi.CreateJunction(str(real), str(tmp_path / 'alias'))
    with pytest.raises(containment.Refused, match='link'):
        containment.check_path(tmp_path / 'alias' / 'private' / 'x', tmp_path / 'alias' / 'private')


def test_a_symbolic_link_is_refused(root, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    try:
        (root / 'link').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('this account cannot create symbolic links')
    with pytest.raises(containment.Refused, match='link'):
        containment.check_path(root / 'link' / 'x', root)


def test_a_hard_link_to_a_file_elsewhere_is_refused(root, tmp_path):
    elsewhere = tmp_path / 'elsewhere.jsonl'
    elsewhere.write_text('{}', encoding='utf-8')
    os.link(elsewhere, root / 'r.jsonl')
    with pytest.raises(containment.Refused):
        containment.seal(root, root)


@pytest.mark.parametrize('path', [
    r'\\server\share\results.jsonl',
    r'\\?\C:\Users\Public\results.jsonl',
    r'\\.\C:\results.jsonl',
    '//server/share/results.jsonl',
    r'C:\Users\Public\results.jsonl',
    r'C:\ProgramData\results.jsonl',
    r'C:\Users\x\OneDrive\results.jsonl',
    r'C:\Users\x\AnyAiCam-1.2.7-HANDOFF\results.jsonl',
])
def test_network_public_and_unapproved_paths_are_refused(root, path):
    with pytest.raises(containment.Refused, match='private'):
        containment.check_path(path, root)


def test_climbing_out_of_the_root_is_refused(root):
    with pytest.raises(containment.Refused, match='private'):
        containment.check_path(root / '..' / 'elsewhere' / 'x', root)


def test_a_folder_named_like_a_shared_one_inside_the_root_is_refused(root):
    with pytest.raises(containment.Refused, match='shared'):
        containment.check_path(root / 'OneDrive' / 'x', root)


@pytest.mark.parametrize('name', ['r.jsonl:stream', 'NUL', 'con.txt', 'COM1', 'trailing.', 'bad name', '.hidden'])
def test_names_that_address_streams_or_devices_are_refused(root, name):
    with pytest.raises(containment.Refused, match='allowed name'):
        containment.check_path(root / name, root)


def test_a_root_outside_this_profile_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'someone-else'))
    (tmp_path / 'someone-else').mkdir()
    with pytest.raises(containment.Refused, match='profile'):
        containment.check_path(tmp_path / 'private' / 'x', tmp_path / 'private')


def test_a_parent_others_can_change_is_refused(tmp_path):
    parent = tmp_path / 'shared-parent'
    parent.mkdir()
    grant(parent, USERS, '(M)')
    with pytest.raises(containment.Refused, match='change it'):
        containment.check_path(parent / 'private', parent / 'private')


def test_a_private_file_is_never_opened_over_a_kept_one(root):
    (root / 'r.jsonl').write_text('kept', encoding='utf-8')
    with pytest.raises(FileExistsError):
        containment.open_new(root / 'r.jsonl', root, encoding='utf-8')
    with pytest.raises(containment.Refused):
        containment.open_new(root / 'r.jsonl', root, 'w', encoding='utf-8')
    assert (root / 'r.jsonl').read_text(encoding='utf-8') == 'kept'


def test_a_file_that_fails_its_check_is_removed_before_any_data(root, monkeypatch):
    monkeypatch.setattr(containment, 'check_file', lambda *_: (_ for _ in ()).throw(containment.Refused('widened')))
    with pytest.raises(containment.Refused):
        containment.open_new(root / 'r.jsonl', root, encoding='utf-8')
    assert not (root / 'r.jsonl').exists()


def test_the_probe_refuses_before_it_writes(tmp_path, monkeypatch):
    class Lpr:
        @staticmethod
        def capability():
            return {'available': True}

    (tmp_path / 'm.json').write_text(json.dumps({'passes': []}), encoding='utf-8')
    monkeypatch.setattr(field_probe, 'load_lpr', lambda _: Lpr)
    monkeypatch.setitem(sys.modules, 'cv2', type(sys)('cv2'))
    public = tmp_path / 'public.jsonl'
    with pytest.raises(containment.Refused):
        field_probe.main(['probe', str(tmp_path / 'm.json'), str(tmp_path), str(public)])
    assert not public.exists()


def test_frames_refuse_the_same_way(tmp_path):
    pytest.importorskip('cv2')
    (tmp_path / 'w.csv').write_text('pass_id,clip,start_seconds,end_seconds\np1,a.avi,0,1\n', encoding='utf-8')
    root = tmp_path / 'private'
    with pytest.raises(containment.Refused):
        prepare_frames.prepare(tmp_path / 'w.csv', root / 'clips', tmp_path / 'public', root=root)
    assert not (tmp_path / 'public').exists()


def test_all_three_writers_use_one_policy_and_none_writes_around_it():
    folder = Path(containment.__file__).parent
    for name in ('fetch_clips.py', 'field_probe.py', 'prepare_frames.py'):
        text = (folder / name).read_text(encoding='utf-8')
        assert 'containment.' in text and 'write_text(' not in text and 'icacls' not in text
        assert 'FORBIDDEN_FOLDERS' not in text and 'SHARED_FOLDERS' not in text


def test_the_sandbox_returns_only_the_plate_free_report():
    script = (Path(containment.__file__).parents[2] / 'installer/windows/sandbox/lpr-in-sandbox.ps1').read_text(encoding='utf-8')
    field = script[script.index("$field = Join-Path $assets 'field'"):script.index("Check 'Code Integrity")]
    assert '$private' in field and 'field-$($variant.Name).jsonl' in field
    assert 'Join-Path $results "field-$($variant.Name)-report.json"' in field
    assert 'field-$($variant.Name).log' not in field


def test_the_private_root_is_this_profile_not_a_fixed_account():
    assert containment.PRIVATE_ROOT == Path.home() / 'anyaicam-blue' / 'lpr-field-private'


def test_broad_sids_and_struct_layout_stay_exact():
    assert {EVERYONE, USERS} <= containment.BROAD_SIDS
    assert containment._Ace.sid_start.offset == 8 and ctypes.sizeof(containment._AceHeader) == 4
