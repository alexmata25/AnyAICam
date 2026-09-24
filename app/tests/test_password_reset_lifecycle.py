"""Password Recovery + Account Controls milestone (2026-09-17): closes
three real coverage gaps found while auditing the already-substantial
existing password-reset implementation (cloud_security.py's create_
password_reset()/consume_password_reset(), cloud_features.py's request/
complete routes) before building anything new:

1. Real token EXPIRY rejection was never directly tested -- the existing
   "invalid or expired" test (test_password_reset_link_host.py) only
   ever exercised a token that never existed at all, not a real row
   whose expires_at has genuinely passed. Same final code path today,
   but a regression in the date-comparison logic itself would not have
   been caught by "invalid random string" alone.
2. No-account-enumeration was implemented (password_reset_request()
   returns the identical generic message whether or not the account
   exists) but never asserted by a test -- nothing would have caught a
   future change that made the response distinguishable.
3. "Successful login with the new password" was verified only at the
   password_hash level (test_account_recovery.py), never through the
   real login route end to end -- this proves a customer can actually
   sign back in afterward, and that the OLD password stops working.

Same fixture shape as test_password_reset_link_host.py (its own sibling
file, per this suite's established per-file-fixture convention) --
deliberately duplicated, not imported.
"""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cloud_features
import cloud_security
import main
from cloud_config import Settings
from cloud_security import consume_password_reset
from database_backend import override_target
from partner_db import initialize_database, password_hash


STRONG_SECRET = "a" * 40


class _CapturingEmailService:
    def __init__(self):
        self.sent = []

    def send(self, message_type, to, subject, text, html=None, metadata=None):
        self.sent.append({"type": message_type, "to": to, "subject": subject, "text": text})
        return {"id": "test-message-id", "status": "preview"}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_password_reset_lifecycle.db"


@pytest.fixture()
def http_client(db_path):
    cloud_features._password_reset_email_limiter.events.clear()
    cloud_features._password_reset_ip_limiter.events.clear()
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app) as test_client:
            yield test_client


def _seed_customer(db_path, email="customer@example.test", password="temp-password-123"):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,created_at) VALUES(?,?,?,?)", ("partner-1", "Partner", "approved", "2026-01-01"))
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)", ("cust-1", "partner-1", "Customer", email, "active", "2026-01-01"))
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,must_change_password,account_status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("cust-user-1", "partner-1", email, "Customer", "customer_owner", password_hash(password), 1, "cust-1", "2026-01-01", 0, "active"),
    )
    conn.commit()
    return "cust-user-1"


def _cloud_production():
    return Settings(
        environment="production", runtime_role="cloud", app_secrets=[STRONG_SECRET],
        password_reset_url="https://portal.anyaicam.com/reset-password",
        allowed_origins=["https://portal.anyaicam.com"],
    )


def _request_and_extract_token(http_client, capturing, email):
    http_client.post("/api/password-reset/request", json={"email": email})
    text = capturing.sent[-1]["text"]
    return text.split("token=")[1].strip()


# --------------------------------------------------------------- real token expiry


def test_a_genuinely_expired_token_is_rejected_not_only_an_invalid_string(http_client, db_path, monkeypatch):
    """Regression guard distinct from the existing invalid-token test:
    this token is REAL (created through the real request flow, its hash
    genuinely matches), but its expires_at has genuinely passed --
    proving the date comparison itself enforces expiry, not merely that
    an unrecognized string is rejected."""
    _seed_customer(db_path, email="customer@example.test")
    capturing = _CapturingEmailService()
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)
    monkeypatch.setattr(cloud_features, "settings", _cloud_production())

    token = _request_and_extract_token(http_client, capturing, "customer@example.test")
    with override_target(sqlite_path=db_path):
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE password_reset_tokens SET expires_at=? WHERE user_id='cust-user-1'", ((datetime.now() - timedelta(minutes=1)).isoformat(),))
            conn.commit()

    response = http_client.post("/api/password-reset/complete", json={"token": token, "password": "brand-new-password-123"})

    assert response.status_code == 400
    with override_target(sqlite_path=db_path):
        account = sqlite3.connect(db_path).execute("SELECT password_hash FROM partner_users WHERE id='cust-user-1'").fetchone()[0]
    # The password must be genuinely unchanged -- not merely that the
    # HTTP response reported failure.
    from partner_db import verify_password
    assert verify_password("brand-new-password-123", account) is False


def test_consume_password_reset_itself_rejects_an_expired_row_directly(db_path):
    """Same property, exercised one layer lower (direct function call,
    no HTTP/email plumbing) -- proves the expiry check lives in
    consume_password_reset() itself, not merely somewhere in the route
    wrapping it."""
    with override_target(sqlite_path=db_path):
        initialize_database()
        _seed_customer(db_path, email="customer@example.test")
        now = datetime.now()
        raw = "a-real-looking-raw-token-value-1234567890"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO password_reset_tokens(id,user_id,email,token_hash,expires_at,used_at,created_at) VALUES(?,?,?,?,?,?,?)",
                ("tok-1", "cust-user-1", "customer@example.test", password_hash(raw), (now - timedelta(seconds=1)).isoformat(), None, now.isoformat()),
            )
            conn.commit()
        assert consume_password_reset(raw, "brand-new-password-123") is None


# --------------------------------------------------------------- no account enumeration


def test_forgot_password_response_is_identical_for_an_existing_and_a_nonexistent_email(http_client, db_path, monkeypatch):
    _seed_customer(db_path, email="real-customer@example.test")
    capturing = _CapturingEmailService()
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)
    monkeypatch.setattr(cloud_features, "settings", _cloud_production())

    existing_response = http_client.post("/api/password-reset/request", json={"email": "real-customer@example.test"})
    nonexistent_response = http_client.post("/api/password-reset/request", json={"email": "nobody-real@example.test"})

    assert existing_response.status_code == nonexistent_response.status_code == 200
    assert existing_response.json() == nonexistent_response.json()
    # The real account did actually get an email prepared -- proving the
    # identical response isn't because nothing happened for EITHER case,
    # only that the anonymous-facing response doesn't reveal which.
    assert len(capturing.sent) == 1
    assert capturing.sent[0]["to"] == "real-customer@example.test"


def test_forgot_password_response_shape_never_includes_a_boolean_existence_field(http_client, db_path, monkeypatch):
    """Guards against a future refactor accidentally adding an `exists`/
    `found`/`account_found`-shaped field to the response, which would
    reintroduce enumeration even while the message text stayed generic."""
    _seed_customer(db_path, email="real-customer@example.test")
    monkeypatch.setattr(cloud_features, "settings", _cloud_production())

    response = http_client.post("/api/password-reset/request", json={"email": "real-customer@example.test"})

    assert set(response.json().keys()) == {"message"}


# --------------------------------------------------------------- real end-to-end login with the new password


def test_customer_can_really_log_in_with_the_new_password_after_reset(http_client, db_path, monkeypatch):
    """The actual "successful login with the new password" requirement,
    proven through the real login route (/api/partner-login, which both
    the partner and customer sign-in pages actually submit to -- see
    customer-login.html), not merely a direct password_hash comparison."""
    _seed_customer(db_path, email="customer@example.test", password="old-temp-password-123")
    capturing = _CapturingEmailService()
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)
    monkeypatch.setattr(cloud_features, "settings", _cloud_production())

    token = _request_and_extract_token(http_client, capturing, "customer@example.test")
    complete = http_client.post("/api/password-reset/complete", json={"token": token, "password": "brand-new-password-456"})
    assert complete.status_code == 200

    login_response = http_client.post(
        "/api/partner-login",
        json={"email": "customer@example.test", "password": "brand-new-password-456", "customer_only": True},
        follow_redirects=False,
    )
    assert login_response.status_code in (200, 303)
    assert any(cookie for cookie in login_response.cookies)


def test_the_old_password_no_longer_works_after_reset(http_client, db_path, monkeypatch):
    _seed_customer(db_path, email="customer@example.test", password="old-temp-password-123")
    capturing = _CapturingEmailService()
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)
    monkeypatch.setattr(cloud_features, "settings", _cloud_production())

    token = _request_and_extract_token(http_client, capturing, "customer@example.test")
    http_client.post("/api/password-reset/complete", json={"token": token, "password": "brand-new-password-456"})

    old_password_login = http_client.post(
        "/api/partner-login",
        json={"email": "customer@example.test", "password": "old-temp-password-123", "customer_only": True},
        follow_redirects=False,
    )
    assert old_password_login.status_code == 403


# --------------------------------------------------------------- CSRF-enabled real flow (2026-09-20)
#
# Confirmed live on staging: every one of the four self-service password-
# reset pages (/forgot-password, /customer-forgot-password, /reset-
# password, /customer-reset-password) posts to /api/password-reset/
# request or /api/password-reset/complete WITHOUT an X-CSRF-Token header
# -- unlike every other authenticated form in this app (partner.html,
# customer-login.html), which read the anyaicam_csrf cookie via a csrf()
# helper and attach it. With ANYAICAM_CSRF_ENABLED=true (every staging/
# production deployment), every real submission to these four pages was
# unconditionally rejected 403 "CSRF validation failed" before the token
# was ever consumed -- confirmed by two consecutive real admin password-
# reset attempts on staging that both silently failed this way, with the
# token's used_at staying NULL and must_change_password staying set.
# Every existing test above passes with CSRF genuinely disabled (no test
# in this file ever monkeypatches cloud_security.settings, only cloud_
# features.settings -- a separate name binding to the same module-level
# Settings instance, so patching one never changes what the real
# ProductionSecurityMiddleware.dispatch() actually enforces), which is
# exactly how this shipped unnoticed. The tests below monkeypatch cloud_
# security.settings itself so the real middleware genuinely enforces
# CSRF, then drive the exact same GET-page/read-cookie/POST-with-header
# sequence a real browser performs.


def _csrf_enabled_cloud_production(**overrides):
    kwargs = dict(
        environment="production", runtime_role="cloud", app_secrets=[STRONG_SECRET],
        csrf_enabled=True,
        password_reset_url="https://portal.anyaicam.com/reset-password",
        allowed_origins=["https://portal.anyaicam.com"],
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def test_every_reset_page_source_includes_the_csrf_header():
    """Fast, deterministic guard directly on the served markup -- proves
    the fix (and any future regression) without needing the real
    middleware engaged at all. Mirrors test_website_partner_session_nav_
    links.py's own established pattern for this exact class of bug."""
    with TestClient(main.app) as client:
        for path in ("/forgot-password", "/customer-forgot-password", "/reset-password", "/customer-reset-password"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert "'X-CSRF-Token':csrf()" in response.text, path


# --------------------------------------------------------------- legacy quoted/padded CSRF cookie (2026-09-20)
#
# Independently confirmed (Codex review of commit 1b9e5f5): token_
# security.py's sign() strips base64 '=' padding specifically so http.
# cookies never wraps the Set-Cookie value in double quotes for a
# FRESHLY issued cookie -- but every hand-written client-side csrf()
# helper across this app (partner.html, customer-login.html, and all
# four cloud_features.py password-reset pages) read that cookie back
# with `.split('=')[1]`, which only keeps the text up to the FIRST '='
# in the whole "anyaicam_csrf=<value>" substring. A LEGACY cookie
# issued before sign()'s own padding-stripping fix (still sitting in a
# real browser that visited this deployment before that fix shipped --
# unsign() itself already re-pads specifically to accept such a token)
# is quoted AND contains internal '=' padding, so naive [1] truncates
# it, and the truncated value is sent as X-CSRF-Token -- silently
# mismatching the real (already-unquoted-by-Starlette) cookie value
# server-side, 403ing every submission. The main.py shell() wrapper's
# own auto-injected fetch patch already used the correct `.split('=').
# slice(1).join('=')` extraction (which is exactly why /reset-password
# and /forgot-password, both shell()-wrapped, were never provably
# broken by this -- see this file's own note on that dead end); the
# standalone pages had no such protection.
_LEGACY_COOKIE_CSRF_SOURCES = (
    "/forgot-password", "/customer-forgot-password", "/reset-password", "/customer-reset-password",
)
_NAIVE_BROKEN_CSRF_PATTERN = "?.split('=')[1]||''"
_FIXED_CSRF_PATTERN = "m.split('=').slice(1).join('=')"


def test_no_reset_page_uses_the_truncating_csrf_cookie_read():
    with TestClient(main.app) as client:
        for path in _LEGACY_COOKIE_CSRF_SOURCES:
            response = client.get(path)
            assert _NAIVE_BROKEN_CSRF_PATTERN not in response.text, path
            assert _FIXED_CSRF_PATTERN in response.text, path


def test_partner_html_and_customer_login_html_use_the_fixed_csrf_cookie_read():
    """partner.html/customer-login.html are standalone pages (not shell()-
    wrapped), so admin/partner login itself carried the exact same
    truncation bug -- a real login attempt with a legacy quoted cookie
    still present would 403 on CSRF before credentials are even checked."""
    app_dir = Path(__file__).resolve().parents[1]
    for name in ("partner.html", "customer-login.html"):
        source = (app_dir / name).read_text(encoding="utf-8")
        assert _NAIVE_BROKEN_CSRF_PATTERN not in source, name
        assert _FIXED_CSRF_PATTERN in source, name


def test_csrf_helper_correctly_extracts_a_legacy_quoted_padded_cookie_value():
    """Proves the fixed extraction logic itself (not just its absence/
    presence as a string) against the exact byte shape a legacy cookie
    actually has: base64 padding ('=') plus the double-quote wrapping
    http.cookies applies around any value containing it. Runs the real
    extracted JS snippet under Node (already a project dependency via
    the repo's own JS assets) against a mocked document.cookie -- more
    rigorous than a source-text assertion alone."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available in this environment")

    app_dir = Path(__file__).resolve().parents[1]
    source = (app_dir / "partner.html").read_text(encoding="utf-8")
    start = source.index("const csrf=()=>{")
    end = source.index("};", start) + 2
    csrf_fn_source = source[start:end]

    # The exact shape a legacy pre-fix cookie has: quoted, with internal
    # '=' padding -- e.g. Set-Cookie: anyaicam_csrf="MTIz:csrf:abc="
    legacy_value = "MTIz:csrf:abcdef=="
    mocked_document_cookie = f'other=1; anyaicam_csrf="{legacy_value}"; more=2'

    script = f"""
    const document = {{cookie: {json.dumps(mocked_document_cookie)}}};
    {csrf_fn_source}
    console.log(JSON.stringify(csrf()));
    """
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    extracted = json.loads(result.stdout.strip())
    assert extracted == legacy_value


# --------------------------------------------------------------- reset-request pages must not fake success on error (2026-09-20)
#
# Independently confirmed (Codex review): /forgot-password and /customer-
# forgot-password both unconditionally treated the response body as the
# success shape (`r.message` / `r.message||'...prepared.'`) without ever
# checking response.ok first. A CSRF failure (or any other error) returns
# {"detail": "..."} with no `message` key -- so `r.message` is undefined,
# and customer-forgot-password's own `||` fallback then displayed the
# exact same reassuring "If the account exists..." text a real success
# shows, silently hiding the failure from the user. /reset-password and
# /customer-reset-password's own COMPLETE-step handlers were already
# correct (`r.message||r.detail`, unconditional either way is fine there
# since both fields never collide) -- this defect was specific to the
# two REQUEST-step pages.


def test_forgot_password_pages_surface_the_real_error_instead_of_fake_success():
    app_dir = Path(__file__).resolve().parents[1]
    source = (app_dir / "cloud_features.py").read_text(encoding="utf-8")
    # 2026-09-24: a non-JSON error body no longer throws (r falls back to
    # {}), and the fallbacks read clearly -- still never a fake success.
    assert "r=await response.json().catch(()=>({}));showToast(response.ok?(r.message||'If the account exists, a password-reset message has been prepared.'):(r.detail||'Request failed. Please try again.'))" in source
    assert "box.textContent=response.ok?(r.message||'If the account exists, a reset message has been prepared.'):(r.detail||'Request failed. Please try again.')" in source


def test_forgot_password_request_shows_the_real_csrf_failure_not_generic_success(db_path, monkeypatch):
    """Real HTTP proof: submitting /api/password-reset/request WITHOUT a
    CSRF header (the exact failure a stale/legacy cookie situation would
    still produce for any request that somehow reaches the server without
    a valid token) must be distinguishable from success at the response
    level that the page's own JS branches on."""
    csrf_settings = _csrf_enabled_cloud_production()
    monkeypatch.setattr(cloud_security, "settings", csrf_settings)
    monkeypatch.setattr(cloud_features, "settings", csrf_settings)

    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app) as client:
            client.get("/forgot-password")
            response = client.post("/api/password-reset/request", json={"email": "nobody@example.test"})
            assert response.status_code == 403
            body = response.json()
            assert "message" not in body
            assert body["detail"] == "CSRF validation failed."


def test_reset_password_completes_with_real_csrf_middleware_enforced(db_path, monkeypatch):
    """The actual bug, proven end to end: with CSRF genuinely enforced by
    the real middleware (not just disabled-by-default in every other test
    in this file), the real browser sequence -- GET the page, read the
    anyaicam_csrf cookie it sets, POST with that value as X-CSRF-Token --
    must actually succeed and consume the token. Before this fix, this
    exact sequence 403'd with "CSRF validation failed" and left used_at
    NULL, which is precisely what happened live on staging."""
    csrf_settings = _csrf_enabled_cloud_production()
    monkeypatch.setattr(cloud_security, "settings", csrf_settings)
    monkeypatch.setattr(cloud_features, "settings", csrf_settings)
    capturing = _CapturingEmailService()
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)

    with override_target(sqlite_path=db_path):
        initialize_database()
        _seed_customer(db_path, email="admin-like@example.test", password="old-temp-password-123")
        with TestClient(main.app) as client:
            request_page = client.get("/forgot-password")
            request_csrf = request_page.cookies.get("anyaicam_csrf")
            client.post(
                "/api/password-reset/request",
                json={"email": "admin-like@example.test"},
                headers={"X-CSRF-Token": request_csrf},
            )
            token = capturing.sent[-1]["text"].split("token=")[1].strip()

            client.get(f"/reset-password?token={token}")
            csrf_cookie = client.cookies.get("anyaicam_csrf")
            assert csrf_cookie, "the CSRF cookie must be available to the browser by the time the page loads"

            complete = client.post(
                "/api/password-reset/complete",
                json={"token": token, "password": "brand-new-password-789"},
                headers={"X-CSRF-Token": csrf_cookie},
            )
            assert complete.status_code == 200, complete.text

        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT used_at FROM password_reset_tokens WHERE user_id='cust-user-1'").fetchone()
            assert row[0] is not None, "used_at must be populated once the real flow completes"
            must_change = conn.execute("SELECT must_change_password FROM partner_users WHERE id='cust-user-1'").fetchone()[0]
            assert must_change == 0


def test_reset_password_without_the_csrf_header_is_the_confirmed_live_failure(db_path, monkeypatch):
    """Negative control: proves the test above is actually exercising the
    real bug, not a fixture artifact -- the exact same sequence, minus
    the X-CSRF-Token header, must still fail exactly as it did live."""
    csrf_settings = _csrf_enabled_cloud_production()
    monkeypatch.setattr(cloud_security, "settings", csrf_settings)
    monkeypatch.setattr(cloud_features, "settings", csrf_settings)
    capturing = _CapturingEmailService()
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)

    with override_target(sqlite_path=db_path):
        initialize_database()
        _seed_customer(db_path, email="admin-like@example.test", password="old-temp-password-123")
        with TestClient(main.app) as client:
            request_page = client.get("/forgot-password")
            request_csrf = request_page.cookies.get("anyaicam_csrf")
            client.post(
                "/api/password-reset/request",
                json={"email": "admin-like@example.test"},
                headers={"X-CSRF-Token": request_csrf},
            )
            token = capturing.sent[-1]["text"].split("token=")[1].strip()
            client.get(f"/reset-password?token={token}")

            complete = client.post(
                "/api/password-reset/complete",
                json={"token": token, "password": "brand-new-password-789"},
            )
            assert complete.status_code == 403
            assert complete.json()["detail"] == "CSRF validation failed."

        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT used_at FROM password_reset_tokens WHERE user_id='cust-user-1'").fetchone()
            assert row[0] is None
