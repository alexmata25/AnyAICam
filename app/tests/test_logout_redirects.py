"""Regression coverage for the customer/portal authentication-flow
redirect bugs:

  1. Customer logout must return to the white customer login
     (/customer-login.html), never the blue Partner/Admin/Technician
     portal login.
  2. "Already approved? Sign in" on the customer account-request page
     (/customer-register) must go to /customer-login.html.
  3. Customer Forgot Password must stay entirely in the customer flow
     (new /customer-forgot-password, /customer-reset-password pages),
     never redirecting to or reusing /forgot-password, /reset-password,
     or /partner-login.
  4. Admin/Partner/Salesperson/Technician logout must return to the
     blue portal login (/partner.html).

Root cause traced: POST /logout (main.py) is the single shared logout
form action page_shell()'s sidebar renders on every page -- legacy
Admin Portal pages, Partner/Admin/Salesperson/Technician pages (which
also use page_shell() as their chrome), and customer portal pages
alike. It hardcoded RedirectResponse("/login", ...) regardless of who
was logging out, and only ever cleared the legacy admin cookie -- a
customer or partner-role session's real cookie (partner_portal.
SESSION_COOKIE) was never cleared at all. Fixed by making the route
identity-aware (logout_destination()) and clearing whichever cookie(s)
were actually present.

Also found and fixed while verifying "no redirect loops": /partner.html,
/forgot-password, and /reset-password were not in PUBLIC_PATH_PREFIXES,
so an unauthenticated visitor -- which is what everyone hitting a login
or forgot-password page necessarily is -- was bounced straight to
/login before ever seeing them. Redirecting a customer to /customer-
login.html only avoids the bug if that page itself doesn't then bounce
them again; the same is true for /partner.html.
"""

import sqlite3

import pytest

import cloud_config
import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database


class _StubRequest:
    headers: dict = {}


def _stub_request():
    return _StubRequest()


# =============================================================== pure logic: logout_destination()


@pytest.mark.parametrize("portal_role", ["customer_owner", "customer_viewer"])
def test_customer_portal_role_goes_to_the_external_marketing_homepage(portal_role):
    # A real customer_owner/customer_viewer identity logs all the way
    # out to the public marketing site -- not back to any sign-in page
    # inside this app. See logout_destination()'s own docstring.
    assert main.logout_destination(None, portal_role) == "https://anyaicam.com/"
    assert main.logout_destination(None, portal_role) == main.CUSTOMER_LOGOUT_DESTINATION


@pytest.mark.parametrize("portal_role", ["administrator", "partner_owner", "salesperson", "technician"])
def test_partner_portal_role_goes_to_portal_login(portal_role):
    assert main.logout_destination(None, portal_role) == "/partner.html"


@pytest.mark.parametrize("legacy_role", ["administrator", "admin", "support_admin", "installer"])
def test_legacy_admin_only_session_goes_to_portal_login(legacy_role):
    assert main.logout_destination(legacy_role, None) == "/partner.html"


@pytest.mark.parametrize("legacy_role", ["customer_owner", "customer_viewer"])
def test_legacy_customer_role_never_goes_to_portal_login(legacy_role):
    # A vestigial customer_owner/customer_viewer row in the old JSON
    # store must still never be sent to the blue portal login -- and,
    # like the real Partner Portal customer identity, goes all the way
    # out to the external marketing homepage now, not a local page.
    assert main.logout_destination(legacy_role, None) == "https://anyaicam.com/"


def test_no_identity_at_all_defaults_to_customer_login_not_portal():
    # Fail-safe direction for the case nothing meaningful was actually
    # logged out: local customer sign-in page, never the blue portal
    # login, and never the external homepage either -- that's reserved
    # for a real customer identity that was actually present.
    assert main.logout_destination(None, None) == "/customer-login.html"


def test_customer_portal_identity_wins_even_if_legacy_side_looks_like_admin():
    # An inconsistent dual-cookie state (shouldn't normally happen, but
    # must still fail toward never leaking a customer to the portal
    # login) -- the customer signal on either side always wins, sending
    # them to the external homepage same as any other customer logout.
    assert main.logout_destination("administrator", "customer_owner") == "https://anyaicam.com/"


# =============================================================== real HTTP: POST /logout


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_logout.db"


@pytest.fixture()
def http_client(tmp_path, monkeypatch, db_path):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client


def _seed_partner_session(db_path, *, email, role, customer_id=None, partner_id=None):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO user_sessions(id,user_id,email,role,device_name,session_type,created_at,last_seen_at,expires_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("sess-1", None, email, role, "Web browser", "cookie", "2026-01-01T00:00:00", "2026-01-01T00:00:00", "2099-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()
    return partner_portal._token(email, role, partner_id, customer_id, "sess-1")


def _admin_session_cookie(admin_id="admin-1", role="administrator"):
    main.save_users([{"id": admin_id, "email": "admin@example.test", "role": role, "enabled": True, "camera_ids": []}])
    return main.create_session(admin_id)


@pytest.mark.parametrize("role", ["customer_owner", "customer_viewer"])
def test_customer_logout_redirects_to_the_external_homepage_and_destroys_the_session(http_client, db_path, role):
    """The exact behavior this task confirmed as intended: customer_owner
    and customer_viewer both log all the way out (server-side session
    revoked, not just the cookie dropped client-side) and land on the
    public https://anyaicam.com/ homepage, never a sign-in page inside
    this app."""
    token = _seed_partner_session(db_path, email=f"{role}@example.test", role=role, customer_id="cust-1")
    response = http_client.post("/logout", cookies={partner_portal.SESSION_COOKIE: token})
    assert response.status_code == 303
    assert response.headers["location"] == "https://anyaicam.com/"
    assert response.headers["location"] == main.CUSTOMER_LOGOUT_DESTINATION
    set_cookie = response.headers.get("set-cookie", "")
    assert partner_portal.SESSION_COOKIE in set_cookie  # the real cookie is actually being cleared
    revoked = sqlite3.connect(db_path).execute("SELECT revoked_at FROM user_sessions WHERE id='sess-1'").fetchone()[0]
    assert revoked is not None  # server-side session revoked, not just the cookie dropped client-side


@pytest.mark.parametrize("role", ["customer_owner", "customer_viewer"])
def test_customer_logout_old_cookie_is_rejected_on_the_very_next_request(http_client, db_path, role):
    """Belt-and-suspenders on top of the revoked_at check above: the
    exact cookie just logged out with must be genuinely unusable
    afterward against a real protected route, not merely marked
    revoked in a column nothing reads."""
    token = _seed_partner_session(db_path, email=f"{role}@example.test", role=role, customer_id="cust-1")
    http_client.post("/logout", cookies={partner_portal.SESSION_COOKIE: token})
    still_works = http_client.get("/customer-account", cookies={partner_portal.SESSION_COOKIE: token})
    assert still_works.status_code in (303, 401, 403)  # denied -- the old cookie is genuinely dead, not just redirected home


@pytest.mark.parametrize("role", ["administrator", "partner_owner", "salesperson", "technician"])
def test_portal_role_logout_redirects_to_portal_login(http_client, db_path, role):
    token = _seed_partner_session(db_path, email=f"{role}@example.test", role=role, partner_id="partner-1")
    response = http_client.post("/logout", cookies={partner_portal.SESSION_COOKIE: token})
    assert response.status_code == 303
    assert response.headers["location"] == "/partner.html"


def test_legacy_admin_logout_redirects_to_portal_login_and_clears_legacy_cookie(http_client):
    token = _admin_session_cookie()
    response = http_client.post("/logout", cookies={main.SESSION_COOKIE_NAME: token})
    assert response.status_code == 303
    assert response.headers["location"] == "/partner.html"
    assert main.SESSION_COOKIE_NAME in response.headers.get("set-cookie", "")


def test_logout_with_no_session_at_all_does_not_crash_and_defaults_to_customer_login(http_client):
    response = http_client.post("/logout")
    assert response.status_code == 303
    assert response.headers["location"] == "/customer-login.html"


def test_logout_remains_reachable_through_authentication_middleware():
    # PUBLIC_PATH_PREFIXES is what authentication_middleware consults
    # to decide whether a request may proceed without an established
    # session -- /logout must stay listed so a customer whose session
    # already expired (or who never had one) can still POST here
    # without being bounced by the middleware itself before
    # logout_destination()'s own fail-safe default ever runs.
    assert "/logout" in main.PUBLIC_PATH_PREFIXES


def test_customer_logout_never_touches_partner_role_session_data(http_client, db_path):
    """Cross-role leakage check: logging out a customer session must
    never revoke or otherwise affect a *different* session belonging to
    a partner-role account."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO user_sessions(id,user_id,email,role,device_name,session_type,created_at,last_seen_at,expires_at) "
        "VALUES('sess-partner',NULL,'tech@example.test','technician','Web browser','cookie','2026-01-01T00:00:00','2026-01-01T00:00:00','2099-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()
    customer_token = _seed_partner_session(db_path, email="owner@example.test", role="customer_owner", customer_id="cust-1")
    http_client.post("/logout", cookies={partner_portal.SESSION_COOKIE: customer_token})
    partner_session_untouched = sqlite3.connect(db_path).execute(
        "SELECT revoked_at FROM user_sessions WHERE id='sess-partner'"
    ).fetchone()[0]
    assert partner_session_untouched is None


# =============================================================== no redirect loops: destinations actually render


def test_customer_login_page_reachable_unauthenticated_no_bounce(http_client):
    response = http_client.get("/customer-login.html")
    assert response.status_code == 200


def test_portal_login_page_reachable_unauthenticated_no_bounce(http_client):
    # Found while verifying this: /partner.html was NOT in
    # PUBLIC_PATH_PREFIXES, so an unauthenticated visitor -- exactly who
    # a post-logout redirect here produces -- was bounced to /login
    # before ever seeing it. A customer/portal logout landing here must
    # not chain into a second, unwanted redirect.
    response = http_client.get("/partner.html")
    assert response.status_code == 200


def test_customer_logout_destination_is_external_not_a_local_hop(http_client, db_path):
    # Supersedes the old "single hop" check for the customer case: the
    # destination is now https://anyaicam.com/, a page this app does
    # not and should not serve itself -- the only thing to verify here
    # is that the redirect target really is that absolute external URL
    # (already asserted in detail above), not something this app could
    # attempt to GET against its own TestClient.
    token = _seed_partner_session(db_path, email="owner@example.test", role="customer_owner", customer_id="cust-1")
    logout_response = http_client.post("/logout", cookies={partner_portal.SESSION_COOKIE: token})
    assert logout_response.headers["location"].startswith("https://anyaicam.com/")


def test_logout_then_get_portal_login_is_a_single_hop(http_client, db_path):
    token = _seed_partner_session(db_path, email="tech@example.test", role="technician", partner_id="partner-1")
    logout_response = http_client.post("/logout", cookies={partner_portal.SESSION_COOKIE: token})
    destination = http_client.get(logout_response.headers["location"])
    assert destination.status_code == 200


# =============================================================== "Already approved? Sign in"


def test_customer_register_page_already_approved_link_goes_to_customer_login(http_client):
    response = http_client.get("/customer-register")
    assert response.status_code == 200
    assert 'href="/customer-login.html">Already approved? Sign in</a>' in response.text
    assert 'href="/login">Already approved? Sign in</a>' not in response.text


# =============================================================== customer Forgot Password stays in the customer flow


def test_customer_login_html_forgot_password_link_points_to_the_customer_flow():
    text = (main.__file__.rsplit("main.py", 1)[0] + "customer-login.html")
    from pathlib import Path
    content = Path(text).read_text(encoding="utf-8")
    assert 'href="/customer-forgot-password">Forgot password?</a>' in content
    assert 'href="/forgot-password"' not in content


def test_customer_forgot_password_page_reachable_unauthenticated_and_customer_branded(http_client):
    response = http_client.get("/customer-forgot-password")
    assert response.status_code == 200
    assert "ANY AI CAM" in response.text
    assert "/customer-login.html" in response.text
    # Never pulls in the dark Admin/Partner Portal shell() chrome.
    assert "sidebar-logout" not in response.text
    assert '/partner-login' not in response.text


def test_customer_reset_password_page_redirects_back_to_customer_login_on_success(http_client):
    response = http_client.get("/customer-reset-password?token=abc123")
    assert response.status_code == 200
    assert "location.href='/customer-login.html'" in response.text
    assert "/partner-login" not in response.text
    assert "abc123" in response.text  # token is threaded through to the hidden field


def test_customer_forgot_password_posts_to_the_existing_role_agnostic_api(http_client):
    response = http_client.get("/customer-forgot-password")
    assert "/api/password-reset/request" in response.text


def test_partner_side_forgot_password_flow_is_unchanged_and_still_reachable(http_client):
    # Regression: the existing partner/admin forgot-password flow must
    # keep working exactly as before -- only the customer side gets its
    # own separate pages.
    forgot = http_client.get("/forgot-password")
    assert forgot.status_code == 200
    reset = http_client.get("/reset-password?token=xyz")
    assert reset.status_code == 200
    assert "location.href='/partner-login'" in reset.text
