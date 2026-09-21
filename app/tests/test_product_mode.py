"""Local vs Hybrid product mode (2026-09-21): current_mode()'s 3-step
resolution order (explicit env var > persisted state file > legacy
unset), resolve_cloud_flag()'s "explicit env var always wins, otherwise
mode supplies the default" contract, and persist_mode()'s idempotent
write behavior -- see product_mode.py's own module docstring for the
full design rationale."""
import pytest

import product_mode as pm


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Every test gets its own state file path and starts with
    ANYAICAM_PRODUCT_MODE unset -- never the real
    /opt/anyaicam/data/config/product_mode.json, and never able to leak
    state between tests."""
    monkeypatch.setattr(pm, "STATE_FILE", tmp_path / "product_mode.json")
    monkeypatch.delenv(pm.PRODUCT_MODE_ENV, raising=False)


# ------------------------------------------------------------- current_mode()


def test_no_env_var_and_no_persisted_file_is_unset():
    assert pm.current_mode() == ""
    assert not pm.is_local()
    assert not pm.is_hybrid()


def test_env_var_local_is_read_directly():
    import os
    os.environ[pm.PRODUCT_MODE_ENV] = "local"
    try:
        assert pm.current_mode() == "local"
        assert pm.is_local()
    finally:
        del os.environ[pm.PRODUCT_MODE_ENV]


def test_env_var_hybrid_is_read_directly():
    import os
    os.environ[pm.PRODUCT_MODE_ENV] = "HYBRID"
    try:
        assert pm.current_mode() == "hybrid"
        assert pm.is_hybrid()
    finally:
        del os.environ[pm.PRODUCT_MODE_ENV]


def test_an_invalid_env_var_value_falls_back_to_persisted_or_unset():
    import os
    os.environ[pm.PRODUCT_MODE_ENV] = "nonsense"
    try:
        assert pm.current_mode() == ""
    finally:
        del os.environ[pm.PRODUCT_MODE_ENV]


def test_persisted_mode_is_read_when_no_env_var_is_set():
    pm.persist_mode("hybrid")
    assert pm.current_mode() == "hybrid"


def test_env_var_always_wins_over_a_persisted_mode():
    import os
    pm.persist_mode("hybrid")
    os.environ[pm.PRODUCT_MODE_ENV] = "local"
    try:
        assert pm.current_mode() == "local"
    finally:
        del os.environ[pm.PRODUCT_MODE_ENV]


# --------------------------------------------------------------- persist_mode()


def test_persist_mode_writes_the_file_and_reports_a_change():
    changed = pm.persist_mode("local")
    assert changed is True
    assert pm.STATE_FILE.exists()
    assert pm.current_mode() == "local"


def test_persist_mode_is_a_noop_when_the_mode_is_unchanged():
    pm.persist_mode("hybrid")
    changed_again = pm.persist_mode("hybrid")
    assert changed_again is False


def test_persist_mode_reports_a_change_on_a_real_transition():
    pm.persist_mode("local")
    changed = pm.persist_mode("hybrid")
    assert changed is True
    assert pm.current_mode() == "hybrid"


def test_persist_mode_rejects_an_invalid_value_and_writes_nothing():
    changed = pm.persist_mode("not_a_real_mode")
    assert changed is False
    assert not pm.STATE_FILE.exists()


def test_persist_mode_rejects_an_empty_value():
    assert pm.persist_mode("") is False
    assert pm.persist_mode(None) is False


# ------------------------------------------------------------ resolve_cloud_flag()


def test_explicit_true_env_var_wins_regardless_of_mode():
    import os
    pm.persist_mode("local")
    os.environ["ANYAICAM_TEST_FLAG"] = "true"
    try:
        assert pm.resolve_cloud_flag("ANYAICAM_TEST_FLAG") is True
    finally:
        del os.environ["ANYAICAM_TEST_FLAG"]


def test_explicit_false_env_var_wins_regardless_of_mode():
    import os
    pm.persist_mode("hybrid")
    os.environ["ANYAICAM_TEST_FLAG"] = "false"
    try:
        assert pm.resolve_cloud_flag("ANYAICAM_TEST_FLAG") is False
    finally:
        del os.environ["ANYAICAM_TEST_FLAG"]


def test_local_mode_defaults_an_unset_flag_to_false():
    pm.persist_mode("local")
    assert pm.resolve_cloud_flag("ANYAICAM_TEST_FLAG_UNSET") is False


def test_hybrid_mode_defaults_an_unset_flag_to_true():
    pm.persist_mode("hybrid")
    assert pm.resolve_cloud_flag("ANYAICAM_TEST_FLAG_UNSET") is True


def test_no_mode_configured_falls_back_to_legacy_default_false():
    assert pm.resolve_cloud_flag("ANYAICAM_TEST_FLAG_UNSET") is False


def test_no_mode_configured_falls_back_to_legacy_default_true_when_asked():
    assert pm.resolve_cloud_flag("ANYAICAM_TEST_FLAG_UNSET", legacy_default=True) is True


def test_an_empty_string_env_var_is_treated_as_unset_not_as_false():
    """Matches every existing flag's own original os.environ.get(...,
    'false').strip().lower()=='true' behavior: an env var present but
    blank was never distinguishable from unset before this module
    existed, so resolve_cloud_flag() must preserve that, not newly
    treat '' as an explicit false."""
    import os
    pm.persist_mode("hybrid")
    os.environ["ANYAICAM_TEST_FLAG_BLANK"] = "   "
    try:
        assert pm.resolve_cloud_flag("ANYAICAM_TEST_FLAG_BLANK") is True
    finally:
        del os.environ["ANYAICAM_TEST_FLAG_BLANK"]


# ---------------------------------------------------------------- FLAG_REGISTRY


def test_flag_registry_documents_every_governed_flag_used_in_this_codebase():
    expected = {
        "ANYAICAM_ANALYTICS_SYNC_ENABLED",
        "ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED",
        "ANYAICAM_FACIAL_EMBEDDING_SYNC_ENABLED",
        "ANYAICAM_LIVE_RELAY_ENABLED",
        "ANYAICAM_RECORDING_UPLOAD_ENABLED",
        "ANYAICAM_LIVE_P2P_ENABLED",
    }
    assert expected == set(pm.FLAG_REGISTRY.keys())
