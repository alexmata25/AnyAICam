"""One sign-out for every portal, signed-in pages never cached, and the legacy
/alerts page (2026-10-01).

- /logout and /partner-logout (customer VMS + partner workspace Sign out,
  and the Friends & Family account switch) share main.perform_logout():
  revoke the Partner Portal session, destroy the legacy session, clear both
  cookies, land by role.
- page_shell() shows who is signed in and gives the phone layout its own
  Log out.
- Signed-in HTML is Cache-Control: private, no-store, so Back/Forward after
  Log out cannot redisplay it from history.
- The legacy /alerts page called in_app_alerts(30) -- 30 as the request --
  and failed for every admin."""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

import main
import partner_portal
from test_logout_redirects import (  # noqa: F401 -- fixtures and helpers
    _admin_session_cookie,
    _seed_partner_session,
    db_path,
    http_client,
)

PARTNER = partner_portal.SESSION_COOKIE
LEGACY = main.SESSION_COOKIE_NAME


def _customer_session(email, role, customer_id="cust-1"):
    """Linux 1.2.7.1 (D-AUTHZ-1 residual, spec §5 S-2): a session authorizes only
    while its user_sessions row names a live partner_users account and that
    account's current authorization_version. _seed_partner_session() writes a
    row with no account and no version (still fine for sign-out, which reads the
    cookie's own claims), so the tests that need a signed-in customer PAGE use
    a real account + bound session, as a customer sign-in creates it."""
    from authz_helpers import make_partner_user, partner_session_cookie
    from partner_db import connection
    with connection() as db:
        exists = db.execute("SELECT 1 FROM partner_users WHERE lower(email)=?", (email.lower(),)).fetchone()
    if not exists:
        make_partner_user(email, role, customer_id=customer_id)
    return partner_session_cookie(email, role)


def _cleared(response, name):
    return any(header.startswith(f"{name}=") and ("Max-Age=0" in header or "expires=" in header.lower())
               for header in response.headers.get_list("set-cookie"))


# ------------------------------------------------------------------ one sign-out

@pytest.mark.parametrize("path", ["/logout", "/partner-logout"])
@pytest.mark.parametrize("role,landing", [
    ("customer_owner", main.CUSTOMER_LOGOUT_DESTINATION),
    ("customer_viewer", main.CUSTOMER_LOGOUT_DESTINATION),  # household member
    ("administrator", "/partner.html"),
    ("partner_owner", "/partner.html"),
    ("technician", "/partner.html"),
])
def test_both_sign_out_routes_behave_identically(http_client, db_path, path, role, landing):
    token = _seed_partner_session(db_path, email=f"{role}@example.test", role=role,
                                  customer_id="cust-1" if role.startswith("customer") else None)
    response = http_client.post(path, cookies={PARTNER: token})
    assert response.status_code == 303 and response.headers["location"] == landing
    assert _cleared(response, PARTNER) and _cleared(response, LEGACY)
    assert sqlite3.connect(db_path).execute("SELECT revoked_at FROM user_sessions WHERE id='sess-1'").fetchone()[0]


@pytest.mark.parametrize("path", ["/logout", "/partner-logout"])
def test_a_browser_holding_both_sessions_loses_both(http_client, db_path, path):
    partner_token = _seed_partner_session(db_path, email="administrator@example.test", role="administrator")
    legacy_token = _admin_session_cookie()
    response = http_client.post(path, cookies={PARTNER: partner_token, LEGACY: legacy_token})
    assert response.status_code == 303 and response.headers["location"] == "/partner.html"
    assert _cleared(response, PARTNER) and _cleared(response, LEGACY)
    assert legacy_token not in main.load_sessions()
    assert sqlite3.connect(db_path).execute("SELECT revoked_at FROM user_sessions WHERE id='sess-1'").fetchone()[0]
    # The old cookies are dead on the very next request.
    assert http_client.get("/customer-account", cookies={PARTNER: partner_token}).status_code in (303, 401, 403)
    assert http_client.get("/alerts", cookies={LEGACY: legacy_token}).status_code in (303, 401, 403)


def test_switching_from_a_household_member_to_the_owner(http_client, db_path):
    """Friends & Family's 'switch account' and the customer Sign out both go
    through the shared sign-out; the next account starts clean."""
    # 1.2.7.1: real accounts with bound sessions (see _customer_session).
    viewer = _customer_session("maria@example.test", "customer_viewer")
    assert partner_portal._identity(type("R", (), {"cookies": {PARTNER: viewer}})())["email"] == "maria@example.test"
    out = http_client.post("/partner-logout", cookies={PARTNER: viewer})
    assert out.headers["location"] == main.CUSTOMER_LOGOUT_DESTINATION
    assert http_client.get("/customer-account", cookies={PARTNER: viewer}).status_code in (303, 401, 403)
    owner = _customer_session("owner@example.test", "customer_owner")
    assert partner_portal._identity(type("R", (), {"cookies": {PARTNER: owner}})())["email"] == "owner@example.test"


# ------------------------------------------------------------------ page shell

@pytest.mark.parametrize("role,label", [("customer_owner", "Account owner"), ("customer_viewer", "Household member")])
def test_the_phone_layout_has_log_out_and_shows_who_is_signed_in(http_client, db_path, role, label):
    token = _customer_session(f"{role}@example.test", role)  # 1.2.7.1: a real bound session
    page = http_client.get("/dashboard", cookies={PARTNER: token})
    assert page.status_code == 200
    html = page.text
    nav = html[html.index('<nav class="mobile-nav"'):html.index("</nav>", html.index('<nav class="mobile-nav"'))]
    assert 'class="mobile-logout logout-form" method="post" action="/logout"' in nav
    assert f"{role}@example.test" in nav and label in nav
    assert 'class="sidebar-identity"' in html and label in html
    # Every logout form -- including this mobile one, rendered after the
    # script -- gets the CSRF token filled in on submit: one document-level
    # listener, never forms looked up when the script runs (that missed the
    # mobile form; see test_household_logout.py).
    assert "document.addEventListener('submit'" in html and "contains('logout-form')" in html
    assert "document.querySelectorAll('.logout-form')" not in html


def test_signed_in_pages_are_never_cached_and_public_pages_are_unaffected(http_client, db_path):
    token = _customer_session("owner@example.test", "customer_owner")  # 1.2.7.1: a real bound session
    page = http_client.get("/dashboard", cookies={PARTNER: token})
    assert page.headers["cache-control"] == "private, no-store" and page.headers["cdn-cache-control"] == "no-store"
    public = http_client.get("/customer-login.html")
    assert "private, no-store" not in public.headers.get("cache-control", "")


def test_after_log_out_back_cannot_reload_the_signed_in_page(http_client, db_path):
    """Back/Forward: the page was served no-store (never stored in history),
    the sign-out tells the browser to drop its cache, and re-requesting it
    with the old cookie is refused."""
    token = _customer_session("owner@example.test", "customer_owner")  # 1.2.7.1: a real bound session
    before = http_client.get("/dashboard", cookies={PARTNER: token})
    assert before.status_code == 200
    assert "no-store" in before.headers["cache-control"]
    out = http_client.post("/logout", cookies={PARTNER: token})
    assert out.headers.get("clear-site-data") == '"cache"' and out.headers.get("cache-control") == "no-store"
    again = http_client.get("/dashboard", cookies={PARTNER: token})
    assert again.status_code in (303, 401, 403)


# ------------------------------------------------------------------ legacy /alerts

@pytest.fixture()
def alerts_file(tmp_path, monkeypatch):
    path = tmp_path / "in_app_alerts.jsonl"
    monkeypatch.setattr(main, "IN_APP_ALERTS_FILE", path)
    return path


def _populate(path):
    # 2026-10-08: dated relative to now. The fixed 2026-10-01 timestamps
    # passed RETENTION_DAYS (7) on 2026-10-08, after which the app's own
    # retention pass (prune_expired_in_app_alerts, started with the app)
    # could delete them before the page read the file -- an intermittent
    # failure that only looked like a regression.
    now = datetime.now()
    path.write_text("\n".join(json.dumps(item) for item in [
        {"id": "a1", "timestamp": (now - timedelta(hours=2)).isoformat(timespec="seconds"), "title": "Camera offline",
         "message": "Bedroom stopped responding.", "severity": "warning"},
        {"id": "a2", "timestamp": (now - timedelta(hours=1)).isoformat(timespec="seconds"), "event_type": "system_health",
         "message": "Storage below 10%.", "severity": "critical"},
    ]) + "\n", encoding="utf-8")


def test_the_alert_fixtures_are_within_retention():
    # Guards the fixture above against going stale again.
    path_items = []

    class Capture:
        def write_text(self, text, encoding=None):
            path_items.extend(json.loads(line) for line in text.splitlines())
    _populate(Capture())
    cutoff = datetime.now() - timedelta(days=main.RETENTION_DAYS)
    assert path_items and all(datetime.fromisoformat(item["timestamp"]) > cutoff for item in path_items)


@pytest.mark.parametrize("populated", [False, True])
def test_legacy_admin_alerts_page_renders(http_client, alerts_file, populated):
    if populated:
        _populate(alerts_file)
    response = http_client.get("/alerts", cookies={LEGACY: _admin_session_cookie()})
    assert response.status_code == 200, response.text[:300]
    if populated:
        assert "Storage below 10%." in response.text
    assert main.in_app_alerts.__name__ == "in_app_alerts"


@pytest.mark.parametrize("populated", [False, True])
def test_portal_administrator_alerts_page_renders(http_client, db_path, alerts_file, populated):
    if populated:
        _populate(alerts_file)
    token = _seed_partner_session(db_path, email="administrator@example.test", role="administrator")
    response = http_client.get("/alerts", cookies={PARTNER: token})
    assert response.status_code in (200, 303), response.text[:300]
    if response.status_code == 200 and populated:
        assert "Storage below 10%." in response.text
