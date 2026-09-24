"""Shared test isolation for process-wide state that production code keeps
across calls on purpose.

appliance_config_cache.py holds the appliance's latest configuration in
memory for the whole process (see its module docstring). Every test here
runs in one pytest process, so without a reset a configuration published
by one test's edge sync would be served to an unrelated later test.
"""
import pytest


@pytest.fixture(autouse=True)
def _reset_appliance_config_cache():
    try:
        import appliance_config_cache
    except ImportError:  # module path not importable in some isolated runs
        yield
        return
    appliance_config_cache.reset()
    yield
    appliance_config_cache.reset()


@pytest.fixture(autouse=True)
def _reset_facial_directory_sync_state():
    """facial_embedding_sync.py remembers the directory version it last
    applied (conditional sync) for the life of the process -- reset it so
    one test's applied version never turns another test's sync into a
    conditional one."""
    try:
        import facial_embedding_sync
    except ImportError:
        yield
        return
    facial_embedding_sync.reset_sync_state()
    yield
    facial_embedding_sync.reset_sync_state()


@pytest.fixture(autouse=True)
def _reset_password_reset_completion_limiter():
    """cloud_features' per-IP limit on /api/password-reset/complete is
    process-wide by design; every TestClient request comes from the same
    client address, so without a reset the suite's many reset completions
    would start hitting 429 partway through. Only touched if the module
    is already imported (never imports main/cloud_features on its own)."""
    import sys

    module = sys.modules.get("cloud_features")
    limiter = getattr(module, "_password_reset_complete_ip_limiter", None) if module else None
    if limiter is not None:
        limiter.events.clear()
    yield
