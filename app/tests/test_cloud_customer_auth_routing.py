"""Regression coverage for the EC2/cloud auth-routing bug: protected
customer pages (reported live: /dashboard and /playback, confirmed to
also affect /events, /alerts, /investigate, /subscription-portal, and
/mobile-app) redirected an unauthenticated visitor to the local-
emergency-recovery /login page instead of the real customer sign-in
page (/customer-login.html).

Root cause: authentication_middleware's unauthenticated fallback only
special-cased paths starting with "/customer" -- but the customer
portal's own nav (NAV_ITEMS, filtered for role in CUSTOMER_PORTAL_ROLES)
links to a MIX of "/customer"-prefixed paths and bare top-level paths
that were never covered by that check, and there was no RUNTIME_ROLE
awareness in this function at all: an edge appliance's identical
unauthenticated visit to these same shared dual-purpose routes (its
own local owner, not a cloud customer account) correctly wants /login,
but a cloud deployment's identical visit (always a real or prospective
customer, since a cloud server has no "local owner" concept) does not.

Fixed by CLOUD_CUSTOMER_NAV_PATH_PREFIXES + a RUNTIME_ROLE == "cloud"
gated branch in authentication_middleware. This file proves: the fix
applies on cloud, is a no-op on edge (existing /login behavior
unchanged there), the pre-existing "/customer"-prefixed branch and
admin/legacy fallback are both untouched either way, and the
previously-broken next= preservation through the actual customer
login POST (a separate, related bug in customer-login.html/partner_
portal.py's partner_login_submit -- next= was read into the URL
correctly but silently dropped by the client-side JS and never
consulted server-side) now genuinely returns the user to the page they
originally asked for.

Real HTTP through the real app (TestClient(main.app, follow_redirects=
False)), a throwaway sqlite DB via override_target() -- same style as
test_live_view_page_customer_auth.py / test_logout_redirects.py, which
already exercise this exact class of routing bug for other routes.
"""

import pytest
from fastapi.testclient import TestClient

import main
from database_backend import override_target
from partner_db import initialize_database, password_hash


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_cloud_customer_auth_routing.db"


@pytest.fixture()
def http_client(tmp_path, monkeypatch, db_path):
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    with override_target(sqlite_path=db_path):
        initialize_database()
        # base_url="https://app.anyaicam.com": the real production host,
        # not a stand-in -- three separate, pre-existing production-only
        # checks TestClient's own defaults would otherwise trip,
        # regardless of anything this file actually tests. Host:
        # TrustedHostMiddleware (main.py, gated by cloud_config.
        # effective_trusted_hosts) only accepts the configured
        # production hosts, not TestClient's own default Host of
        # "testserver". Scheme: cloud_security.py 308-redirects any
        # non-https request (this deployment sits behind a load
        # balancer that always terminates TLS upstream) -- TestClient's
        # own default scheme is http. Cookie domain: the CSRF cookie
        # (cloud_security.py) is set with Domain=app.anyaicam.com
        # (settings.cookie_domain) -- httpx's cookie jar (matching any
        # real browser) only stores/replays a cookie whose Domain
        # matches the request's own host, so "https://localhost" passed
        # the first two checks but silently never received the cookie a
        # real POST /api/partner-login needs, exactly the way an actual
        # customer-login.html page load and its own follow-up fetch()
        # do need to agree on host for the same reason.
        with TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client


# =============================================================== cloud vs edge: which login page

PREVIOUSLY_BROKEN_CUSTOMER_PATHS = [
    "/dashboard",
    "/playback",
    "/events",
    "/alerts",
    "/investigate",
    "/subscription-portal",
]


def test_mobile_app_is_already_public_so_this_fix_deliberately_excludes_it(monkeypatch):
    # NAV_ITEMS' one remaining bare-path customer link -- but it's
    # already in PUBLIC_PATH_PREFIXES (pre-existing, untouched by this
    # fix), so authentication_middleware never reaches CLOUD_CUSTOMER_
    # NAV_PATH_PREFIXES for it at all, on either role. Confirms that
    # intentional exclusion rather than assuming it.
    assert "/mobile-app" not in main.CLOUD_CUSTOMER_NAV_PATH_PREFIXES
    assert any(
        "/mobile-app" == prefix or "/mobile-app".startswith(prefix)
        for prefix in main.PUBLIC_PATH_PREFIXES
    )


@pytest.mark.parametrize("path", PREVIOUSLY_BROKEN_CUSTOMER_PATHS)
def test_cloud_role_sends_these_paths_to_customer_login(http_client, monkeypatch, path):
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    response = http_client.get(path)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/customer-login.html?next="), (path, location)


@pytest.mark.parametrize("path", PREVIOUSLY_BROKEN_CUSTOMER_PATHS)
def test_edge_role_still_sends_these_same_paths_to_local_recovery_login(http_client, monkeypatch, path):
    # The exact same shared dual-purpose routes an edge appliance's own
    # local owner legitimately reaches unauthenticated -- must be
    # completely unaffected by the cloud-only fix.
    monkeypatch.setattr(main, "RUNTIME_ROLE", "edge")
    response = http_client.get(path)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/login?next="), (path, location)
    assert "customer-login" not in location


@pytest.mark.parametrize("path", PREVIOUSLY_BROKEN_CUSTOMER_PATHS)
def test_cloud_next_is_preserved_exactly_as_the_originally_requested_path(http_client, monkeypatch, path):
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    response = http_client.get(path)
    location = response.headers["location"]
    next_value = location.split("next=", 1)[1]
    from urllib.parse import unquote
    assert unquote(next_value) == path


def test_customer_prefixed_paths_unaffected_by_this_fix_on_either_role(http_client, monkeypatch):
    # The pre-existing, already-correct branch -- must still work
    # exactly as before, on both roles, completely independent of
    # CLOUD_CUSTOMER_NAV_PATH_PREFIXES.
    for role in ("cloud", "edge"):
        monkeypatch.setattr(main, "RUNTIME_ROLE", role)
        response = http_client.get("/customer-account")
        assert response.status_code == 303
        assert response.headers["location"].startswith("/customer-login.html?next="), role


def test_admin_and_unrecognized_paths_still_fall_back_to_login_even_on_cloud(http_client, monkeypatch):
    # Deliberately NOT in CLOUD_CUSTOMER_NAV_PATH_PREFIXES -- an
    # unauthenticated visit to a genuinely admin/staff-only path must
    # keep going to /login regardless of role. Proves the fix is a
    # narrow widening, not a blanket default flip.
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    response = http_client.get("/admin-portal")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


def test_login_page_itself_still_renders_the_local_emergency_recovery_copy(http_client, monkeypatch):
    # This fix must never touch what /login itself renders -- only
    # which unauthenticated requests get redirected there.
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    response = http_client.get("/login")
    assert response.status_code == 200
    assert "Local emergency recovery sign-in" in response.text
    assert "Portal login" in response.text


def test_customer_login_html_still_reachable_unauthenticated_on_both_roles(http_client, monkeypatch):
    for role in ("cloud", "edge"):
        monkeypatch.setattr(main, "RUNTIME_ROLE", role)
        response = http_client.get("/customer-login.html")
        assert response.status_code == 200, role
        assert "Customer Login" in response.text


# =============================================================== next= actually round-trips through a real login

def _csrf_headers(http_client):
    """Primes the CSRF cookie (cloud_security.py sets anyaicam_csrf on
    any response if the client doesn't already have one) with a cheap
    GET, then returns the X-CSRF-Token header POST /api/partner-login
    actually requires -- matching customer-login.html's own real
    fetch() flow (which reads the same cookie the page's own initial
    GET already received) rather than skipping CSRF entirely."""
    http_client.get("/customer-login.html")
    token = http_client.cookies.get("anyaicam_csrf")
    return {"X-CSRF-Token": token} if token else {}


def _seed_customer(conn, *, email, password, customer_id="cust-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (customer_id, "partner-1", "Test Customer", "billing@example.test", "active", "2026-01-01"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,approved,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (f"user-{customer_id}", email, "customer_owner", customer_id, password_hash(password), "all", 1, "2026-01-01"),
    )
    conn.commit()


@pytest.mark.parametrize("path", ["/dashboard", "/playback", "/subscription-portal"])
def test_next_round_trips_through_a_real_customer_login_post(http_client, db_path, monkeypatch, path):
    """The full, previously-broken flow end to end: an unauthenticated
    cloud visit to a protected customer page redirects to /customer-
    login.html?next=<path> (this file's own fix above), and signing in
    for real from there now returns the browser to that exact path --
    not the previous behavior of silently dropping next= and always
    landing on the same fixed per-role destination regardless of what
    the customer actually clicked."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_customer(conn, email="owner@example.test", password="correct horse battery staple")
    conn.close()

    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    redirect = http_client.get(path)
    assert redirect.status_code == 303
    next_url = redirect.headers["location"].split("next=", 1)[1]
    from urllib.parse import unquote
    next_path = unquote(next_url)
    assert next_path == path

    login_response = http_client.post(
        "/api/partner-login",
        json={"email": "owner@example.test", "password": "correct horse battery staple", "customer_only": True, "next": next_path},
        headers=_csrf_headers(http_client),
    )
    assert login_response.status_code in (200, 303)
    # establish_partner_session() may respond 200 with a JSON body
    # carrying its own destination, or a redirect directly -- either
    # way the destination must be the originally-requested path, not
    # some fixed default.
    if login_response.status_code == 303:
        assert login_response.headers["location"] == next_path
    else:
        body = login_response.json()
        destination = body.get("destination") or body.get("redirect") or body.get("url")
        assert destination == next_path, body


def test_next_is_ignored_when_it_points_off_origin(http_client, db_path, monkeypatch):
    """Open-redirect guard: a next= that isn't a same-origin relative
    path must never be honored, even though the client only ever sends
    a same-origin value in practice -- defense in depth against a
    forged POST straight to /api/partner-login."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_customer(conn, email="owner2@example.test", password="another very good passphrase")
    conn.close()

    login_response = http_client.post(
        "/api/partner-login",
        json={
            "email": "owner2@example.test",
            "password": "another very good passphrase",
            "customer_only": True,
            "next": "https://evil.example/phish",
        },
        headers=_csrf_headers(http_client),
    )
    assert login_response.status_code in (200, 303)
    if login_response.status_code == 303:
        location = login_response.headers["location"]
        assert not location.startswith("https://evil.example")
    else:
        body = login_response.json()
        destination = body.get("destination") or body.get("redirect") or body.get("url") or ""
        assert not destination.startswith("https://evil.example")


def test_forced_password_change_still_wins_over_a_next_value(http_client, db_path, monkeypatch):
    """The pre-existing must_change_password precedence is never
    skipped past just because next= is also present."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) "
        "VALUES('cust-1','partner-1','Test Customer','billing@example.test','active','2026-01-01')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,approved,must_change_password,created_at) "
        "VALUES('user-mcp','mustchange@example.test','customer_owner','cust-1',?,'all',1,1,'2026-01-01')",
        (password_hash("temporary-password-123"),),
    )
    conn.commit()
    conn.close()

    login_response = http_client.post(
        "/api/partner-login",
        json={"email": "mustchange@example.test", "password": "temporary-password-123", "customer_only": True, "next": "/dashboard"},
        headers=_csrf_headers(http_client),
    )
    assert login_response.status_code in (200, 303)
    if login_response.status_code == 303:
        assert login_response.headers["location"] == "/change-password"
    else:
        body = login_response.json()
        destination = body.get("destination") or body.get("redirect") or body.get("url")
        assert destination == "/change-password", body


# =============================================================== cloud runtime-role startup/import validation

def test_runtime_role_reads_cloud_from_environment(monkeypatch):
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "cloud")
    import importlib
    importlib.reload(main)
    try:
        assert main.RUNTIME_ROLE == "cloud"
    finally:
        monkeypatch.delenv("ANYAICAM_RUNTIME_ROLE", raising=False)
        importlib.reload(main)


def test_cloud_customer_nav_path_prefixes_defined_and_matches_the_customer_nav():
    assert set(main.CLOUD_CUSTOMER_NAV_PATH_PREFIXES) == {
        "/dashboard", "/playback", "/events", "/alerts",
        "/investigate", "/subscription-portal",
    }
