"""The Windows link carries the Claim ID and the link check accepts it."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / 'installer' / 'windows' / 'open-claim-link.ps1').read_text(encoding='utf-8')
SETUP = (ROOT / 'installer' / 'windows' / 'AnyAiCam-VMS.iss').read_text(encoding='utf-8')
PATTERN = re.search(r"\$LinkPattern = '([^']+)'", SCRIPT).group(1)
BASE = 'https://portal.example/claim#code=A1B2C3D4&link=windows'


def test_the_link_check_takes_links_with_and_without_a_claim_id():
    assert re.match(PATTERN, BASE)
    assert re.match(PATTERN, BASE + '&claim=CID-VFB9-ZERD-6QYK-0FBR')


def test_the_link_check_refuses_anything_else_in_that_place():
    assert not re.match(PATTERN, BASE + '&claim=00000000-0000-4000-8000-000000000000')
    assert not re.match(PATTERN, BASE + '&claim=CID-VFB9-ZERD-6QYK-0FBR&next=https://evil.example')


def test_the_manual_text_is_only_in_the_signed_in_users_message_box():
    assert 'Get-ManualLinkText $result.link' in SCRIPT
    assert 'Show-Message $manual 64 900' in SCRIPT
    elevated = SCRIPT.split('if ($Prepare) {', 1)[1].split('$powershell = Join-Path', 1)[0]
    assert 'Get-ManualLinkText' not in elevated
    assert not re.search(r'Write-(Host|Output|Verbose|Information).*(manual|\.link)', SCRIPT, re.I)


def test_the_manual_text_pattern_follows_the_link_form():
    pattern = re.search(r"-cnotmatch '(#code=[^']+)'", SCRIPT).group(1)
    match = re.search(pattern, BASE + '&claim=CID-VFB9-ZERD-6QYK-0FBR')
    assert match.group(1) == 'A1B2C3D4' and match.group(2) == 'CID-VFB9-ZERD-6QYK-0FBR'
    assert not re.search(pattern, BASE)


def test_a_linked_pc_shows_only_a_checked_claim_id_and_setup_never_waits_for_it():
    assert "Get-AccountLinkResult $dataRoot -WithClaimId" in SCRIPT
    prepare = SCRIPT.split('if ($Prepare) {', 1)[1].split('if ($Elevated) {', 1)[0]
    assert '-WithClaimId' not in prepare
    assert "-cmatch $ClaimIdPattern" in SCRIPT
    assert 'device_id' not in SCRIPT and 'appliance_id' not in SCRIPT


def test_the_setup_check_strips_exactly_the_claim_suffix():
    assert len('&claim=CID-VFB9-ZERD-6QYK-0FBR') == 30
    check = SETUP.split('function IsAccountLink', 1)[1].split('\nend;', 1)[0]
    assert "Pos('&claim=CID-', Base) = Length(Base) - 29" in check
    assert 'Copy(Base, 1, Length(Base) - 30)' in check


def test_the_pc_accepts_a_long_code_and_the_finish_page_shows_the_claim_id():
    root = Path(__file__).resolve().parents[1] / 'windows'
    script = (root / 'open-claim-link.ps1').read_text(encoding='utf-8')
    assert script.count('[0-9A-Z]{12}') >= 2
    setup = (root / 'AnyAiCam-VMS.iss').read_text(encoding='utf-8')
    assert '(CodeLength <> 8) and (CodeLength <> 12)' in setup and 'ClaimIdText()' in setup and "'Claim ID of this computer: '" in setup


def test_sandbox_link_checks_read_a_link_that_carries_a_claim_id():
    folder = Path(__file__).resolve().parents[1] / 'windows' / 'sandbox'
    for name in ('functional-in-sandbox.ps1', 'validate-in-sandbox.ps1'):
        assert "&link=windows(&|$)')" in (folder / name).read_text(encoding='utf-8')
