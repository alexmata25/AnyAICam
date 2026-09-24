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
