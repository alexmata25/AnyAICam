"""Users & household invitation email delivery (2026-10-02).

Owners saw every invitation listed as "Waiting" while recipients received
nothing: a failed (or preview-mode) send was reported only in a neutral line
at the bottom of the page and was stored nowhere -- not on the invitation,
not in email_messages (so the operator's email-delivery readiness warning
never saw it). Every initial send and resend now records its outcome, the
API says whether an email actually went out, and the page shows it.

All mail here is fake: nothing is sent to any real recipient."""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from test_household_users import (  # noqa: F401 -- fixtures
    OWNER,
    _cookie,
    _home,
    _invite,
    _join,
    _login,
    _q,
    _token,
    db_path,
    mail,
    portal,
)


class _Outbox:
    """A fake email backend: 'sent', a provider-reported failure (what the
    real SMTPEmail returns for e.g. SMTP 535) or a raised exception."""

    def __init__(self):
        self.mode = "sent"
        self.messages = []

    def send(self, message_type, to, subject, text, html=None, metadata=None, images=None):
        if self.mode == "raise":
            raise RuntimeError("connection refused")
        self.messages.append({"type": message_type, "to": to, "subject": subject, "text": text, "html": html})
        if self.mode == "provider_failed":
            return {"type": message_type, "to": to, "status": "failed",
                    "error": "(535, b'5.7.8 Username and Password not accepted')"}
        return {"type": message_type, "to": to, "status": "sent"}


@pytest.fixture()
def outbox(monkeypatch):
    box = _Outbox()
    import email_service
    import household_users
    monkeypatch.setattr(email_service, "get_email_service", lambda: box)
    monkeypatch.setenv("ANYAICAM_PUBLIC_URL", "https://portal.example.test")
    for limiter in (household_users._invite_limiter, household_users._join_ip_limiter):
        limiter.events.clear()
    return box


def _invitation(db_path, email="maria@example.test"):
    return _q(db_path, "SELECT * FROM invitations WHERE email=? ORDER BY created_at DESC", (email,))[0]


def _email_records(db_path):
    return _q(db_path, "SELECT * FROM email_messages WHERE message_type='invitation' ORDER BY rowid")


def _allow_resend(db_path, invitation_id):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE invitations SET last_sent_at=? WHERE id=?", ((datetime.now() - timedelta(minutes=5)).isoformat(), invitation_id))
    conn.commit()
    conn.close()


def _resend(client, invitation_id):
    return client.post(f"/api/customer/household/invitations/{invitation_id}/resend", cookies=_cookie(*OWNER))


def _overview(client):
    return client.get("/api/customer/household", cookies=_cookie(*OWNER)).json()


# ------------------------------------------------------------------ initial invitation

def test_the_initial_invitation_is_emailed_and_its_delivery_recorded(portal, db_path, outbox):
    client, _, _ = portal
    _home(db_path)
    response = _invite(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email_status"] == "sent" and body["email_sent"] is True
    assert body["message"] == "Invitation sent to maria@example.test."
    assert len(outbox.messages) == 1 and outbox.messages[0]["to"] == "maria@example.test"
    assert "https://portal.example.test/customer/join?token=" in outbox.messages[0]["text"]
    row = _invitation(db_path)
    assert row["email_status"] == "sent" and row["email_error"] is None and row["email_attempted_at"]
    records = _email_records(db_path)
    assert len(records) == 1 and records[0]["recipient"] == "maria@example.test" and records[0]["status"] == "sent"
    metadata = json.loads(records[0]["metadata_json"])
    assert metadata == {"kind": "household", "invitation_id": row["id"], "resend": False}
    # Never the link or its token in the delivery record.
    assert "token" not in records[0]["metadata_json"] and _token(outbox.messages) not in json.dumps(records[0])
    listed = _overview(client)["invitations"][0]
    assert listed["email_status"] == "sent" and listed["email_attempted_at"]


# ------------------------------------------------------------------ delivery failure

@pytest.mark.parametrize("mode", ["provider_failed", "raise"])
def test_a_failed_send_is_never_reported_as_sent(portal, db_path, outbox, mode):
    client, _, _ = portal
    _home(db_path)
    outbox.mode = mode
    response = _invite(client)
    assert response.status_code == 200, response.text  # the invitation itself was created
    body = response.json()
    assert body["email_status"] == "failed" and body["email_sent"] is False
    assert "sent to" not in body["message"] and "have not received it" in body["message"]
    row = _invitation(db_path)
    assert row["status"] == "pending" and row["email_status"] == "failed"
    assert ("535" in row["email_error"]) if mode == "provider_failed" else ("connection refused" in row["email_error"])
    record = _email_records(db_path)[-1]
    assert record["status"] == "failed" and json.loads(record["metadata_json"])["error"] == row["email_error"]
    # The owner sees the failure on the invitation; the provider's raw reason stays operator-side.
    listed = _overview(client)["invitations"][0]
    assert listed["email_status"] == "failed" and "email_error" not in listed


def test_the_operator_readiness_check_sees_a_failed_invitation_email(portal, db_path, outbox):
    import main
    client, _, _ = portal
    _home(db_path)
    outbox.mode = "provider_failed"
    _invite(client)
    last = main._last_email_delivery()
    assert last and last["kind"] == "account" and last["status"] == "failed" and "535" in last["error"]


def test_try_again_after_a_failure_delivers_a_working_link(portal, db_path, outbox):
    client, _, _ = portal
    _home(db_path)
    outbox.mode = "provider_failed"
    _invite(client)
    invitation_id = _invitation(db_path)["id"]
    failed_link = _token(outbox.messages)
    outbox.mode = "sent"
    _allow_resend(db_path, invitation_id)
    response = _resend(client, invitation_id)
    assert response.status_code == 200, response.text
    assert response.json()["email_sent"] is True and response.json()["message"] == "Invitation sent again to maria@example.test."
    row = _invitation(db_path)
    assert row["email_status"] == "sent" and row["email_error"] is None
    assert [r["status"] for r in _email_records(db_path)] == ["failed", "sent"]
    assert json.loads(_email_records(db_path)[-1]["metadata_json"])["resend"] is True
    # The link from the failed attempt no longer works; the delivered one does.
    assert _join(client, failed_link).status_code == 400
    assert _join(client, _token(outbox.messages)).status_code == 200


# ------------------------------------------------------------------ resend

def test_resend_emails_a_new_link_and_records_it(portal, db_path, outbox):
    client, _, _ = portal
    _home(db_path)
    _invite(client)
    invitation_id = _invitation(db_path)["id"]
    first = _token(outbox.messages)
    _allow_resend(db_path, invitation_id)
    response = _resend(client, invitation_id)
    assert response.status_code == 200 and response.json()["email_status"] == "sent"
    assert len(outbox.messages) == 2 and outbox.messages[1]["to"] == "maria@example.test"
    second = _token(outbox.messages)
    assert second != first
    assert _invitation(db_path)["send_count"] == 2
    assert len(_email_records(db_path)) == 2


def test_a_failed_resend_says_so(portal, db_path, outbox):
    client, _, _ = portal
    _home(db_path)
    _invite(client)
    invitation_id = _invitation(db_path)["id"]
    outbox.mode = "raise"
    _allow_resend(db_path, invitation_id)
    response = _resend(client, invitation_id)
    assert response.status_code == 200
    assert response.json()["email_sent"] is False and "could not be sent" in response.json()["message"]
    assert _invitation(db_path)["email_status"] == "failed"


def test_resend_is_still_rate_limited_and_capped(portal, db_path, outbox):
    import household_users
    client, _, _ = portal
    _home(db_path)
    _invite(client)
    invitation_id = _invitation(db_path)["id"]
    assert _resend(client, invitation_id).status_code == 400  # within a minute
    for _ in range(household_users.MAX_SENDS_PER_INVITATION - 1):
        _allow_resend(db_path, invitation_id)
        assert _resend(client, invitation_id).status_code == 200
    _allow_resend(db_path, invitation_id)
    capped = _resend(client, invitation_id)
    assert capped.status_code == 400 and "maximum number of times" in capped.json()["detail"]
    assert len(outbox.messages) == household_users.MAX_SENDS_PER_INVITATION


def test_another_household_can_not_resend_this_invitation(portal, db_path, outbox):
    client, _, _ = portal
    _home(db_path)
    _invite(client)
    invitation_id = _invitation(db_path)["id"]
    _home(db_path, customer_id="cust-2", email="other@example.test", door_cameras=())
    _allow_resend(db_path, invitation_id)
    other = client.post(f"/api/customer/household/invitations/{invitation_id}/resend",
                        cookies=_cookie("other@example.test", "customer_owner", "cust-2"))
    assert other.status_code == 404
    assert len(outbox.messages) == 1 and _invitation(db_path)["send_count"] == 1


# ------------------------------------------------------------------ expiration, replay, acceptance

def test_an_expired_invitation_can_be_resent_and_the_old_link_stays_dead(portal, db_path, outbox):
    client, _, _ = portal
    _home(db_path)
    _invite(client)
    invitation_id = _invitation(db_path)["id"]
    expired_link = _token(outbox.messages)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE invitations SET expires_at=?,last_sent_at=? WHERE id=?",
                 ((datetime.now() - timedelta(minutes=1)).isoformat(), (datetime.now() - timedelta(days=8)).isoformat(), invitation_id))
    conn.commit()
    conn.close()
    assert _overview(client)["invitations"][0]["status"] == "expired"
    assert _join(client, expired_link).status_code == 400
    assert _resend(client, invitation_id).json()["email_sent"] is True
    assert datetime.fromisoformat(_invitation(db_path)["expires_at"]) > datetime.now() + timedelta(days=6)
    assert _join(client, expired_link).status_code == 400
    assert _join(client, _token(outbox.messages)).status_code == 200


def test_the_emailed_link_is_accepted_once_and_cannot_be_replayed(portal, db_path, outbox):
    client, _, _ = portal
    _home(db_path)
    _invite(client)
    link = _token(outbox.messages)
    assert _join(client, link).status_code == 200
    assert _login(client).status_code == 303
    replay = _join(client, link, password="a different long password")
    assert replay.status_code == 400 and "invalid, expired, or already used" in replay.json()["detail"]
    assert _invitation(db_path)["status"] != "pending"
    # An accepted invitation can not be re-sent (no fresh link for a used invitation).
    _allow_resend(db_path, _invitation(db_path)["id"])
    assert _resend(client, _invitation(db_path)["id"]).status_code == 400
    assert len(outbox.messages) == 1


def test_inviting_the_same_person_again_replaces_the_earlier_link(portal, db_path, outbox):
    client, _, _ = portal
    _home(db_path)
    _invite(client)
    first = _token(outbox.messages)
    assert _invite(client).json()["email_sent"] is True
    assert _join(client, first).status_code == 400
    assert _join(client, _token(outbox.messages)).status_code == 200


# ------------------------------------------------------------------ page

def test_the_page_shows_delivery_state_and_failures_as_warnings(portal, db_path, outbox):
    client, _, _ = portal
    _home(db_path)
    html = client.get("/customer/household", cookies=_cookie(*OWNER)).text
    assert "'Email not sent'" in html and "'Email sent'" in html and "Try again" in html
    assert "say(r.message,r.email_sent!==false)" in html
    # The status line sits above the lists, where the owner is looking.
    assert html.index('id="hh-message"') < html.index('id="hh-users"')


def test_the_household_page_script_is_valid_javascript(tmp_path):
    import re
    import shutil
    import subprocess

    import household_users
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    script = tmp_path / "household.js"
    script.write_text(re.search(r"<script>(.*)</script>", household_users._HOUSEHOLD_SCRIPT, re.S).group(1), encoding="utf-8")
    done = subprocess.run([node, "--check", str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[:300]
