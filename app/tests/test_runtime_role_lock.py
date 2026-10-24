"""admin_auth_policy.is_cloud_profile() (Linux 1.2.7.1, D-AUTHZ-2b design §3):
the cloud profile is decided by EVERY configured source (main.RUNTIME_ROLE,
settings.runtime_role, feature_entitlements.runtime_role()) and by a database
lock written on the first cloud run, so a mis-set env var on the cloud host can
never silently drop it back to the edge's password-only rules."""
import logging

import pytest

import main
from authz_helpers import isolated_app, set_cloud_profile


@pytest.fixture()
def edge_db(tmp_path, monkeypatch):
    with isolated_app(tmp_path, monkeypatch, cloud=False) as client:
        yield client


def test_edge_database_and_edge_config_is_not_cloud(edge_db):
    import admin_auth_policy
    assert admin_auth_policy.is_cloud_profile() is False


@pytest.mark.parametrize("role", ["cloud", "combined"])
def test_any_cloud_source_makes_it_cloud(edge_db, monkeypatch, role):
    import admin_auth_policy
    monkeypatch.setattr(main, "RUNTIME_ROLE", role)
    admin_auth_policy.reset_cache_for_tests()
    assert admin_auth_policy.is_cloud_profile() is True
    monkeypatch.setattr(main, "RUNTIME_ROLE", "edge")
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", role)
    admin_auth_policy.reset_cache_for_tests()
    assert admin_auth_policy.is_cloud_profile() is True


def test_role_lock_keeps_the_cloud_profile_after_a_config_mismatch(edge_db, monkeypatch, caplog):
    import admin_auth_policy
    set_cloud_profile(monkeypatch, True)
    admin_auth_policy.record_role_lock()
    set_cloud_profile(monkeypatch, False)
    with caplog.at_level(logging.WARNING):
        assert admin_auth_policy.is_cloud_profile() is True
    assert "security.runtime_role_mismatch" in caplog.text


def test_role_lock_is_never_written_on_the_edge(edge_db):
    import admin_auth_policy
    admin_auth_policy.record_role_lock()
    admin_auth_policy.reset_cache_for_tests()
    assert admin_auth_policy.is_cloud_profile() is False


def test_cloud_database_initialised_with_the_cloud_role_is_locked(tmp_path, monkeypatch):
    import admin_auth_policy
    with isolated_app(tmp_path, monkeypatch, cloud=True, db_name="cloud.db"):
        set_cloud_profile(monkeypatch, False)
        assert admin_auth_policy.is_cloud_profile() is True
