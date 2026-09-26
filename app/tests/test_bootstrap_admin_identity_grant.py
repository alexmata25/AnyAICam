"""Regression coverage for two confirmed-live release blockers:

1. bootstrap_admin() (partner_db.py, TEMPORARY LOCAL BOOTSTRAP PATH --
   ANYAICAM_ADMIN_EMAIL/ANYAICAM_ADMIN_PASSWORD scaffolding, not the
   production onboarding design) created a partner_users row but never
   a matching identity_grants row. That was invisible right up until
   this account's own appliance activated -- POST /api/portal-login
   then switches from the simple local password check (partner_db.
   authenticate_detailed()) to the cloud-delegated one (appliance_
   identity.authenticate_operator()), which additionally requires a
   live identity_grants row resolving to the activated appliance's
   scope. A bootstrap admin with a perfectly correct password was
   denied with a generic "Invalid email or password" the moment its
   appliance activated, no matter how many times the password was
   reset.

2. The first fix for #1 used scope_type='partner' for that grant. Login
   itself started working again, but the Admin Portal was left
   effectively empty ("Your current role does not include
   manage_settings"): cloud_administrator_bridge() (main.py) -- the
   sole path a cloud-delegated session uses to reach the legacy Admin
   Portal's manage_settings-gated pages -- deliberately only recognizes
   a scope_type='global' administrator grant, by design excluding a
   partner-scoped (company-level) administrator from the Admin Portal
   (see test_cloud_administrator_bridge.py's own test_partner_scoped_
   administrator_cannot_reach_admin_portal). bootstrap_admin() now uses
   scope_type='global' (scope_id NULL) instead -- the one, already-
   documented scope 'administrator' legitimately has for a true
   top-level operator identity, matching what this account's pre-
   activation, scope-less local password check effectively granted it.

Both grant creation and idempotency checks below use scope_type='global'
throughout.
"""
import pytest
from fastapi.testclient import TestClient

import appliance_identity
import main
from database_backend import override_target
from partner_db import bootstrap_admin, connection, initialize_database, password_hash


BOOTSTRAP_EMAIL = "bootstrap-admin@example.test"
BOOTSTRAP_PASSWORD = "Sup3rSecret!123"
BOOTSTRAP_PARTNER_ID = "anyaicam-primary"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_bootstrap_admin.db"


@pytest.fixture()
def http_client(tmp_path, monkeypatch, db_path):
    users_file = tmp_path / "users.json"
    # A missing/empty USERS_FILE makes main.load_users() fall back to
    # default_users(), which -- entirely separately from bootstrap_admin()
    # below -- ALSO reads ANYAICAM_ADMIN_EMAIL/PASSWORD and seeds a
    # legacy "local-admin" users.json account with the same address.
    # That's a real, separate two-bootstrap-mechanisms situation worth
    # knowing about on its own, but it's not what this file tests: left
    # in place, portal-login would succeed via that legacy account
    # regardless of identity_grants, masking whether bootstrap_admin()'s
    # own grant is actually what let the login through. One harmless
    # placeholder user keeps load_users() from falling into that path.
    users_file.write_text('[{"id":"placeholder","email":"placeholder@example.test","role":"viewer","enabled":true,"password_hash":"","camera_ids":[]}]', encoding="utf-8")
    monkeypatch.setattr(main, "USERS_FILE", users_file)
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setenv("ANYAICAM_ADMIN_EMAIL", BOOTSTRAP_EMAIL)
    monkeypatch.setenv("ANYAICAM_ADMIN_PASSWORD", BOOTSTRAP_PASSWORD)
    with override_target(sqlite_path=db_path):
        initialize_database()  # runs bootstrap_admin() for real, same as a container start
        appliance_identity.reset_cloud_identity_backend_for_tests()
        with TestClient(main.app) as test_client:
            yield test_client
    appliance_identity.reset_cloud_identity_backend_for_tests()


def _seed_activated_appliance_under_bootstrap_partner(db_path, cloud_id="AIC-BOOTSTRAP1"):
    # Deliberately does NOT create its own partner_user or grant -- only
    # the appliance/customer/site/credential rows, so the login below
    # exercises exactly what bootstrap_admin() itself created.
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            now = "2026-08-27T00:00:00"
            db.execute(
                "INSERT OR IGNORE INTO customers(id,partner_id,name,company,email,status,trial_status,source,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("cust-1", BOOTSTRAP_PARTNER_ID, "Customer", "", "cust1@example.test", "active", "eligible", "real", now),
            )
            db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", ("site-1", "cust-1", "Site", now))
            db.execute(
                "INSERT INTO appliances(id,customer_id,site_id,cloud_id,partner_id,created_at) VALUES(?,?,?,?,?,?)",
                ("appl-1", "cust-1", "site-1", cloud_id, BOOTSTRAP_PARTNER_ID, now),
            )
            db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", ("cred-1", "appl-1", password_hash("appliance-credential"), now))


def _configure_own_appliance(monkeypatch, cloud_id="AIC-BOOTSTRAP1"):
    monkeypatch.setenv("ANYAICAM_APPLIANCE_ID", "appl-1")
    monkeypatch.setenv("ANYAICAM_APPLIANCE_CLOUD_ID", cloud_id)
    monkeypatch.setenv("ANYAICAM_APPLIANCE_CREDENTIAL", "appliance-credential")


def test_bootstrap_admin_can_log_in_before_activation(http_client, monkeypatch):
    monkeypatch.delenv("ANYAICAM_APPLIANCE_ID", raising=False)
    monkeypatch.delenv("ANYAICAM_APPLIANCE_CLOUD_ID", raising=False)
    monkeypatch.delenv("ANYAICAM_APPLIANCE_CREDENTIAL", raising=False)

    response = http_client.post(
        "/api/portal-login",
        json={"email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD, "portal": "administrator"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_bootstrap_admin_can_still_log_in_after_appliance_activation(http_client, db_path, monkeypatch):
    # The exact live failure: this must succeed now, not return the
    # generic "Invalid email or password" the missing grant used to
    # cause.
    _seed_activated_appliance_under_bootstrap_partner(db_path)
    _configure_own_appliance(monkeypatch)

    response = http_client.post(
        "/api/portal-login",
        json={"email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD, "portal": "administrator"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # scope_type='global' -- reaches the real Admin Portal, not the
    # Partner Portal's customer view (see the module docstring's #2).
    assert response.headers["location"] == "/admin-portal"


def test_admin_portal_manage_settings_pages_are_reachable_after_activation(http_client, db_path, monkeypatch):
    # The exact second live failure: login succeeding is not enough --
    # every Admin Portal sidebar page is gated on has_permission(user,
    # "manage_settings"), which only a scope_type='global' grant
    # satisfies via cloud_administrator_bridge(). Proves the bootstrap
    # admin actually reaches manage_settings-gated content, not just a
    # "your role doesn't include manage_settings" page rendered at 200.
    _seed_activated_appliance_under_bootstrap_partner(db_path)
    _configure_own_appliance(monkeypatch)

    login = http_client.post(
        "/api/portal-login",
        json={"email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD, "portal": "administrator"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/admin-portal"
    # partner_portal.SESSION_COOKIE is the actual cookie establish_
    # partner_session() sets for a cloud-delegated login (matches
    # test_cloud_administrator_bridge.py's identical pattern).
    import partner_portal
    session_cookie = login.cookies[partner_portal.SESSION_COOKIE]

    page = http_client.get("/admin-portal", cookies={partner_portal.SESSION_COOKIE: session_cookie})
    assert page.status_code == 200
    # The exact live symptom: "Your current role does not include
    # manage_settings" rendered at 200 instead of real page content.
    assert "does not include" not in page.text
    assert "manage_settings" not in page.text.lower()


def test_duplicate_bootstrap_runs_do_not_create_duplicate_grants(http_client, db_path):
    # bootstrap_admin() already ran once via the http_client fixture's
    # initialize_database() call.
    with override_target(sqlite_path=str(db_path)):
        bootstrap_admin()
        bootstrap_admin()
        with connection() as db:
            user_id = db.execute("SELECT id FROM partner_users WHERE email=?", (BOOTSTRAP_EMAIL,)).fetchone()["id"]
            count = db.execute(
                "SELECT count(*) FROM identity_grants WHERE user_id=? AND role='administrator' AND scope_type='global'",
                (user_id,),
            ).fetchone()[0]
    assert count == 1


def test_existing_admin_with_the_correct_grant_is_left_unchanged(http_client, db_path):
    # Confirms the idempotency check reads the grant it should, not just
    # that it doesn't over-create: capture the grant's id/granted_at
    # before and after a second bootstrap run.
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            user_id = db.execute("SELECT id FROM partner_users WHERE email=?", (BOOTSTRAP_EMAIL,)).fetchone()["id"]
            before = dict(db.execute(
                "SELECT id,granted_at FROM identity_grants WHERE user_id=? AND role='administrator' AND scope_type='global'",
                (user_id,),
            ).fetchone())
        bootstrap_admin()
        with connection() as db:
            after = dict(db.execute(
                "SELECT id,granted_at FROM identity_grants WHERE user_id=? AND role='administrator' AND scope_type='global'",
                (user_id,),
            ).fetchone())
    assert before == after


def test_existing_admin_grant_is_backfilled_without_any_password_env_var(http_client, db_path, monkeypatch):
    # Simulates the real sequence: the account already exists (created
    # earlier, with a password, by the http_client fixture's own
    # initialize_database() call), the password env var has since been
    # removed from vms.env (never left sitting in a persistent file, per
    # bootstrap_admin()'s TEMPORARY LOCAL BOOTSTRAP PATH contract), and
    # bootstrap_admin() runs again on a later container start -- it must
    # still be able to backfill a missing grant using only the email.
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            user_id = db.execute("SELECT id FROM partner_users WHERE email=?", (BOOTSTRAP_EMAIL,)).fetchone()["id"]
            db.execute("DELETE FROM identity_grants WHERE user_id=?", (user_id,))
    monkeypatch.delenv("ANYAICAM_ADMIN_PASSWORD", raising=False)

    with override_target(sqlite_path=str(db_path)):
        bootstrap_admin()
        with connection() as db:
            count = db.execute(
                "SELECT count(*) FROM identity_grants WHERE user_id=? AND role='administrator' AND scope_type='global' AND revoked_at IS NULL",
                (user_id,),
            ).fetchone()[0]
    assert count == 1


def test_backfill_never_touches_the_existing_password_hash(http_client, db_path, monkeypatch):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            row = db.execute("SELECT id,password_hash FROM partner_users WHERE email=?", (BOOTSTRAP_EMAIL,)).fetchone()
            user_id, original_hash = row["id"], row["password_hash"]
            db.execute("DELETE FROM identity_grants WHERE user_id=?", (user_id,))
    monkeypatch.delenv("ANYAICAM_ADMIN_PASSWORD", raising=False)

    with override_target(sqlite_path=str(db_path)):
        bootstrap_admin()
        with connection() as db:
            new_hash = db.execute("SELECT password_hash FROM partner_users WHERE id=?", (user_id,)).fetchone()["password_hash"]
    assert new_hash == original_hash


def test_password_less_backfill_is_also_idempotent(http_client, db_path, monkeypatch):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            user_id = db.execute("SELECT id FROM partner_users WHERE email=?", (BOOTSTRAP_EMAIL,)).fetchone()["id"]
            db.execute("DELETE FROM identity_grants WHERE user_id=?", (user_id,))
    monkeypatch.delenv("ANYAICAM_ADMIN_PASSWORD", raising=False)

    with override_target(sqlite_path=str(db_path)):
        bootstrap_admin()
        bootstrap_admin()
        with connection() as db:
            count = db.execute(
                "SELECT count(*) FROM identity_grants WHERE user_id=? AND role='administrator' AND scope_type='global'",
                (user_id,),
            ).fetchone()[0]
    assert count == 1


def test_password_less_backfilled_admin_can_log_in_after_activation(http_client, db_path, monkeypatch):
    # The exact live scenario end to end: password env var gone,
    # backfill runs on email alone, and the account still authenticates
    # successfully via the cloud-delegated path once its appliance is
    # activated -- using the ORIGINAL password from account creation,
    # never re-supplied.
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            user_id = db.execute("SELECT id FROM partner_users WHERE email=?", (BOOTSTRAP_EMAIL,)).fetchone()["id"]
            db.execute("DELETE FROM identity_grants WHERE user_id=?", (user_id,))
    monkeypatch.delenv("ANYAICAM_ADMIN_PASSWORD", raising=False)
    with override_target(sqlite_path=str(db_path)):
        bootstrap_admin()

    _seed_activated_appliance_under_bootstrap_partner(db_path)
    _configure_own_appliance(monkeypatch)

    response = http_client.post(
        "/api/portal-login",
        json={"email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD, "portal": "administrator"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_no_new_account_is_created_without_a_password(http_client, db_path, monkeypatch):
    # The other half of "password required only to create a brand-new
    # account": an email with no existing partner_users row and no
    # password must not create anything at all.
    monkeypatch.setenv("ANYAICAM_ADMIN_EMAIL", "never-created@example.test")
    monkeypatch.delenv("ANYAICAM_ADMIN_PASSWORD", raising=False)
    with override_target(sqlite_path=str(db_path)):
        bootstrap_admin()
        with connection() as db:
            row = db.execute("SELECT id FROM partner_users WHERE email=?", ("never-created@example.test",)).fetchone()
    assert row is None


def test_revoked_grant_is_recreated_by_a_password_less_rerun(http_client, db_path, monkeypatch):
    # Complements test_revoked_grant_is_still_rejected_normally below
    # (which proves a revoked grant denies login): this proves the
    # password-less backfill path still notices a live grant is missing
    # -- whether it was never created or was revoked -- and restores one,
    # exactly like the original password-bearing path already did.
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            user_id = db.execute("SELECT id FROM partner_users WHERE email=?", (BOOTSTRAP_EMAIL,)).fetchone()["id"]
            db.execute("UPDATE identity_grants SET revoked_at=? WHERE user_id=?", ("2026-08-27T00:00:00", user_id))
    monkeypatch.delenv("ANYAICAM_ADMIN_PASSWORD", raising=False)

    with override_target(sqlite_path=str(db_path)):
        bootstrap_admin()
        with connection() as db:
            live = db.execute(
                "SELECT count(*) FROM identity_grants WHERE user_id=? AND role='administrator' AND scope_type='global' AND revoked_at IS NULL",
                (user_id,),
            ).fetchone()[0]
    assert live == 1


def test_revoked_grant_is_still_rejected_normally(http_client, db_path, monkeypatch):
    # Not a claim about bootstrap_admin() re-granting over a revocation
    # -- this proves the existing, unmodified grant-resolution logic
    # still denies access once this specific grant is revoked, i.e. the
    # new grant doesn't somehow bypass revocation checks.
    _seed_activated_appliance_under_bootstrap_partner(db_path)
    _configure_own_appliance(monkeypatch)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            user_id = db.execute("SELECT id FROM partner_users WHERE email=?", (BOOTSTRAP_EMAIL,)).fetchone()["id"]
            db.execute("UPDATE identity_grants SET revoked_at=? WHERE user_id=? AND role='administrator' AND scope_type='global'", ("2026-08-27T00:00:00", user_id))

    response = http_client.post(
        "/api/portal-login",
        json={"email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD, "portal": "administrator"},
        follow_redirects=False,
    )
    assert response.status_code in (401, 403)
