"""Household invitations at the resend limit, and invitations from before
delivery tracking (2026-10-03).

Found on staging: an owner clicked Resend on one person's invitation nine
times and got 400 every time -- it had reached MAX_SENDS_PER_INVITATION, but
the page still offered Resend. Its sends predated delivery tracking, so the
page also showed no delivery status at all. The server-side cap is unchanged;
the page now offers Invite again (a fresh invitation that replaces the old
one) and says "Delivery not recorded" for an unknown outcome.

All mail is fake; nothing is sent to anyone.
"""
import json
import re
import shutil
import sqlite3
import subprocess

import pytest

from test_household_invitation_delivery import _invitation, outbox  # noqa: F401 -- fixture
from test_household_users import (  # noqa: F401 -- fixtures
    OWNER,
    _cookie,
    _home,
    _invite,
    _q,
    _token,
    db_path,
    mail,
    portal,
)

import household_users

MARIA = "maria@example.test"


def _set(db_path, invitation_id, **fields):
    conn = sqlite3.connect(db_path)
    conn.execute(f"UPDATE invitations SET {','.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), invitation_id))
    conn.commit()
    conn.close()


def _listed(client, invitation_id):
    data = client.get("/api/customer/household", cookies=_cookie(*OWNER)).json()
    return data, next(i for i in data["invitations"] if i["id"] == invitation_id)


def _capped(portal, db_path):
    client, _, _ = portal
    _home(db_path)
    assert _invite(client).status_code == 200
    invitation = _invitation(db_path)
    _set(db_path, invitation["id"], send_count=household_users.MAX_SENDS_PER_INVITATION)
    return client, invitation


def _rendered_invites(data, tmp_path) -> str:
    """The page's real script, run in Node on the real API response; returns
    what it renders into the invitations list."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    script = re.search(r"<script>(.*)</script>", household_users._HOUSEHOLD_SCRIPT, re.S).group(1)
    harness = tmp_path / "household_render.js"
    harness.write_text(
        "const els={};const el=id=>els[id]||(els[id]={id,style:{},textContent:'',innerHTML:'',value:'',checked:false,"
        "showModal(){},close(){},addEventListener(){},querySelectorAll(){return[]}});\n"
        "global.document={getElementById:el,addEventListener(){},querySelectorAll:()=>[],cookie:''};\n"
        "global.window=global;global.confirm=()=>true;\n"
        f"const DATA={json.dumps(data)};\n"
        "global.fetch=async()=>({ok:true,status:200,json:async()=>DATA});\n"
        f"{script}\n"
        "setTimeout(()=>console.log(JSON.stringify(els['hh-invites'].innerHTML)),100);\n",
        encoding="utf-8")
    done = subprocess.run([node, str(harness)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-700:]
    return json.loads(done.stdout.strip())


def test_a_capped_invitation_is_flagged_and_shows_invite_again_not_resend(portal, db_path, outbox, tmp_path):
    client, invitation = _capped(portal, db_path)
    data, listed = _listed(client, invitation["id"])
    assert listed["resend_limit_reached"] is True and listed["status"] == "pending"
    html = _rendered_invites(data, tmp_path)
    assert f'data-again="{invitation["id"]}"' in html and ">Invite again<" in html
    assert 'data-inv="resend"' not in html
    assert "reached the resend limit" in html


def test_an_invitation_below_the_cap_still_offers_resend(portal, db_path, outbox, tmp_path):
    client, _, _ = portal
    _home(db_path)
    assert _invite(client).status_code == 200
    invitation = _invitation(db_path)
    data, listed = _listed(client, invitation["id"])
    assert listed["resend_limit_reached"] is False and listed["delivery_recorded"] is True
    html = _rendered_invites(data, tmp_path)
    assert 'data-inv="resend"' in html and "data-again" not in html and "Email sent" in html


def test_the_server_still_refuses_a_resend_past_the_cap(portal, db_path, outbox):
    client, invitation = _capped(portal, db_path)
    sent_before = len(outbox.messages)
    refused = client.post(f"/api/customer/household/invitations/{invitation['id']}/resend", cookies=_cookie(*OWNER))
    assert refused.status_code == 400 and "maximum number of times" in refused.json()["detail"]
    assert len(outbox.messages) == sent_before  # nothing was emailed
    assert _invitation(db_path)["send_count"] == household_users.MAX_SENDS_PER_INVITATION


def test_invite_again_replaces_the_capped_invitation_with_a_fresh_tracked_one(portal, db_path, outbox):
    client, old = _capped(portal, db_path)
    old_link = _token(outbox.messages)
    again = _invite(client)  # what Invite again submits: same person, same permissions
    assert again.status_code == 200 and again.json()["email_sent"] is True
    rows = {r["id"]: r for r in _q(db_path, "SELECT * FROM invitations WHERE email=?", (MARIA,))}
    assert rows[old["id"]]["status"] == "cancelled"
    [fresh] = [r for r in rows.values() if r["id"] != old["id"]]
    assert fresh["status"] == "pending" and fresh["send_count"] == 1
    assert fresh["email_status"] == "sent" and fresh["email_attempted_at"]
    recorded = _q(db_path, "SELECT status, metadata_json FROM email_messages WHERE message_type='invitation'")
    assert any(json.loads(r["metadata_json"])["invitation_id"] == fresh["id"] and r["status"] == "sent" for r in recorded)
    # Only the new link works.
    new_link = _token(outbox.messages)
    assert new_link != old_link
    from database_backend import override_target
    from partner_db import connection
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            assert household_users.invitation_for_token(db, old_link, household_users._now()) is None
            assert household_users.invitation_for_token(db, new_link, household_users._now())["id"] == fresh["id"]
    _, listed = _listed(client, fresh["id"])
    assert listed["resend_limit_reached"] is False and listed["delivery_recorded"] is True


def test_invite_again_works_even_when_the_waiting_limit_is_full(portal, db_path, outbox, monkeypatch):
    """The capped invitation it replaces never counts toward the waiting limit."""
    client, old = _capped(portal, db_path)
    monkeypatch.setattr(household_users, "MAX_PENDING_INVITATIONS", 1)  # Maria's own invitation fills it
    other = client.post("/api/customer/household/invitations", cookies=_cookie(*OWNER),
                        json={"name": "Other", "email": "other@example.test"})
    assert other.status_code == 400 and "Too many invitations" in other.json()["detail"]
    assert _invite(client).status_code == 200  # Invite again for Maria replaces hers


def test_an_invitation_from_before_delivery_tracking_says_delivery_not_recorded(portal, db_path, outbox, tmp_path):
    client, _, _ = portal
    _home(db_path)
    assert _invite(client).status_code == 200
    invitation = _invitation(db_path)
    _set(db_path, invitation["id"], email_status=None, email_attempted_at=None)  # as sent by a pre-tracking build
    data, listed = _listed(client, invitation["id"])
    assert listed["delivery_recorded"] is False
    html = _rendered_invites(data, tmp_path)
    assert "Delivery not recorded" in html and "whether it was emailed is not recorded" in html
    assert "Email sent" not in html
