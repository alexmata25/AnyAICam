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


@pytest.fixture(autouse=True)
def _reset_partner_application_limiter():
    """website_partner's per-IP limit on the public partner application form
    is process-wide; every TestClient request shares one client address."""
    import sys

    module = sys.modules.get("website_partner")
    limiter = getattr(module, "_application_ip_limiter", None) if module else None
    if limiter is not None:
        limiter.events.clear()
    yield


@pytest.fixture(autouse=True)
def _ai_activities_end_within_their_test(request, monkeypatch):
    """main.ai_activities (ai_activity.py) is process-wide, and each open
    activity has a finaliser on a background loop that waits for it to go
    quiet. In one shared pytest process a finaliser outliving its test ran
    alongside later tests that monkeypatch asyncio/subprocess globally and
    deadlocked the suite. Every test therefore ends its activities at once
    (continuation gap 0) and starts/ends with an empty tracker; tests of
    continuous activity set their own limits (main._ai_activity_limits).
    Never imports main on its own."""
    import sys

    try:
        import ai_activity
    except ImportError:
        yield
        return
    if request.node.name != "test_continuation_gap_is_the_merge_gap_but_never_less_than_two_scans":
        monkeypatch.setattr(ai_activity, "continuation_gap", lambda merge_gap_seconds, scan_interval_seconds: 0.0)
    module = sys.modules.get("main")
    if module is not None and hasattr(module, "ai_activities"):
        module.ai_activities.reset()
    yield
    module = sys.modules.get("main")
    if module is not None and hasattr(module, "ai_activities"):
        module.ai_activities.reset()


@pytest.fixture()
def fake_stripe_prices(monkeypatch):
    """Answers main.stripe_api_get('/v1/prices/<id>') with the catalog amount
    for whichever catalog item a test configured that Price ID for, so the
    checkout price guard (require_stripe_price_matches_catalog) sees a
    matching Stripe Price. Tests that want a mismatch override `overrides`."""
    import main
    import pricing_catalog as pc
    overrides = {}

    def _get(path):
        price_id = path.rsplit("/", 1)[-1]
        if price_id in overrides:
            return overrides[price_id]
        for plan in pc.base_plans():
            if plan["stripe_price_id"] == price_id:
                return {"id": price_id, "unit_amount": plan["monthly_cents"], "currency": "usd", "active": True, "recurring": {"interval": "month"}}
        for addon in pc.addons():
            if addon["stripe_price_id"] == price_id:
                return {"id": price_id, "unit_amount": addon["monthly_cents"], "currency": "usd", "active": True, "recurring": {"interval": "month"}}
        for tier in pc.face_access_tiers():
            if tier["stripe_price_id"] == price_id:
                return {"id": price_id, "unit_amount": tier["monthly_cents_per_door"], "currency": "usd", "active": True, "recurring": {"interval": "month"}}
        for lic in pc.vms_licenses():
            if lic["stripe_price_id"] == price_id:
                return {"id": price_id, "unit_amount": lic["one_time_cents"], "currency": "usd", "active": True, "recurring": None}
        raise AssertionError(f"unexpected Stripe price lookup: {price_id}")

    monkeypatch.setattr(main, "stripe_api_get", _get)
    monkeypatch.setattr(main, "_VERIFIED_STRIPE_PRICES", {})
    return overrides
