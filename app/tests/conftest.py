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
