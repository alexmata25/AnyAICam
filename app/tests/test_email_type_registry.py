"""Every email type the app sends must be registered in EMAIL_TYPES.

The preview backend (local, staging, tests) refuses unregistered types, so a
missing entry silently drops the email there while SMTP still sends it.
Found in 1.2.6 customer acceptance: payment_failed was refused with
'Unsupported email type.'.
"""
import re
from pathlib import Path

import email_service

APP = Path(__file__).resolve().parents[1]
LITERAL = re.compile(r"""notification_type\s*=\s*["']([a-z_]+)["']|\.send\(\s*["']([a-z_]+)["']""")
CONSTANT = re.compile(r"""^([A-Z_]+_TYPE)\s*=\s*["']([a-z_]+)["']""", re.M)


def _sent_types():
    found = {}
    for path in APP.glob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in LITERAL.finditer(text):
            found.setdefault(match.group(1) or match.group(2), path.name)
        constants = dict(CONSTANT.findall(text))
        for name in re.findall(r"notification_type\s*=\s*([A-Z_]+_TYPE)\b", text):
            if name in constants:
                found.setdefault(constants[name], path.name)
    return found


def test_every_sent_email_type_is_registered():
    sent = _sent_types()
    assert "payment_failed" in sent  # the scan itself still finds the failed-renewal email
    missing = {kind: where for kind, where in sent.items() if kind not in email_service.EMAIL_TYPES}
    assert missing == {}


def test_preview_backend_accepts_payment_failed(tmp_path):
    record = email_service.PreviewEmail(tmp_path).send("payment_failed", "a@example.test", "s", "t")
    assert record["type"] == "payment_failed"
    assert list(tmp_path.glob("*.json"))
