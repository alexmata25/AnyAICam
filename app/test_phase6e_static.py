
"""Static safety checks for the Phase 6E extension."""
from pathlib import Path
import ast

EXTENSION = Path(__file__).with_name("phase6e_extension.py.txt")
if not EXTENSION.exists():
    # The phase6e source lives at the repo root alongside the other phase
    # artifacts (README_PHASE6E.md, phase6e_verification_report.md, ...),
    # not next to this test file.
    EXTENSION = Path(__file__).resolve().parents[1] / "phase6e_extension.py.txt"

def test_extension_parses():
    ast.parse(EXTENSION.read_text(encoding="utf-8"))

def test_no_outbound_network_calls():
    text = EXTENSION.read_text(encoding="utf-8")
    forbidden = ["urlopen(", "requests.", "httpx.", "boto3.client(", "socket."]
    assert not any(item in text for item in forbidden)

def test_expected_routes_present():
    text = EXTENSION.read_text(encoding="utf-8")
    for route in [
        "/api/operations/stability",
        "/api/operations/deployments/check",
        "/api/operations/drills",
        "/operations/stability",
    ]:
        assert route in text
