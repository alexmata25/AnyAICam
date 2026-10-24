"""D-AUTHZ-2b bootstrap and rollback safety (Linux 1.2.7.1).

Green IDs: T-3 (rollback to b8 on migrated data keeps legacy admins out),
T-3a / Z6j2 (b8 with users.json ABSENT re-seeds from ANYAICAM_ADMIN_PASSWORD --
documented, and why the env removal is a production prerequisite), T-4 / Z6l
(missing / empty / corrupt users.json never yields a password-only admin on the
cloud), S-1 (retire migration: disabled, backed up, sessions purged, audited),
design (b) and (g) (bootstrap_admin never creates a password account or
re-creates a revoked grant on the cloud). Edge (E-1) unchanged.
"""
import json
import os
import stat

import pytest

import main
import partner_db
from authz_helpers import audit_actions, isolated_app, make_partner_user, set_cloud_profile
from partner_db import connection

ENV_EMAIL = "bootstrap@anyaicam.test"
ENV_PASSWORD = "env-bootstrap-password-123"
TOMBSTONE = "!retired-1.2.7.1"


@pytest.fixture()
def cloud(tmp_path, monkeypatch):
    with isolated_app(tmp_path, monkeypatch, cloud=True) as client:
        monkeypatch.setenv("ANYAICAM_ADMIN_EMAIL", ENV_EMAIL)
        monkeypatch.setenv("ANYAICAM_ADMIN_PASSWORD", ENV_PASSWORD)
        yield client


@pytest.fixture()
def edge(tmp_path, monkeypatch):
    with isolated_app(tmp_path, monkeypatch, cloud=False) as client:
        monkeypatch.setenv("ANYAICAM_ADMIN_EMAIL", ENV_EMAIL)
        monkeypatch.setenv("ANYAICAM_ADMIN_PASSWORD", ENV_PASSWORD)
        yield client


def _env_password_signs_in(users) -> bool:
    return any(user.get("enabled", True) and main.verify_password(ENV_PASSWORD, user.get("password_hash", "")) for user in users)


def _b8_load_users(path):
    """b8c7a7ed's load_users()/migrate_users()/default_users(), verbatim in
    effect: a non-empty list is migrated (an EMPTY local-admin hash is filled
    from ANYAICAM_ADMIN_PASSWORD); a missing/empty/corrupt file becomes a fresh
    enabled local-admin hashed from ANYAICAM_ADMIN_PASSWORD."""
    env_password = os.environ.get("ANYAICAM_ADMIN_PASSWORD", "")
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                for user in data:
                    user.setdefault("password_hash", "")
                    if user.get("id") == "local-admin" and not user.get("password_hash") and env_password:
                        user["password_hash"] = main.hash_password(env_password)
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return [{"id": "local-admin", "email": os.environ.get("ANYAICAM_ADMIN_EMAIL", "admin@local"), "role": "administrator",
             "enabled": True, "password_hash": main.hash_password(env_password) if env_password else ""}]


# ------------------------------------------------------------------ T-4 / Z6l


def test_missing_users_file_on_the_cloud_writes_a_disabled_tombstone(cloud):
    assert not main.USERS_FILE.exists()
    users = main.load_users()
    assert not _env_password_signs_in(users)
    tomb = next(user for user in users if user["id"] == "local-admin")
    assert tomb["enabled"] is False and tomb["password_hash"] == TOMBSTONE
    on_disk = json.loads(main.USERS_FILE.read_text(encoding="utf-8"))
    assert on_disk and not _env_password_signs_in(on_disk)


def test_empty_users_file_on_the_cloud_writes_the_tombstone(cloud):
    main.USERS_FILE.write_text("[]", encoding="utf-8")
    users = main.load_users()
    assert not _env_password_signs_in(users)
    assert json.loads(main.USERS_FILE.read_text(encoding="utf-8"))[0]["password_hash"] == TOMBSTONE


def test_corrupt_users_file_on_the_cloud_is_left_untouched(cloud, caplog):
    main.USERS_FILE.write_text("{not json", encoding="utf-8")
    users = main.load_users()
    assert users == []
    assert main.USERS_FILE.read_text(encoding="utf-8") == "{not json"


def test_default_and_migrate_users_never_hash_the_env_password_on_the_cloud(cloud):
    assert not _env_password_signs_in(main.default_users())
    migrated, _changed = main.migrate_users([{"id": "local-admin", "email": "", "role": "administrator", "enabled": True, "password_hash": ""}])
    assert migrated[0]["password_hash"] in ("", TOMBSTONE)


def test_restarts_never_re_enable_the_tombstone(cloud):
    main.load_users()
    for _ in range(3):
        users = main.load_users()
    assert not _env_password_signs_in(users)


# ------------------------------------------------------------------ T-3 / Z6j / Z6j2 (rollback)


def test_rollback_to_b8_on_tombstoned_data_cannot_sign_in_with_the_env_password(cloud):
    main.load_users()  # 1.2.7.1 writes the tombstone
    assert not _env_password_signs_in(_b8_load_users(main.USERS_FILE))


def test_rollback_to_b8_after_the_retire_migration_keeps_legacy_admins_out(cloud):
    main.save_users([{"id": "ops-1", "email": "ops@anyaicam.test", "role": "administrator", "enabled": True,
                      "password_hash": main.hash_password("ops-password-1234")}])
    main.retire_cloud_legacy_admins()
    users = _b8_load_users(main.USERS_FILE)
    assert not any(user.get("enabled", True) and main.verify_password("ops-password-1234", user.get("password_hash", "")) for user in users)


def test_b8_with_users_json_absent_re_seeds_from_the_env_password_documenting_t3a(tmp_path, monkeypatch):
    """Z6j2: the reason ANYAICAM_ADMIN_PASSWORD must be removed from the cloud
    env before deploy -- b8 itself, with users.json absent, re-creates a
    password admin from it (PROVEN-bypass with the env, not reproduced without)."""
    path = tmp_path / "users.json"
    monkeypatch.setenv("ANYAICAM_ADMIN_PASSWORD", ENV_PASSWORD)
    assert _env_password_signs_in(_b8_load_users(path))
    monkeypatch.delenv("ANYAICAM_ADMIN_PASSWORD")
    assert not _env_password_signs_in(_b8_load_users(path))


# ------------------------------------------------------------------ S-1 retire migration


def test_retire_cloud_legacy_admins_disables_backs_up_purges_and_audits(cloud):
    users = [
        {"id": "a1", "email": "a1@x.test", "role": "administrator", "enabled": True, "password_hash": main.hash_password("p" * 12)},
        {"id": "a2", "email": "a2@x.test", "role": "support_admin", "enabled": True, "password_hash": main.hash_password("p" * 12)},
        {"id": "a3", "email": "a3@x.test", "role": "installer", "enabled": True, "password_hash": main.hash_password("p" * 12)},
        {"id": "v1", "email": "v1@x.test", "role": "viewer", "enabled": True, "password_hash": main.hash_password("p" * 12)},
    ]
    main.save_users(users)
    original = main.USERS_FILE.read_bytes()
    for user_id in ("a1", "v1"):
        main.create_session(user_id)
    result = main.retire_cloud_legacy_admins()
    assert result["retired"] == 3
    after = {user["id"]: user for user in json.loads(main.USERS_FILE.read_text(encoding="utf-8"))}
    assert [after[i]["enabled"] for i in ("a1", "a2", "a3")] == [False, False, False]
    assert after["v1"]["enabled"] is True  # non-admin roles: reported for owner review, not changed
    backup = main.USERS_FILE.with_name("users.json.pre-1271")
    assert backup.read_bytes() == original
    if os.name == "posix":
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    sessions = main.load_sessions()
    assert {s["user_id"] for s in sessions.values()} == {"v1"}
    audits = [a for a in audit_actions() if a[1] == "legacy_admins.retired"]
    assert audits and json.loads(audits[-1][2])["count"] == 3
    assert "a1@x.test" not in audits[-1][2]
    # idempotent: nothing more to do, backup not overwritten
    assert main.retire_cloud_legacy_admins()["retired"] == 0
    assert backup.read_bytes() == original


def test_retire_is_a_noop_on_the_edge(edge):
    main.save_users([{"id": "a1", "email": "a1@x.test", "role": "administrator", "enabled": True, "password_hash": main.hash_password("p" * 12)}])
    assert main.retire_cloud_legacy_admins()["retired"] == 0
    assert main.load_users()[0]["enabled"] is True


def test_authenticated_user_refuses_an_enabled_legacy_admin_on_the_cloud_even_before_retirement(cloud):
    main.save_users([{"id": "a1", "email": "a1@x.test", "role": "administrator", "enabled": True, "password_hash": main.hash_password("p" * 12)}])
    token = main.create_session("a1")
    from starlette.requests import Request
    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "client": ("127.0.0.1", 1),
                       "headers": [(b"cookie", f"{main.SESSION_COOKIE_NAME}={token}".encode())]})
    assert main.authenticated_user(request) is None


# ------------------------------------------------------------------ configuration_issues


def test_configuration_issues_flags_the_admin_password_env_on_the_cloud(cloud):
    issue = next(i for i in main.configuration_issues() if i["key"] == "ANYAICAM_ADMIN_PASSWORD")
    assert issue["severity"] == "warning" and "remove" in issue["message"].lower()
    assert ENV_PASSWORD not in json.dumps(main.configuration_issues())


def test_configuration_issues_does_not_ask_for_the_admin_password_on_the_cloud(cloud, monkeypatch):
    monkeypatch.delenv("ANYAICAM_ADMIN_PASSWORD")
    assert not [i for i in main.configuration_issues() if i["key"] == "ANYAICAM_ADMIN_PASSWORD"]


# ------------------------------------------------------------------ partner_db.bootstrap_admin (design b/g)


def test_bootstrap_admin_creates_no_password_account_on_the_cloud(cloud):
    partner_db.bootstrap_admin()
    with connection() as db:
        assert db.execute("SELECT 1 FROM partner_users WHERE email=?", (ENV_EMAIL,)).fetchone() is None


def test_bootstrap_admin_never_recreates_a_revoked_global_grant_on_the_cloud(cloud):
    import appliance_identity
    uid = make_partner_user(ENV_EMAIL, "administrator", global_admin=True)
    with connection() as db:
        grant = db.execute("SELECT id FROM identity_grants WHERE user_id=?", (uid,)).fetchone()
        appliance_identity.revoke_grant(db, grant_id=grant["id"])
    partner_db.bootstrap_admin()
    with connection() as db:
        assert not appliance_identity.has_global_administrator_grant(db, email=ENV_EMAIL)


def test_bootstrap_admin_unchanged_on_the_edge(edge):
    partner_db.bootstrap_admin()
    with connection() as db:
        import appliance_identity
        assert appliance_identity.has_global_administrator_grant(db, email=ENV_EMAIL)


def test_edge_missing_users_file_still_gets_the_local_recovery_admin(edge):
    users = main.load_users()
    assert _env_password_signs_in(users)
