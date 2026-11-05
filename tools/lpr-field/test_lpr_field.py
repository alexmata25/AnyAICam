"""Field-test tooling with a fake engine. No model, no Tesseract, no footage."""
import csv
from datetime import datetime, timezone
import hashlib
import io
import io
import json
import threading
from pathlib import Path
import types

import pytest

import containment
import fetch_clips
import field_probe
import prepare_frames
import scoring


class FakeFrame:
    def __getitem__(self, _):
        return self


class FakeLpr:
    """Reads what each frame file name says, confirming on the second read."""
    def __init__(self, script):
        self.script, self.current = script, None
        self._votes, self._emitted, self._votes_lock = {}, {}, threading.Lock()
        self.model_loads = 0

    def detect_plates_full_frame(self, frame):
        return [{'region': (1, 2, 30, 10), 'detector_confidence': 0.8}] if self.script[frame] else []

    def read_detected_plate(self, _crop, _confidence):
        return (self.script[self.frame], 90.0)

    def confirm_plate(self, _camera, result, now=None):
        self._votes[result['plate_number']] = self._votes.get(result['plate_number'], 0) + 1
        return result if self._votes[result['plate_number']] == 2 else None


def truth_row(pass_id, plate, legible='y', authorized='y', light='day', camera='2'):
    return {'pass_id': pass_id, 'camera': camera, 'light': light, 'plate': plate,
            'human_legible': legible, 'authorized': authorized}


def write_truth(path, rows):
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def frame(pass_id, reads=(), confirmed=(), detect_ms=100.0):
    return {'pass_id': pass_id, 'detect_ms': detect_ms, 'confirmed': list(confirmed),
            'detections': [{'text': text, 'ocr_ms': 50.0} for text in reads]}


def test_exact_near_missed_and_wrong_are_told_apart():
    plates = {'ABC1234'}
    assert scoring.score_pass(plates, [frame('p', ['ABC1234'], ['ABC1234'])])['confirmed_exact']
    near = scoring.score_pass(plates, [frame('p', ['ABC1Z34'])])
    assert near['near'] and not near['read_exact'] and near['char_accuracy'] == 0.857
    wrong = scoring.score_pass(plates, [frame('p', ['XYZ9999'], ['XYZ9999'])])
    assert wrong['wrong_confirmed'] == 1 and not wrong['near']
    missed = scoring.score_pass(plates, [frame('p')])
    assert not missed['detected'] and missed['char_accuracy'] == 0.0


def test_a_confirmed_read_on_an_empty_pass_is_a_false_positive():
    empty = scoring.score_pass(set(), [frame('p', ['ABC1234'], ['ABC1234'])])
    assert empty['wrong_confirmed'] == 1 and empty['detected']


def test_two_plates_in_one_pass_may_both_be_right():
    assert scoring.score_pass({'ABC1234', 'KPC6266'}, [frame('p', ['KPC6266'], ['KPC6266'])])['wrong_confirmed'] == 0


def test_illegible_passes_do_not_count_against_accuracy(tmp_path):
    write_truth(tmp_path / 't.csv', [truth_row('a', 'ABC1234'), truth_row('b', 'KPC6266', legible='n')])
    report = scoring.summarize(scoring.load_truth(tmp_path / 't.csv'), {'a': [frame('a', ['ABC1234'], ['ABC1234'])], 'b': [frame('b')]})
    assert report['overall']['exact_confirmed']['rate'] == 1.0
    assert report['overall']['illegible_passes_excluded'] == 1


def test_the_report_never_contains_plate_text(tmp_path):
    write_truth(tmp_path / 't.csv', [truth_row('a', 'ABC1234')])
    results = {'a': [frame('a', ['ABC1234', 'QQQ9999'], ['QQQ9999'])]}
    report = json.dumps(scoring.summarize(scoring.load_truth(tmp_path / 't.csv'), results))
    assert 'ABC1234' not in report and 'QQQ9999' not in report


def test_unauthorized_passes_are_refused_not_scored(tmp_path):
    write_truth(tmp_path / 't.csv', [truth_row('a', 'ABC1234', authorized='n')])
    with pytest.raises(ValueError, match='purged'):
        scoring.load_truth(tmp_path / 't.csv')


def test_results_without_ground_truth_stop_the_score():
    with pytest.raises(ValueError):
        scoring.summarize({}, {'ghost': [frame('ghost')]})


def test_small_samples_show_a_wide_interval():
    low, high = scoring.wilson(9, 10)
    assert low < 0.6 and high > 0.95
    assert scoring.wilson(0, 0) is None


def test_probe_records_reads_confirmation_and_time_per_pass():
    frames = {'f1': 'A', 'f2': 'A', 'g1': 'A'}
    fake = FakeLpr({})
    fake.script = {k: v for k, v in frames.items()}
    objects = {name: name for name in frames}

    def imread(path):
        name = path.replace('\\', '/').split('/')[-1]
        fake.frame = name
        return objects[name]

    manifest = {'passes': [
        {'pass_id': 'p1', 'frames': [{'file': 'f1', 'seconds': 0.0}, {'file': 'f2', 'seconds': 0.5}]},
        {'pass_id': 'p2', 'frames': [{'file': 'g1', 'seconds': 0.0}]},
    ]}
    fake.detect_plates_full_frame = lambda f: [{'region': (0, 0, 30, 10), 'detector_confidence': 0.8}]

    class Slice(str):
        def __getitem__(self, _):
            return self
    objects = {k: Slice(k) for k in frames}
    out = io.StringIO()
    field_probe.run(fake, manifest, 'frames', out, imread)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [line['confirmed'] for line in lines] == [[], ['A'], []]  # the reset stops p1 helping p2
    assert lines[0]['detections'][0]['plate_width_px'] == 30 and 'detect_ms' in lines[0]


def test_the_probe_never_unloads_the_model_between_passes():
    assert 'reset_pipeline_state(' not in (Path(field_probe.__file__).read_text(encoding='utf-8').split('def run', 1)[1])


def test_sample_times_and_frame_limit(tmp_path):
    assert prepare_frames.sample_times(1.0, 2.0, 2.0) == [1.0, 1.5]
    with pytest.raises(ValueError):
        prepare_frames.sample_times(2.0, 1.0, 2.0)
    (tmp_path / 'w.csv').write_text('pass_id,clip,start_seconds,end_seconds\np1,a.mkv,0,300\n', encoding='utf-8')
    pytest.importorskip('cv2')
    with pytest.raises(ValueError, match='limit'):
        prepare_frames.prepare(tmp_path / 'w.csv', tmp_path / 'private' / 'clips', tmp_path / 'private' / 'out', root=tmp_path / 'private')


def test_a_clip_path_cannot_leave_the_clips_folder(tmp_path):
    (tmp_path / 'w.csv').write_text('pass_id,clip,start_seconds,end_seconds\np1,../a.mkv,0,1\n', encoding='utf-8')
    root = tmp_path / 'private'
    pytest.importorskip('cv2')
    with pytest.raises(ValueError, match='directly'):
        prepare_frames.prepare(tmp_path / 'w.csv', root / 'clips', root / 'out', root=root)


def test_frames_are_extracted_with_hashes_from_a_tiny_synthetic_clip(tmp_path):
    cv2 = pytest.importorskip('cv2')
    numpy = pytest.importorskip('numpy')
    root = tmp_path / 'private'
    clips = containment.prepare_folder(root / 'clips', root)
    writer = cv2.VideoWriter(str(clips / 'a.avi'), cv2.VideoWriter_fourcc(*'MJPG'), 10, (64, 48))
    for value in range(30):
        writer.write(numpy.full((48, 64, 3), value * 8, dtype=numpy.uint8))
    writer.release()
    (tmp_path / 'w.csv').write_text('pass_id,clip,start_seconds,end_seconds\np1,a.avi,0,2\n', encoding='utf-8')
    manifest = prepare_frames.prepare(tmp_path / 'w.csv', clips, root / 'out', root=root)
    assert len(manifest['passes'][0]['frames']) == 4
    containment.seal(root / 'out', root)
    assert all(len(item['sha256']) == 64 for item in manifest['passes'][0]['frames'])


NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
OLD_CLIP = 'camera2_2026-10-30_18-00-00.mkv'


def selection(*clips, camera='2'):
    return [{'pass_id': f'p{i}', 'camera': camera, 'clip': clip} for i, clip in enumerate(clips)]


def cool(tctl=50.0):
    return lambda: {'at': '2026-10-31T11:59:00Z', 'cpu_tctl_c': tctl}


def SAFE(root):
    return {'root': root, 'free_bytes': lambda folder: 10 ** 12}


class FakeSsh:
    def __init__(self, data=b'clip', reported=None, exit_code=0):
        self.data, self.reported, self.commands = data, reported or hashlib.sha256(data).hexdigest(), []
        self.exit_code, self.killed = exit_code, False

    def __call__(self, command, **_):
        self.commands.append(command)
        return types.SimpleNamespace(stdout=f'{self.reported}  x\n')

    def popen(self, command, **_):
        self.commands.append(command)
        return types.SimpleNamespace(stdout=io.BytesIO(self.data), wait=lambda: self.exit_code, kill=lambda: setattr(self, 'killed', True))


def test_a_clip_is_copied_only_after_both_hashes_agree(tmp_path):
    ssh = FakeSsh()
    copied = fetch_clips.fetch(selection(OLD_CLIP), tmp_path / 'r' / 'private', run=ssh, popen=ssh.popen, read_temperature=cool(), now=lambda: NOW, **SAFE(tmp_path / 'r'))
    assert copied[0]['sha256'] == hashlib.sha256(b'clip').hexdigest()
    assert (tmp_path / 'r' / 'private' / OLD_CLIP).read_bytes() == b'clip'
    bad = FakeSsh(reported='0' * 64)
    with pytest.raises(ValueError, match='hash differs'):
        fetch_clips.fetch(selection(OLD_CLIP), tmp_path / 'r' / 'other', run=bad, popen=bad.popen, read_temperature=cool(), now=lambda: NOW, **SAFE(tmp_path / 'r'))
    assert not list((tmp_path / 'r' / 'other').iterdir())


def test_the_copy_only_ever_runs_cat_and_sha256sum_at_low_priority(tmp_path):
    ssh = FakeSsh()
    fetch_clips.fetch(selection(OLD_CLIP), tmp_path / 'r' / 'private', run=ssh, popen=ssh.popen, read_temperature=cool(), now=lambda: NOW, **SAFE(tmp_path / 'r'))
    assert [command[command.index('19') + 1] for command in ssh.commands] == ['cat', 'sha256sum']
    assert all(command[-1] == f'/app/recordings/camera2/{OLD_CLIP}' for command in ssh.commands)


@pytest.mark.parametrize('clip', ['camera2_2026-10-30_18-00-00.mkv;rm -rf /', '../x.mkv', 'camera3_2026-10-30_18-00-00.mkv'])
def test_anything_but_an_exact_clip_of_that_camera_is_refused(clip):
    with pytest.raises(ValueError, match='not a clip'):
        fetch_clips.check_selection(selection(clip), NOW)


def test_a_clip_that_may_still_be_open_and_an_unapproved_camera_are_refused():
    with pytest.raises(ValueError, match='still be open'):
        fetch_clips.check_selection(selection('camera2_2026-10-31_11-55-00.mkv'), NOW)
    with pytest.raises(ValueError, match='not approved'):
        fetch_clips.check_selection(selection('camera5_2026-10-30_18-00-00.mkv', camera='5'), NOW)


@pytest.mark.parametrize('reading, reason', [
    ({'at': '2026-10-31T11:59:00Z', 'cpu_tctl_c': 71.0}, 'start limit'),
    ({'at': '2026-10-31T11:59:00Z', 'cpu_tctl_c': None}, 'no temperature'),
    ({'at': '2026-10-31T11:00:00Z', 'cpu_tctl_c': 40.0}, 'stale'),
])
def test_a_hot_or_silent_ryzen_stops_the_copy_before_any_ssh(tmp_path, reading, reason):
    ssh = FakeSsh()
    with pytest.raises(RuntimeError, match=reason):
        fetch_clips.fetch(selection(OLD_CLIP), tmp_path / 'r' / 'private', run=ssh, popen=ssh.popen, read_temperature=lambda: reading, now=lambda: NOW, **SAFE(tmp_path / 'r'))
    assert ssh.commands == []


def test_one_clip_past_the_limit_stops_mid_stream_and_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_clips, 'MAX_CLIP_BYTES', 10)
    ssh = FakeSsh(data=b'x' * (2 * 1024 * 1024))
    with pytest.raises(RuntimeError, match='byte limit'):
        fetch_clips.fetch(selection(OLD_CLIP), tmp_path / 'r' / 'private', run=ssh, popen=ssh.popen, read_temperature=cool(), now=lambda: NOW, **SAFE(tmp_path / 'r'))
    assert ssh.killed and not list((tmp_path / 'r' / 'private').iterdir())


def test_the_total_limit_also_applies_during_a_transfer(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_clips, 'MAX_BYTES', 6)
    second = 'camera2_2026-10-30_18-05-00.mkv'
    ssh = FakeSsh(data=b'abcd')
    with pytest.raises(RuntimeError, match='byte limit'):
        fetch_clips.fetch(selection(OLD_CLIP, second), tmp_path / 'r' / 'private', run=ssh, popen=ssh.popen, read_temperature=cool(), now=lambda: NOW, **SAFE(tmp_path / 'r'))
    assert [item.name for item in (tmp_path / 'r' / 'private').iterdir()] == [OLD_CLIP]


def test_a_failed_or_interrupted_stream_leaves_no_partial_file(tmp_path):
    ssh = FakeSsh(exit_code=255)
    with pytest.raises(RuntimeError, match='did not finish'):
        fetch_clips.fetch(selection(OLD_CLIP), tmp_path / 'r' / 'private', run=ssh, popen=ssh.popen, read_temperature=cool(), now=lambda: NOW, **SAFE(tmp_path / 'r'))
    assert not list((tmp_path / 'r' / 'private').iterdir())


def test_a_stale_partial_is_replaced_and_a_finished_clip_is_kept(tmp_path):
    folder = containment.prepare_folder(tmp_path / 'r' / 'private', tmp_path / 'r')
    (folder / f'{OLD_CLIP}.part').write_bytes(b'stale')
    ssh = FakeSsh()
    fetch_clips.fetch(selection(OLD_CLIP), folder, run=ssh, popen=ssh.popen, read_temperature=cool(), now=lambda: NOW, **SAFE(tmp_path / 'r'))
    assert sorted(item.name for item in folder.iterdir()) == [OLD_CLIP]
    with pytest.raises(ValueError, match='already'):
        fetch_clips.fetch(selection(OLD_CLIP), folder, run=ssh, popen=ssh.popen, read_temperature=cool(), now=lambda: NOW, **SAFE(tmp_path / 'r'))


def test_low_disk_stops_the_copy_before_any_ssh(tmp_path):
    quiet = FakeSsh()
    with pytest.raises(RuntimeError, match='free'):
        fetch_clips.fetch(selection(OLD_CLIP), tmp_path / 'r' / 'other', run=quiet, popen=quiet.popen, read_temperature=cool(), now=lambda: NOW,
                          **{**SAFE(tmp_path / 'r'), 'free_bytes': lambda folder: 1})
    assert quiet.commands == []


def test_scoring_skips_the_engine_record(tmp_path):
    results = tmp_path / 'r.jsonl'
    results.write_text(json.dumps({'engine_identity': True, 'tesseract_sha256': 'a'}) + '\n' + json.dumps(frame('a')) + '\n', encoding='utf-8')
    assert list(scoring.load_results(results)) == ['a']


def test_the_probe_records_which_engine_it_measured():
    source = Path(field_probe.__file__).read_text(encoding='utf-8').split('def main', 1)[1]
    assert source.index('engine_identity(lpr)') < source.index('run(lpr, manifest')


def test_the_ocr_acceptance_gate_stays_ten_of_ten_on_the_unchanged_fixtures():
    gate = (Path(__file__).resolve().parents[2] / 'installer/windows/tesseract-build/validate-minimal-ocr.py').read_text(encoding='utf-8')
    assert 'passed = version_ok and len(rows) == 5 * args.repeats and all(row["match"] for row in rows)' in gate
    assert 'if len(images) != 5' in gate
    assert 'default=2' in gate.split('--repeats', 1)[1].split('\n', 1)[0]
