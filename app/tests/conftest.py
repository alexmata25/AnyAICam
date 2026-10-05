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


def policy_coupon(coupon_id: str) -> dict | None:
    """Stripe's answer for a Friends & Family coupon configured exactly as
    pricing_catalog's policy says, or None for an unknown coupon id."""
    import os
    import pricing_catalog as pc
    for discount_class, env_var in pc.FRIENDS_FAMILY_COUPON_ENV.items():
        if os.environ.get(env_var, "").strip() == coupon_id:
            return {"id": coupon_id, "percent_off": pc.FRIENDS_FAMILY_PERCENT_OFF[discount_class], "valid": True}
    return None


@pytest.fixture(autouse=True)
def _fresh_coupon_verification(monkeypatch):
    """friends_family caches verified coupons per process; every test starts empty."""
    try:
        import friends_family
    except Exception:
        return
    monkeypatch.setattr(friends_family, "_VERIFIED_COUPONS", {})


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
        if path.startswith("/v1/coupons/"):
            # A Friends & Family coupon configured as the policy says (tests
            # that want a mismatch override it by coupon id).
            coupon_id = path.split("/")[3].split("?")[0]
            if coupon_id in overrides:
                return overrides[coupon_id]
            coupon = policy_coupon(coupon_id)
            if coupon is None:
                raise AssertionError(f"unexpected Stripe coupon lookup: {coupon_id}")
            return coupon
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
        import os
        from hardware_orders import HARDWARE_CATALOG  # one-time hardware (2026-10-04)
        for _sku, _product, _name, cents, env_var in HARDWARE_CATALOG:
            if os.environ.get(env_var, "").strip() == price_id:
                return {"id": price_id, "unit_amount": cents, "currency": "usd", "active": True, "recurring": None}
        raise AssertionError(f"unexpected Stripe price lookup: {price_id}")

    monkeypatch.setattr(main, "stripe_api_get", _get)
    monkeypatch.setattr(main, "_VERIFIED_STRIPE_PRICES", {})
    return overrides


@pytest.fixture()
def stripe_follows_events(monkeypatch):
    """Stripe test double for tests written before billing read Stripe's
    current state. Since the Codex audit of 3f5b9c4 (finding 9) nothing is
    applied without that state, so these tests need a Stripe to ask. Here
    Stripe "now" is what the test's own events say, in the order sent: a
    subscription is as its latest subscription event describes it (or as its
    checkout created it), a subscription's first invoice is paid, and a
    payment is not refunded unless the test says so. Out-of-order, stale,
    refunded and cross-tenant cases are tested against an explicit Stripe
    in test_stripe_billing_launch.py / test_stripe_billing_policies.py.
    Tests may adjust the returned state ("subscriptions", "invoices",
    "intents") directly."""
    import analytics_entitlements
    import billing_status
    import customer_entitlements
    import sales_commissions
    import stripe_state

    now = {"subscriptions": {}, "invoices": {}, "intents": {}}

    def reader(path):
        kind, _, ident = path.split("?", 1)[0].rpartition("/")
        if kind == "/v1/subscriptions":
            if ident not in now["subscriptions"]:
                raise LookupError(f"Stripe has no subscription {ident}")
            return now["subscriptions"][ident]
        if kind == "/v1/invoices":
            first = ident.startswith("in_first_")
            return now["invoices"].get(ident) or {"id": ident, "object": "invoice", "status": "paid" if first else "open",
                                                  "amount_paid": 1 if first else 0, "payment_intent": f"pi_{ident}"}
        if kind == "/v1/payment_intents":
            return now["intents"].get(ident) or {"id": ident, "status": "succeeded",
                                                 "latest_charge": {"id": f"ch_{ident}", "amount": 1, "amount_refunded": 0}}
        raise AssertionError(f"unexpected Stripe read {path}")

    def observe(event):
        obj = (event.get("data") or {}).get("object") or {}
        event_type = str(event.get("type") or "")
        if event_type.startswith("customer.subscription.") and obj.get("id"):
            now["subscriptions"][obj["id"]] = dict(obj, latest_invoice=obj.get("latest_invoice") or f"in_first_{obj['id']}")
        elif event_type.startswith("checkout.session.") and obj.get("subscription"):
            metadata = dict(obj.get("metadata") or {})
            price = metadata.get("anyaicam_stripe_price_id")
            now["subscriptions"].setdefault(obj["subscription"], {
                "id": obj["subscription"], "object": "subscription", "customer": obj.get("customer"), "status": "active",
                "metadata": metadata, "latest_invoice": f"in_first_{obj['subscription']}",
                "items": {"data": [{"price": {"id": price}, "quantity": int(metadata.get("anyaicam_quantity") or 1)}]}})
        elif event_type.startswith("invoice.") and obj.get("subscription"):
            subscription_id = obj["subscription"]["id"] if isinstance(obj["subscription"], dict) else obj["subscription"]
            details = obj.get("subscription_details") or ((obj.get("parent") or {}).get("subscription_details")) or {}
            now["subscriptions"].setdefault(subscription_id, {
                "id": subscription_id, "object": "subscription", "customer": obj.get("customer"), "status": "active",
                "metadata": dict(details.get("metadata") or {}), "latest_invoice": obj.get("id")})

    monkeypatch.setattr(stripe_state, "_stripe_reader", lambda: reader)
    for module, name in ((customer_entitlements, "sync_entitlement_from_stripe_event"),
                         (analytics_entitlements, "sync_analytics_from_stripe_event"),
                         (billing_status, "sync_from_stripe_event"),
                         (sales_commissions, "sync_commissions_from_stripe_event")):
        def follow(event, _original=getattr(module, name)):
            observe(event)
            return _original(event)
        monkeypatch.setattr(module, name, follow)
    return now
