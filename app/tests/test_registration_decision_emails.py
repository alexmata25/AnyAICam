"""People who ask for an account on /customer-register are told the outcome (Green 2026-10-17).

Approval emailed the requester only when a purchase was attached, and rejection emailed nothing, while the page tells
them to sign in "once yours is approved". Now an approval sends "Your AnyAiCam account is approved" (sign-in link)
and a rejection a neutral "we weren't able to approve" note that never includes the administrator's internal reason.
Each is sent once per request; an email failure never undoes or blocks the decision.
"""
import pytest

import customer_registration as cr
import email_service
from test_registration_email_verification_127 import EMAIL, MASTER, PASSWORD, _request, db  # noqa: F401 -- fixture


@pytest.fixture()
def mail(monkeypatch):
    import purchase_notifications
    sent, failing = [], []

    class _Mail:
        def send(self, message_type, to, subject, text, html=None, metadata=None, images=None):
            assert message_type in email_service.EMAIL_TYPES, message_type
            if failing:
                raise OSError("smtp unavailable")
            sent.append({"type": message_type, "to": to, "subject": subject, "text": text, "html": html or ""})
            return {"status": "sent"}
    monkeypatch.setattr(purchase_notifications, "get_email_service", lambda: _Mail())
    monkeypatch.setattr(email_service, "get_email_service", lambda: _Mail())
    return {"sent": sent, "failing": failing}


def _verified_request(name="Real Buyer"):
    cr.create_pending_registration(name, EMAIL, PASSWORD)
    raw = cr.issue_registration_verification(EMAIL)
    assert raw and cr.verify_registration_email(raw, PASSWORD, name)


def _decisions(mail):
    return [m for m in mail["sent"] if m["type"] in ("registration_approved", "registration_rejected")]


def test_an_approved_requester_is_emailed_once_with_a_sign_in_link(db, mail):
    _verified_request()
    item = _request(db)
    cr.approve_registration(item["id"], "partner-a", MASTER)
    cr.approve_registration(item["id"], "partner-a", MASTER)  # repeated click: "already approved", no second email
    sent = _decisions(mail)
    assert [m["type"] for m in sent] == ["registration_approved"]
    assert sent[0]["to"] == EMAIL and sent[0]["subject"] == "Your AnyAiCam account is approved"
    assert "/customer-login.html" in sent[0]["text"]


def test_a_rejected_requester_is_told_without_the_internal_reason(db, mail):
    _verified_request()
    item = _request(db)
    cr.reject_registration(item["id"], "INTERNAL: looks like a reseller, do not tell them", MASTER)
    sent = _decisions(mail)
    assert [m["type"] for m in sent] == ["registration_rejected"]
    assert sent[0]["to"] == EMAIL
    assert "weren't able to approve" in sent[0]["text"]
    assert "INTERNAL" not in sent[0]["text"] + sent[0]["html"] and "reseller" not in sent[0]["text"] + sent[0]["html"]


def test_an_email_failure_never_blocks_the_decision(db, mail):
    _verified_request()
    mail["failing"].append(True)
    result = cr.approve_registration(_request(db)["id"], "partner-a", MASTER)
    assert result["status"] == "complete" and _request(db)["status"] == "approved"


def test_a_decision_email_is_only_sent_for_the_saved_state(db, mail):
    import purchase_notifications as pn
    cr.create_pending_registration("Still Pending", EMAIL, PASSWORD)
    assert pn.notify_registration_decision(_request(db)["id"], "approved")["status"] == "ignored"
    assert pn.notify_registration_decision(_request(db)["id"], "maybe")["status"] == "ignored"
    assert _decisions(mail) == []
