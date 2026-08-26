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
def test_customer_portal_role_always_goes_to_customer_login(portal_role):
    assert main.logout_destination(None, portal_role) == "/customer-login.html"


@pytest.mark.parametrize("portal_role", ["administrator", "partner_owner", "salesperson", "technician"])
def test_partner_portal_role_goes_to_portal_login(portal_role):
    assert main.logout_destination(None, portal_role) == "/partner.html"


@pytest.mark.parametrize("legacy_role", ["administrator", "admin", "support_admin", "installer"])
def test_legacy_admin_only_session_goes_to_portal_login(legacy_role):
    assert main.logout_destination(legacy_role, None) == "/partner.html"


@pytest.mark.parametrize("legacy_role", ["customer_owner", "customer_viewer"])
def test_legacy_customer_role_never_goes_to_portal_login(legacy_role):
    # A vestigial customer_owner/customer_viewer row in the old JSON
    # store must still never be sent to the blue portal login.
    assert main.logout_destination(legacy_role, None) == "/customer-login.html"


def test_no_identity_at_all_defaults_to_customer_login_not_portal():
    # Fail-safe direction: an ambiguous/absent identity must never
    # default to the blue portal login.
    assert main.logout_destination(None, None) == "/customer-login.html"


def test_customer_portal_identity_wins_even_if_legacy_side_looks_like_admin():
    # An inconsistent dual-cookie state (shouldn't normally happen, but
    # must still fail toward never leaking a customer to the portal
    # login) -- the customer signal on either side always wins.
    assert main.logout_destination("administrator", "customer_owner") == "/customer-login.html"


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


def test_customer_logout_redirects_to_customer_login_and_clears_the_real_cookie(http_client, db_path):
    token = _seed_partner_session(db_path, email="owner@example.test", role="customer_owner", customer_id="cust-1")
    response = http_client.post("/logout", cookies={partner_portal.SESSION_COOKIE: token})
    assert response.status_code == 303
    assert response.headers["location"] == "/customer-login.html"
    set_cookie = response.headers.get("set-cookie", "")
    assert partner_portal.SESSION_COOKIE in set_cookie  # the real cookie is actually being cleared
    revoked = sqlite3.connect(db_path).execute("SELECT revoked_at FROM user_sessions WHERE id='sess-1'").fetchone()[0]
    assert revoked is not None  # server-side session revoked, not just the cookie dropped client-side


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


def test_logout_then_get_customer_login_is_a_single_hop(http_client, db_path):
    token = _seed_partner_session(db_path, email="owner@example.test", role="customer_owner", customer_id="cust-1")
    logout_response = http_client.post("/logout", cookies={partner_portal.SESSION_COOKIE: token})
    destination = http_client.get(logout_response.headers["location"])
    assert destination.status_code == 200


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
