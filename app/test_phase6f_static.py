
"""Static safety and contract tests for Phase 6F."""
from pathlib import Path
import ast

EXTENSION = Path(__file__).with_name("phase6f_extension.py.txt")
if not EXTENSION.exists():
    # The phase6f source lives at the repo root alongside the other phase
    # artifacts (README_PHASE6F.md, phase6f_verification_report.md, ...),
    # not next to this test file. apply_phase6f.py (below) is a separate
    # patcher script and does live in this directory.
    EXTENSION = Path(__file__).resolve().parents[1] / "phase6f_extension.py.txt"
PATCHER = Path(__file__).with_name("apply_phase6f.py")

def test_phase6f_sources_parse():
    ast.parse(EXTENSION.read_text(encoding="utf-8"))
    ast.parse(PATCHER.read_text(encoding="utf-8"))

def test_phase6f_has_required_routes():
    text = EXTENSION.read_text(encoding="utf-8")
    for route in [
        '/api/pilots',
        '/api/pilots/{pilot_id}',
        '/api/pilots/{pilot_id}/approvals',
        '/api/pilots/{pilot_id}/validations',
        '/api/pilots/{pilot_id}/status',
        '/pilots',
    ]:
        assert route in text

def test_phase6f_requires_two_person_approval():
    text = EXTENSION.read_text(encoding="utf-8")
    assert "len(active_approvals) >= 2" in text

def test_phase6f_is_bounded():
    text = EXTENSION.read_text(encoding="utf-8")
    assert "PHASE6F_MAX_PILOT_CUSTOMERS" in text
    assert "1 <= customer_count <= PHASE6F_MAX_PILOT_CUSTOMERS" in text

def test_phase6f_performs_no_outbound_action():
    text = EXTENSION.read_text(encoding="utf-8")
    forbidden = [
        "requests.", "httpx.", "urlopen(", "aiohttp.", "socket.",
        "boto3.client(", "subprocess.run(", "os.system(",
    ]
    assert not any(token in text for token in forbidden)
    assert '"external_action_performed": False' in text
