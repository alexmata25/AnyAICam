"""Household (invited user) logout with CSRF protection on (2026-10-03).

Staging: an invited Household member's Log out showed a plain page reading
"CSRF validation failed". Root cause: the shell's Log out is a plain
<form method="post" action="/logout"> whose hidden csrf_token is filled from
the anyaicam_csrf cookie by a script on submit. That script bound itself with
querySelectorAll('.logout-form') where it ran -- before the mobile
navigation's Log out form existed in the page -- so the phone-width Log out
posted an empty token and the CSRF check (correctly) refused it. Owners on a
phone hit the same thing. Fixed in the script only (one document-level
submit listener); the server-side CSRF check is unchanged and still refuses
a Log out without a valid token.

The full real-browser journey (desktop and phone, invited member and owner)
is tests/test_household_logout_browser.py (opt-in).
"""
import dataclasses
import json
import re
import shutil
import subprocess

import pytest

from test_household_users import (  # noqa: F401 -- fixtures
    OWNER,
    PASSWORD,
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

MEMBER = "maria@example.test"


def _signed_in_member(portal, db_path, mail, monkeypatch):
    """The owner's invitation, then -- with CSRF enforcement ON, as in
    staging -- the invited person's password via the join API and a real
    sign-in; the client then holds the member's session and anyaicam_csrf."""
    import cloud_security
    client, _, _ = portal
    _home(db_path)
    assert _invite(client).status_code == 200
    monkeypatch.setattr(cloud_security, "settings", dataclasses.replace(cloud_security.settings, csrf_enabled=True))
    client.cookies.clear()
    page = client.get("/customer/join", params={"token": _token(mail)})
    assert page.status_code == 200 and client.cookies.get("anyaicam_csrf")
    csrf = client.cookies.get("anyaicam_csrf")
    assert client.post("/api/customer/household/join", headers={"X-CSRF-Token": csrf},
                       json={"token": _token(mail), "password": PASSWORD, "confirm_password": PASSWORD}).status_code == 200
    login = client.post("/api/partner-login", headers={"X-CSRF-Token": csrf},
                        json={"email": MEMBER, "password": PASSWORD, "customer_only": True})
    assert login.status_code == 303, login.text  # signed in, sent on to the portal
    return client


def _live_sessions(db_path, email):
    return _q(db_path, "SELECT s.id FROM user_sessions s JOIN partner_users u ON u.id=s.user_id "
                       "WHERE u.email=? AND s.revoked_at IS NULL", (email,))


def test_an_invited_household_member_logs_out_with_the_form_token(portal, db_path, mail, monkeypatch):
    import main
    client = _signed_in_member(portal, db_path, mail, monkeypatch)
    assert client.get("/customer-account").status_code == 200  # normal household navigation
    assert _live_sessions(db_path, MEMBER)
    # Exactly what the Log out form sends once its token is filled from the cookie.
    response = client.post("/logout", data={"csrf_token": client.cookies.get("anyaicam_csrf")})
    assert response.status_code == 303
    assert response.headers["location"] == main.CUSTOMER_LOGOUT_DESTINATION
    assert not _live_sessions(db_path, MEMBER)  # revoked on the server, not only forgotten
    protected = client.get("/customer/household")
    assert protected.status_code == 303 and "customer-login" in protected.headers["location"]


def test_csrf_still_refuses_a_log_out_without_the_token(portal, db_path, mail, monkeypatch):
    client = _signed_in_member(portal, db_path, mail, monkeypatch)
    refused = client.post("/logout", data={"csrf_token": ""})
    assert refused.status_code == 403 and "CSRF validation failed" in refused.text
    assert _live_sessions(db_path, MEMBER)  # nothing was signed out by the refused request


def test_the_log_out_script_fills_the_token_for_a_form_added_after_it_ran(portal, db_path, mail, monkeypatch, tmp_path):
    """Runs the page's real Log out script in Node with a minimal document:
    the mobile Log out form appears only AFTER the script has run (as in the
    page). Its token must still be filled on submit -- the old script left it
    empty. A quoted cookie value is unquoted like the other pages' csrf()."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    client = _signed_in_member(portal, db_path, mail, monkeypatch)
    html = client.get("/customer-account").text
    assert 'class="mobile-logout logout-form"' in html and 'id="logout-form"' in html
    script = next(s for s in re.findall(r"<script>(.*?)</script>", html, re.S) if "logout-form" in s and "csrf_token" in s)
    harness = tmp_path / "logout_harness.js"
    harness.write_text(
        "const handlers=[];const forms=[];\n"
        "global.document={cookie:'other=1; anyaicam_csrf=%22abc.def%22',"
        "addEventListener:(type,fn)=>{if(type==='submit')handlers.push(fn)},"
        "querySelectorAll:(sel)=>sel==='.logout-form'?forms:[]};\n"
        f"{script}\n"
        "const late={classList:{contains:c=>c==='logout-form'},csrf_token:{value:''},addEventListener:()=>{}};\n"
        "forms.push(late);\n"
        "for(const h of handlers)h.call(late,{target:late});\n"
        "console.log(JSON.stringify(late.csrf_token.value));\n",
        encoding="utf-8")
    done = subprocess.run([node, str(harness)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[:300]
    assert json.loads(done.stdout.strip()) == "abc.def"
