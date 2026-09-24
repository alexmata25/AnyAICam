"""Customer password-reset hardening (2026-09-24).

1. Reset tokens are "<row id>.<secret>": completing a reset verifies ONE
   stored PBKDF2 hash instead of every outstanding token in the system
   (an unauthenticated CPU amplification), with a bounded fallback for
   bare-secret links issued before this change.
2. /api/password-reset/complete is rate limited per client IP.
3. On a cloud deployment, customer reset links go to the customer-branded
   /customer-reset-password page (previously only edge appliances did);
   partner/admin links are unchanged.
4. ANYAICAM_EMAIL_FROM falls back to ANYAICAM_SMTP_FROM, and readiness
   reports whether password-reset email can actually be delivered.

No real email is sent: a capturing email service stands in for SMTP.
"""
import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import cloud_features
import cloud_security
import main
from cloud_config import Settings
from database_backend import override_target
from partner_db import initialize_database, password_hash

STRONG_SECRET = "a" * 40


class _CapturingEmailService:
    def __init__(self):
        self.sent = []

    def send(self, message_type, to, subject, text, html=None, metadata=None):
        self.sent.append({"to": to, "text": text})
        return {"id": f"msg-{len(self.sent)}", "status": "preview"}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "reset_hardening.db"


@pytest.fixture()
def client(db_path):
    for limiter in (cloud_features._password_reset_email_limiter, cloud_features._password_reset_ip_limiter, cloud_features._password_reset_complete_ip_limiter):
        limiter.events.clear()
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app) as test_client:
            yield test_client


def _seed_user(db_path, *, user_id, email, role, customer_id=None, must_change_password=0):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,created_at) VALUES('partner-1','Partner','approved','2026-01-01')")
    if customer_id:
        conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)", (customer_id, "partner-1", "Customer", f"{customer_id}@example.test", "active", "2026-01-01"))
    conn.execute(
        "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,must_change_password) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (user_id, "partner-1", email, "User", role, password_hash("old-password-123"), 1, customer_id, "2026-01-01", must_change_password),
    )
    conn.commit()
    conn.close()


def _rows(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _cloud_settings(**overrides):
    values = dict(
        environment="production", runtime_role="cloud", app_secrets=[STRONG_SECRET],
        password_reset_url="https://portal.anyaicam.com/reset-password",
        allowed_origins=["https://portal.anyaicam.com"],
    )
    values.update(overrides)
    return Settings(**values)


# ------------------------------------------------------------- token format / lookup


def test_a_reset_token_carries_its_row_id_and_only_the_secret_is_stored_hashed(db_path, client):
    _seed_user(db_path, user_id="u1", email="owner@example.test", role="customer_owner", customer_id="cust-1")
    with override_target(sqlite_path=db_path):
        raw = cloud_security.create_password_reset("u1", "owner@example.test")
    token_id, _, secret = raw.partition(".")
    stored = _rows(db_path, "SELECT * FROM password_reset_tokens WHERE id=?", (token_id,))
    assert len(stored) == 1
    assert secret and secret not in stored[0]["token_hash"] and raw not in stored[0]["token_hash"]


def test_completing_a_reset_verifies_exactly_one_hash_however_many_tokens_are_outstanding(db_path, client, monkeypatch):
    _seed_user(db_path, user_id="u1", email="owner@example.test", role="customer_owner", customer_id="cust-1")
    for index in range(30):
        _seed_user(db_path, user_id=f"other-{index}", email=f"other{index}@example.test", role="customer_owner", customer_id=f"cust-o{index}")
    with override_target(sqlite_path=db_path):
        for index in range(30):
            cloud_security.create_password_reset(f"other-{index}", f"other{index}@example.test")
        raw = cloud_security.create_password_reset("u1", "owner@example.test")
        calls = []
        real_verify = cloud_security.verify_password
        monkeypatch.setattr(cloud_security, "verify_password", lambda value, encoded: calls.append(1) or real_verify(value, encoded))
        assert cloud_security.consume_password_reset(raw, "new-password-4567") == "customer_owner"
    assert len(calls) == 1


def test_a_wrong_secret_fails_and_leaves_the_real_token_usable(db_path, client):
    _seed_user(db_path, user_id="u1", email="owner@example.test", role="customer_owner", customer_id="cust-1")
    with override_target(sqlite_path=db_path):
        raw = cloud_security.create_password_reset("u1", "owner@example.test")
        token_id = raw.split(".")[0]
        assert cloud_security.consume_password_reset(f"{token_id}.not-the-secret", "new-password-4567") is None
        assert cloud_security.consume_password_reset(raw, "new-password-4567") == "customer_owner"


def test_a_token_is_single_use(db_path, client):
    _seed_user(db_path, user_id="u1", email="owner@example.test", role="customer_owner", customer_id="cust-1")
    with override_target(sqlite_path=db_path):
        raw = cloud_security.create_password_reset("u1", "owner@example.test")
        assert cloud_security.consume_password_reset(raw, "new-password-4567")
        assert cloud_security.consume_password_reset(raw, "another-password-890") is None


def test_an_expired_token_is_rejected(db_path, client):
    _seed_user(db_path, user_id="u1", email="owner@example.test", role="customer_owner", customer_id="cust-1")
    with override_target(sqlite_path=db_path):
        raw = cloud_security.create_password_reset("u1", "owner@example.test")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE password_reset_tokens SET expires_at=?", ((datetime.now() - timedelta(minutes=1)).isoformat(),))
    conn.commit(); conn.close()
    with override_target(sqlite_path=db_path):
        assert cloud_security.consume_password_reset(raw, "new-password-4567") is None


def test_a_link_issued_before_this_change_still_works_for_its_remaining_life(db_path, client):
    _seed_user(db_path, user_id="u1", email="owner@example.test", role="customer_owner", customer_id="cust-1")
    legacy_secret = "legacy-bare-secret-without-an-id-prefix"
    conn = sqlite3.connect(db_path)
    now = datetime.now()
    conn.execute(
        "INSERT INTO password_reset_tokens(id,user_id,email,token_hash,expires_at,used_at,created_at) VALUES(?,?,?,?,?,NULL,?)",
        ("legacy-1", "u1", "owner@example.test", password_hash(legacy_secret), (now + timedelta(minutes=30)).isoformat(), now.isoformat()),
    )
    conn.commit(); conn.close()
    with override_target(sqlite_path=db_path):
        assert cloud_security.consume_password_reset(legacy_secret, "new-password-4567") == "customer_owner"


def test_the_legacy_fallback_scan_is_bounded(db_path, client, monkeypatch):
    _seed_user(db_path, user_id="u1", email="owner@example.test", role="customer_owner", customer_id="cust-1")
    with override_target(sqlite_path=db_path):
        for _ in range(cloud_security.LEGACY_RESET_TOKEN_SCAN_LIMIT + 15):
            cloud_security.create_password_reset("u1", "owner@example.test")
        calls = []
        monkeypatch.setattr(cloud_security, "verify_password", lambda value, encoded: calls.append(1) or False)
        assert cloud_security.consume_password_reset("no-dot-random-guess", "new-password-4567") is None
    assert len(calls) <= cloud_security.LEGACY_RESET_TOKEN_SCAN_LIMIT


def test_a_completed_reset_revokes_sessions_clears_lockout_and_other_links_and_stores_no_plaintext(db_path, client):
    _seed_user(db_path, user_id="u1", email="owner@example.test", role="customer_owner", customer_id="cust-1", must_change_password=1)
    conn = sqlite3.connect(db_path)
    now = datetime.now().isoformat()
    conn.execute("INSERT INTO user_sessions(id,user_id,email,role,session_type,created_at,expires_at) VALUES('s1','u1','owner@example.test','customer_owner','cookie',?,?)", (now, (datetime.now() + timedelta(hours=8)).isoformat()))
    conn.execute("INSERT INTO account_lockouts(email,attempts,locked_until) VALUES('owner@example.test',5,?)", ((datetime.now() + timedelta(minutes=10)).isoformat(),))
    conn.commit(); conn.close()
    with override_target(sqlite_path=db_path):
        first = cloud_security.create_password_reset("u1", "owner@example.test")
        second = cloud_security.create_password_reset("u1", "owner@example.test")
        assert cloud_security.consume_password_reset(second, "brand-new-password-1") == "customer_owner"
        assert cloud_security.consume_password_reset(first, "yet-another-password") is None  # sibling link invalidated
    user = _rows(db_path, "SELECT password_hash,must_change_password FROM partner_users WHERE id='u1'")[0]
    assert "brand-new-password-1" not in user["password_hash"] and user["password_hash"].startswith("pbkdf2_sha256$")
    assert user["must_change_password"] == 0
    assert _rows(db_path, "SELECT revoked_at FROM user_sessions WHERE id='s1'")[0]["revoked_at"] is not None
    assert _rows(db_path, "SELECT * FROM account_lockouts") == []


# ------------------------------------------------------------- reset links


def test_a_customer_gets_the_customer_reset_page_on_a_cloud_deployment(db_path, client, monkeypatch):
    _seed_user(db_path, user_id="u1", email="owner@example.test", role="customer_owner", customer_id="cust-1")
    capturing = _CapturingEmailService()
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)
    monkeypatch.setattr(cloud_features, "settings", _cloud_settings())
    response = client.post("/api/password-reset/request", json={"email": "owner@example.test"}, headers={"host": "attacker.example"})
    assert response.status_code == 200
    assert "https://portal.anyaicam.com/customer-reset-password?token=" in capturing.sent[0]["text"]
    assert "attacker.example" not in capturing.sent[0]["text"]


def test_a_partner_user_still_gets_the_partner_reset_page(db_path, client, monkeypatch):
    _seed_user(db_path, user_id="p1", email="partner@example.test", role="partner_owner")
    capturing = _CapturingEmailService()
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)
    monkeypatch.setattr(cloud_features, "settings", _cloud_settings())
    client.post("/api/password-reset/request", json={"email": "partner@example.test"})
    assert "https://portal.anyaicam.com/reset-password?token=" in capturing.sent[0]["text"]


def test_an_unknown_email_gets_the_same_generic_response(db_path, client, monkeypatch):
    capturing = _CapturingEmailService()
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)
    response = client.post("/api/password-reset/request", json={"email": "nobody@example.test"})
    assert response.status_code == 200
    assert response.json()["message"].startswith("If the account exists")
    assert capturing.sent == []


def test_the_emailed_customer_link_completes_end_to_end(db_path, client, monkeypatch):
    _seed_user(db_path, user_id="u1", email="owner@example.test", role="customer_owner", customer_id="cust-1")
    capturing = _CapturingEmailService()
    monkeypatch.setattr(cloud_features, "get_email_service", lambda: capturing)
    monkeypatch.setattr(cloud_features, "settings", _cloud_settings())
    client.post("/api/password-reset/request", json={"email": "owner@example.test"})
    token = capturing.sent[0]["text"].split("token=")[1].strip()
    response = client.post("/api/password-reset/complete", json={"token": token, "password": "brand-new-password-1"})
    assert response.status_code == 200
    assert response.json()["destination"] == "/customer-login.html"


@pytest.mark.parametrize(("configured", "expected"), [
    ("https://portal.anyaicam.com/reset-password", "https://portal.anyaicam.com/customer-reset-password"),
    ("https://portal.anyaicam.com/app/reset-password", "https://portal.anyaicam.com/app/customer-reset-password"),
    ("https://portal.anyaicam.com/password", "https://portal.anyaicam.com/customer-reset-password"),
])
def test_the_customer_reset_url_is_derived_from_the_configured_origin(monkeypatch, configured, expected):
    monkeypatch.setattr(cloud_features, "settings", _cloud_settings(password_reset_url=configured))
    assert cloud_features.customer_reset_url() == expected


# ------------------------------------------------------------- rate limit


def test_completing_resets_is_rate_limited_per_client(db_path, client):
    statuses = [client.post("/api/password-reset/complete", json={"token": "x.y", "password": "long-enough-password"}).status_code for _ in range(25)]
    assert statuses[:20] == [400] * 20
    assert statuses[20:] == [429] * 5


# ------------------------------------------------------------- email configuration


def test_the_sender_falls_back_to_the_legacy_smtp_from_variable(monkeypatch):
    monkeypatch.delenv("ANYAICAM_EMAIL_FROM", raising=False)
    monkeypatch.setenv("ANYAICAM_SMTP_FROM", "alerts@anyaicam.example")
    assert Settings().email_from == "alerts@anyaicam.example"
    monkeypatch.setenv("ANYAICAM_EMAIL_FROM", "reset@anyaicam.example")
    assert Settings().email_from == "reset@anyaicam.example"  # the dedicated variable still wins


def test_readiness_reports_whether_reset_email_can_be_delivered(monkeypatch):
    import cloud_config

    monkeypatch.setattr(cloud_config, "settings", Settings(email_backend="preview"))
    assert main._password_reset_email_ready() is False
    monkeypatch.setattr(cloud_config, "settings", Settings(email_backend="smtp", smtp_host="email-smtp.us-east-1.amazonaws.com", email_from="no-reply@anyaicam.example"))
    assert main._password_reset_email_ready() is True
    monkeypatch.setattr(cloud_config, "settings", Settings(email_backend="smtp", smtp_host="smtp.example", email_from="no-reply@localhost"))
    assert main._password_reset_email_ready() is False
