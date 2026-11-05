"""The Claim ID derivation (app and agent copies) and its typing rules."""
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VECTORS = [
    ('00000000-0000-4000-8000-000000000000', 'CID-VFB9-ZERD-6QYK-0FBR'),
    ('11111111-2222-4333-8444-555555555555', 'CID-RE6M-C7A2-ECQ7-29NQ'),
]


def _load(path):
    name = 'claim_id_' + path.parent.name
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=['app/claim_id.py', 'appliance-agent/anyaicam_agent/claim_id.py'])
def claim_id(request):
    return _load(ROOT / request.param)


@pytest.mark.parametrize('device_id,expected', VECTORS)
def test_vectors_match_in_both_copies(claim_id, device_id, expected):
    assert claim_id.claim_id_for(device_id) == expected
    assert claim_id.claim_id_for(device_id.upper()) == expected
    assert claim_id.CLAIM_ID_PATTERN.match(expected)


def test_the_id_never_contains_any_part_of_the_uuid(claim_id):
    device_id = VECTORS[1][0]
    shown = claim_id.claim_id_for(device_id).replace('-', '').lower()
    for group in re.findall(r'[0-9a-f]{4,}', device_id):
        assert group not in shown


def test_it_cannot_be_mistaken_for_a_label_code_or_cloud_id(claim_id):
    shown = claim_id.claim_id_for(VECTORS[0][0])
    assert len(shown.replace('-', '')) != 12 and not shown.startswith('AIC-')


@pytest.mark.parametrize('typed', ['cid-vfb9-zerd-6qyk-0fbr', 'CID VFB9 ZERD 6QYK 0FBR', 'VFB9ZERD6QYK0FBR',
                                   'CID-VFB9-ZERD-6QYK-OFBR'])
def test_people_can_type_it_their_way(claim_id, typed):
    assert claim_id.normalize_claim_id(typed) == VECTORS[0][1]


@pytest.mark.parametrize('typed', ['', 'CID-VFB9', 'CID-VFB9-ZERD-6QYK-0FBS', 'CID-VFB9-ZERD-6QYK-0FB!', None])
def test_malformed_or_mistyped_ids_are_refused(claim_id, typed):
    assert claim_id.normalize_claim_id(typed) is None


def test_every_single_character_typo_is_caught(claim_id):
    good = VECTORS[0][1].replace('CID-', '').replace('-', '')
    for index in range(16):
        for replacement in claim_id.ALPHABET:
            if replacement != good[index]:
                bad = good[:index] + replacement + good[index + 1:]
                assert claim_id.normalize_claim_id(bad) is None


def test_an_id_whose_text_starts_like_the_prefix_can_be_typed_without_it(claim_id):
    device = next(f'{n:08x}-0000-4000-8000-000000000000' for n in range(2000000)
                  if claim_id.claim_id_for(f'{n:08x}-0000-4000-8000-000000000000').startswith('CID-C1D'))
    shown = claim_id.claim_id_for(device)
    assert claim_id.normalize_claim_id(shown.replace('CID-', '').replace('-', '')) == shown
