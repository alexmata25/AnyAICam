"""Tests for the AnyAiCam Base Price + Partner Markup prototype.

Pure stdlib + pytest, zero dependency on main.py/partner_db/`/app` --
runs anywhere plain Python 3 runs, no container required. Proves the
new commercial model works, per explicit instruction, before any of
it is applied to production.
"""

from datetime import datetime

import pytest

import base_price_pricing as bpp


# ============================================================ 1. partner cannot sell below base price


def test_partner_cannot_sell_below_base_price():
    with pytest.raises(bpp.BasePriceError):
        bpp.calculate_customer_quote("local", "2mp", customer_price=1.99)


def test_partner_cannot_sell_one_cent_below_base_price():
    base = bpp.get_base_price("local", "2mp")
    with pytest.raises(bpp.BasePriceError):
        bpp.calculate_customer_quote("local", "2mp", customer_price=base - 0.01)


@pytest.mark.parametrize("product,resolution,retention", [
    ("motion", "2mp", "7"), ("motion", "4mp", "14"), ("motion", "8mp", "30"),
    ("continuous", "2mp", "2"), ("continuous", "4mp", "7"), ("continuous", "8mp", "30"),
])
def test_partner_cannot_sell_below_base_price_any_cloud_plan(product, resolution, retention):
    base = bpp.get_base_price(product, resolution, retention)
    with pytest.raises(bpp.BasePriceError):
        bpp.calculate_customer_quote(product, resolution, customer_price=base - 0.50, retention_days=retention)


# ============================================================ 2. partner can sell exactly at base price


def test_partner_can_sell_exactly_at_base_price():
    base = bpp.get_base_price("local", "4mp")
    quote = bpp.calculate_customer_quote("local", "4mp", customer_price=base)
    assert quote.customer_price == base
    assert quote.partner_earnings == 0.0
    assert quote.anyaicam_owed == base


def test_partner_can_sell_exactly_at_base_price_cloud_plan():
    base = bpp.get_base_price("continuous", "8mp", "14")
    quote = bpp.calculate_customer_quote("continuous", "8mp", customer_price=base, retention_days="14")
    assert quote.partner_earnings == 0.0


# ============================================================ 3. partner can add arbitrary positive markup


@pytest.mark.parametrize("markup", [0.01, 1.00, 5.00, 50.00, 999.99])
def test_partner_can_add_arbitrary_positive_markup(markup):
    base = bpp.get_base_price("motion", "2mp", "2")
    quote = bpp.calculate_customer_quote("motion", "2mp", customer_price=base + markup, retention_days="2")
    assert quote.customer_price == round(base + markup, 2)
    assert quote.partner_earnings == round(markup, 2)


def test_a_second_partner_can_charge_more_and_earn_more_at_the_same_base_price():
    """Directly models the example in the approved commercial model:
    two partners selling the same plan at different prices both clear
    the same AnyAiCam base price; the higher-charging partner simply
    earns more, with no cap and no renegotiation of AnyAiCam's cut."""
    base = bpp.get_base_price("motion", "4mp", "30")
    partner_a = bpp.calculate_customer_quote("motion", "4mp", customer_price=base + 4.0, retention_days="30")
    partner_b = bpp.calculate_customer_quote("motion", "4mp", customer_price=base + 7.0, retention_days="30")
    assert partner_a.anyaicam_owed == partner_b.anyaicam_owed == base
    assert partner_b.partner_earnings > partner_a.partner_earnings
    assert partner_b.partner_earnings - partner_a.partner_earnings == 3.0


# ============================================================ 4. partner earnings equal retail minus base price


@pytest.mark.parametrize("product,resolution,retention,customer_price", [
    ("local", "2mp", None, 6.50),
    ("local", "8mp", None, 25.00),
    ("motion", "2mp", "7", 9.99),
    ("continuous", "4mp", "30", 150.00),
])
def test_partner_earnings_always_equal_customer_price_minus_base_price(product, resolution, retention, customer_price):
    quote = bpp.calculate_customer_quote(product, resolution, customer_price=customer_price, retention_days=retention)
    base = bpp.get_base_price(product, resolution, retention)
    assert quote.partner_earnings == round(customer_price - base, 2)


# ============================================================ 5. AnyAiCam amount owed is invariant to partner markup


@pytest.mark.parametrize("markup", [0, 1, 10, 100, 1000])
def test_anyaicam_owed_is_always_exactly_the_base_price_regardless_of_markup(markup):
    base = bpp.get_base_price("continuous", "2mp", "14")
    quote = bpp.calculate_customer_quote("continuous", "2mp", customer_price=base + markup, retention_days="14")
    assert quote.anyaicam_owed == base


# ============================================================ 6. Local/Motion/Continuous use the correct authoritative base prices


@pytest.mark.parametrize("resolution,expected", [("2mp", 2.00), ("4mp", 3.00), ("8mp", 4.50)])
def test_local_recording_base_prices(resolution, expected):
    assert bpp.get_base_price("local", resolution) == expected


@pytest.mark.parametrize("resolution,retention,expected", [
    ("2mp", "2", 2.00), ("2mp", "7", 2.00), ("2mp", "14", 2.00), ("2mp", "30", 2.00),
    ("4mp", "2", 3.00), ("4mp", "7", 3.00), ("4mp", "14", 3.00), ("4mp", "30", 3.00),
    ("8mp", "2", 4.50), ("8mp", "7", 4.50), ("8mp", "14", 4.50), ("8mp", "30", 4.50),
])
def test_motion_cloud_base_prices(resolution, retention, expected):
    assert bpp.get_base_price("motion", resolution, retention) == expected


@pytest.mark.parametrize("resolution,retention,expected", [
    ("2mp", "2", 3.00), ("2mp", "7", 8.50), ("2mp", "14", 16.50), ("2mp", "30", 35.00),
    ("4mp", "2", 5.00), ("4mp", "7", 15.50), ("4mp", "14", 30.00), ("4mp", "30", 64.00),
    ("8mp", "2", 8.50), ("8mp", "7", 28.00), ("8mp", "14", 54.50), ("8mp", "30", 115.50),
])
def test_continuous_cloud_base_prices(resolution, retention, expected):
    assert bpp.get_base_price("continuous", resolution, retention) == expected


def test_ordering_local_le_motion_le_continuous_every_cell():
    for resolution in ("2mp", "4mp", "8mp"):
        local = bpp.get_base_price("local", resolution)
        for retention in ("2", "7", "14", "30"):
            motion = bpp.get_base_price("motion", resolution, retention)
            continuous = bpp.get_base_price("continuous", resolution, retention)
            assert local <= motion <= continuous


# ============================================================ 7. quote calculations


def test_quote_calculation_full_shape():
    quote = bpp.calculate_customer_quote("motion", "4mp", customer_price=9.99, retention_days="7")
    assert quote.product == "motion"
    assert quote.resolution == "4mp"
    assert quote.retention_days == "7"
    assert quote.base_price == 3.00
    assert quote.customer_price == 9.99
    assert quote.partner_earnings == 6.99
    assert quote.anyaicam_owed == 3.00
    assert quote.pricing_version == bpp.PRICING_VERSION


def test_local_recording_quote_has_no_retention():
    quote = bpp.calculate_customer_quote("local", "2mp", customer_price=5.00)
    assert quote.retention_days is None


def test_unknown_product_is_rejected():
    with pytest.raises(ValueError, match="Unknown product"):
        bpp.calculate_customer_quote("premium_deluxe", "2mp", customer_price=100.0)


def test_unknown_resolution_is_rejected():
    with pytest.raises(ValueError, match="Unknown resolution"):
        bpp.calculate_customer_quote("local", "16mp", customer_price=100.0)


def test_unknown_retention_is_rejected():
    with pytest.raises(ValueError, match="Unknown resolution/retention"):
        bpp.calculate_customer_quote("motion", "2mp", customer_price=100.0, retention_days="99")


# ============================================================ 8. "activation" (creating a durable record from a quote)


def test_activation_equivalent_persists_the_quote_fields_verbatim():
    """Models what a real activation route would do: take a
    CustomerQuote and persist its fields as a durable record. Proves
    every field needed for later billing/reporting is present and
    internally consistent at the moment of activation."""
    quote = bpp.calculate_customer_quote("continuous", "8mp", customer_price=200.0, retention_days="30", now=datetime(2026, 8, 23, 12, 0, 0))
    activation_record = {
        "product": quote.product, "resolution": quote.resolution, "retention_days": quote.retention_days,
        "base_price": quote.base_price, "customer_price": quote.customer_price,
        "partner_earnings": quote.partner_earnings, "pricing_version": quote.pricing_version,
        "activated_at": quote.quoted_at,
    }
    assert activation_record["base_price"] == 115.50
    assert activation_record["partner_earnings"] == 84.50
    assert activation_record["activated_at"] == "2026-08-23T12:00:00"


# ============================================================ 9. customer-facing price never exposes internal figures


def test_customer_facing_view_excludes_base_price_and_partner_earnings():
    quote = bpp.calculate_customer_quote("motion", "8mp", customer_price=25.0, retention_days="14")
    view = bpp.customer_facing_view(quote)
    assert view == {"product": "motion", "resolution": "8mp", "retention_days": "14", "price": 25.0}
    assert "base_price" not in view
    assert "partner_earnings" not in view
    assert "anyaicam_owed" not in view


def test_partner_facing_view_excludes_no_aws_cost_field_exists_at_all():
    """There is no AWS-cost or AnyAiCam-internal-margin field ANYWHERE
    on CustomerQuote -- proving the partner view can't leak it isn't
    just about which keys get copied, it's that the data never enters
    the object partners see in the first place."""
    quote = bpp.calculate_customer_quote("continuous", "4mp", customer_price=120.0, retention_days="14")
    view = bpp.partner_facing_view(quote)
    assert "aws_cost" not in view
    assert not any("aws" in key.lower() for key in view)
    assert not any("margin" in key.lower() for key in view)
    assert set(view.keys()) == {"product", "resolution", "retention_days", "anyaicam_base_price", "standard_retail_price", "customer_price", "partner_earnings"}


# ============================================================ 10. historical quote immutability across a pricing-version change


def test_historical_quote_is_unaffected_by_a_later_base_price_change():
    """The core historical-protection guarantee: a CustomerQuote
    returned in the past is a plain, immutable dataclass -- nothing
    in this module re-derives its fields later. Simulates a base-price
    increase (mutating the module-level table, exactly like editing
    pricing_config.json in production) and confirms the already-issued
    quote object's own fields are untouched."""
    old_quote = bpp.calculate_customer_quote("local", "2mp", customer_price=5.00)
    assert old_quote.base_price == 2.00

    original_price = bpp.LOCAL_RECORDING_BASE_PRICE["2mp"]
    try:
        bpp.LOCAL_RECORDING_BASE_PRICE["2mp"] = 3.50  # simulates a real base-price increase
        assert bpp.get_base_price("local", "2mp") == 3.50  # confirms the live table really changed

        # The old, already-returned quote is a frozen dataclass -- unaffected.
        assert old_quote.base_price == 2.00
        assert old_quote.customer_price == 5.00
        assert old_quote.partner_earnings == 3.00

        with pytest.raises(AttributeError):
            old_quote.base_price = 3.50  # frozen -- cannot even be mutated by mistake
    finally:
        bpp.LOCAL_RECORDING_BASE_PRICE["2mp"] = original_price


# ============================================================ 11. pricing-version changes


def test_pricing_version_is_stamped_on_every_quote():
    quote = bpp.calculate_customer_quote("motion", "2mp", customer_price=5.0, retention_days="2")
    assert quote.pricing_version == "2026.08-baseprice-v1"


def test_a_pricing_version_bump_does_not_alter_already_issued_quotes():
    old_quote = bpp.calculate_customer_quote("motion", "2mp", customer_price=5.0, retention_days="2")
    original_version = bpp.PRICING_VERSION
    try:
        bpp.PRICING_VERSION = "2027.01-baseprice-v2"  # simulates a future version bump
        new_quote = bpp.calculate_customer_quote("motion", "2mp", customer_price=5.0, retention_days="2")
        assert old_quote.pricing_version == "2026.08-baseprice-v1"  # unchanged, frozen at creation
        assert new_quote.pricing_version == "2027.01-baseprice-v2"  # reflects the live version
    finally:
        bpp.PRICING_VERSION = original_version


# ============================================================ 12. future base-price increases (a data change, not a code change)


def test_a_flat_across_the_board_base_price_increase_is_a_pure_data_edit():
    """Directly proves the explicit "if we raise every base price by
    $1 next year, that's a data change, not a rewrite" requirement --
    every base price shifts, and calculate_customer_quote() needs zero
    code changes to reflect it."""
    originals = {
        "local": dict(bpp.LOCAL_RECORDING_BASE_PRICE),
        "motion": {res: dict(vals) for res, vals in bpp.MOTION_CLOUD_BASE_PRICE.items()},
        "continuous": {res: dict(vals) for res, vals in bpp.CONTINUOUS_CLOUD_BASE_PRICE.items()},
    }
    try:
        for res in bpp.LOCAL_RECORDING_BASE_PRICE:
            bpp.LOCAL_RECORDING_BASE_PRICE[res] = round(bpp.LOCAL_RECORDING_BASE_PRICE[res] + 1.0, 2)
        for table in (bpp.MOTION_CLOUD_BASE_PRICE, bpp.CONTINUOUS_CLOUD_BASE_PRICE):
            for res in table:
                for ret in table[res]:
                    table[res][ret] = round(table[res][ret] + 1.0, 2)

        assert bpp.get_base_price("local", "2mp") == 3.00
        assert bpp.get_base_price("motion", "8mp", "30") == 5.50
        assert bpp.get_base_price("continuous", "4mp", "14") == 31.00

        quote = bpp.calculate_customer_quote("local", "2mp", customer_price=3.00)
        assert quote.base_price == 3.00
        assert quote.partner_earnings == 0.0
    finally:
        bpp.LOCAL_RECORDING_BASE_PRICE.clear()
        bpp.LOCAL_RECORDING_BASE_PRICE.update(originals["local"])
        bpp.MOTION_CLOUD_BASE_PRICE.clear()
        bpp.MOTION_CLOUD_BASE_PRICE.update(originals["motion"])
        bpp.CONTINUOUS_CLOUD_BASE_PRICE.clear()
        bpp.CONTINUOUS_CLOUD_BASE_PRICE.update(originals["continuous"])


# ============================================================ 13. removal of old 60/40 assumptions


def test_no_sixty_forty_split_constant_exists_anywhere_in_this_module():
    """Structural proof, not just a behavioral one: reads this
    module's own source and confirms no 0.6/0.4/60%/40% split
    constant was carried over."""
    import inspect
    source = inspect.getsource(bpp)
    for forbidden in ("0.6", "0.4", "60%", "40%", "* 0.60", "* 0.40", "sixty", "forty"):
        assert forbidden not in source, f"found a 60/40-flavored token in base_price_pricing.py: {forbidden!r}"


def test_partner_earnings_is_not_capped_or_percentage_limited():
    """A partner charging 10x the base price keeps the entire markup
    -- no implicit cap, no residual percentage-based ceiling anywhere
    in the calculation."""
    base = bpp.get_base_price("local", "8mp")
    quote = bpp.calculate_customer_quote("local", "8mp", customer_price=base * 10)
    assert quote.partner_earnings == round(base * 9, 2)


# ============================================================ 14. no accidental modification to recording/analytics/motion-gating/talk-down/live-view/uploads/retention/appliance behavior


def test_this_module_has_zero_dependency_on_any_video_system_code():
    """Structural guarantee: this prototype cannot possibly touch
    recording, analytics, motion-gating, talk-down, live view,
    uploads, retention, or appliance behavior, because it doesn't
    import anything besides dataclasses/datetime -- confirmed by
    reading its own import statements directly, not just by
    assumption."""
    import ast
    with open(bpp.__file__) as f:
        tree = ast.parse(f.read())
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    forbidden_video_system_modules = {
        "main", "recording_uploader", "talk_down_transport", "talk_audio_relay",
        "motion_detector", "live_view_page", "live_view_sessions", "analytics_sync",
        "partner_db", "appliance_cloud",
    }
    assert imported_modules.isdisjoint(forbidden_video_system_modules), (
        f"base_price_pricing.py imports video-system code: {imported_modules & forbidden_video_system_modules}"
    )
    assert imported_modules == {"dataclasses", "datetime"}


# ============================================================ 15. channel-conflict safety: three-tier price separation


def test_public_website_view_never_includes_base_price():
    """The core channel-conflict guardrail: AnyAiCam.com's own pricing
    display function has no base_price field anywhere in its return
    value or its own source -- not just "doesn't show it this time,"
    structurally cannot show it."""
    view = bpp.public_website_view("motion", "4mp", "14")
    assert "base_price" not in view
    assert "anyaicam_base_price" not in view
    assert view == {"product": "motion", "resolution": "4mp", "retention_days": "14", "price": 9.99}


def test_public_website_view_shows_standard_retail_not_a_partner_price():
    view = bpp.public_website_view("continuous", "8mp", "30")
    assert view["price"] == bpp.get_standard_retail_price("continuous", "8mp", "30")
    assert view["price"] == 188.00


@pytest.mark.parametrize("product,resolution,retention", [
    ("local", "2mp", None), ("local", "4mp", None), ("local", "8mp", None),
    ("motion", "2mp", "7"), ("motion", "8mp", "30"), ("continuous", "4mp", "14"), ("continuous", "8mp", "2"),
])
def test_standard_retail_is_always_at_or_above_base_price(product, resolution, retention):
    """The other channel-conflict guardrail: AnyAiCam's own published
    price can never be below its own required minimum -- otherwise a
    direct sale would lose money outright, not just undercut a
    partner."""
    base = bpp.get_base_price(product, resolution, retention)
    retail = bpp.get_standard_retail_price(product, resolution, retention)
    assert retail >= base


def test_standard_retail_has_real_headroom_above_base_not_just_barely_above():
    """A thin gap here would let AnyAiCam's own direct price
    effectively match a partner's floor, leaving no room for a partner
    to ever profitably compete -- this asserts the gap is a real,
    meaningful margin (at least 25% of the base price itself), not a
    token cent above it."""
    for product, table in (("local", bpp.LOCAL_RECORDING_BASE_PRICE), ("motion", bpp.MOTION_CLOUD_BASE_PRICE), ("continuous", bpp.CONTINUOUS_CLOUD_BASE_PRICE)):
        if product == "local":
            for resolution, base in table.items():
                retail = bpp.get_standard_retail_price(product, resolution)
                assert retail - base >= base * 0.25, f"{product}/{resolution}: only ${retail - base:.2f} headroom above ${base:.2f} base"
        else:
            for resolution, retentions in table.items():
                for retention, base in retentions.items():
                    retail = bpp.get_standard_retail_price(product, resolution, retention)
                    assert retail - base >= base * 0.25, f"{product}/{resolution}/{retention}d: only ${retail - base:.2f} headroom above ${base:.2f} base"


def test_partner_sees_both_base_price_and_standard_retail():
    quote = bpp.calculate_customer_quote("motion", "2mp", customer_price=5.99, retention_days="7", partner_id="partner-acme")
    view = bpp.partner_facing_view(quote)
    assert view["anyaicam_base_price"] == bpp.get_base_price("motion", "2mp", "7")
    assert view["standard_retail_price"] == bpp.get_standard_retail_price("motion", "2mp", "7")


def test_customer_never_sees_base_price_or_standard_retail_only_their_own_agreed_price():
    quote = bpp.calculate_customer_quote("motion", "2mp", customer_price=5.99, retention_days="7", partner_id="partner-acme")
    view = bpp.customer_facing_view(quote)
    assert "anyaicam_base_price" not in view
    assert "standard_retail_price" not in view
    assert "partner_earnings" not in view
    assert view["price"] == 5.99


# ============================================================ 16. direct AnyAiCam sales never undercut partners


def test_direct_sale_always_charges_standard_retail():
    quote = bpp.calculate_direct_sale_quote("continuous", "2mp", retention_days="7")
    assert quote.customer_price == bpp.get_standard_retail_price("continuous", "2mp", "7")
    assert quote.customer_price == 14.00


def test_direct_sale_is_attributed_to_anyaicam_own_partner_id():
    quote = bpp.calculate_direct_sale_quote("local", "4mp")
    assert quote.attributed_partner_id == bpp.ANYAICAM_DIRECT_PARTNER_ID
    assert quote.attributed_partner_id == "anyaicam-primary"


def test_direct_sale_price_is_never_below_any_real_partner_quote_would_need():
    """A partner and AnyAiCam direct, quoting the same plan, both clear
    at least the base price -- but AnyAiCam direct never charges LESS
    than a partner reasonably would, since it always uses standard
    retail (not the bare minimum base price)."""
    direct = bpp.calculate_direct_sale_quote("motion", "8mp", retention_days="14")
    partner_at_floor = bpp.calculate_customer_quote("motion", "8mp", customer_price=bpp.get_base_price("motion", "8mp", "14"), retention_days="14", partner_id="partner-acme")
    assert direct.customer_price > partner_at_floor.customer_price


def test_a_real_partner_quote_defaults_to_a_named_partner_id_not_anyaicam():
    quote = bpp.calculate_customer_quote("local", "2mp", customer_price=6.00, partner_id="partner-acme")
    assert quote.attributed_partner_id == "partner-acme"
    assert quote.attributed_partner_id != bpp.ANYAICAM_DIRECT_PARTNER_ID


# ============================================================ 17. partner can discount below standard retail, never below base


def test_partner_can_discount_below_standard_retail_but_not_below_base():
    base = bpp.get_base_price("motion", "4mp", "30")
    retail = bpp.get_standard_retail_price("motion", "4mp", "30")
    discounted_price = round((base + retail) / 2, 2)  # a real discount below retail, still comfortably above base
    assert base < discounted_price < retail

    quote = bpp.calculate_customer_quote("motion", "4mp", customer_price=discounted_price, retention_days="30", partner_id="partner-acme")
    assert quote.customer_price == discounted_price
    assert quote.anyaicam_owed == base  # AnyAiCam still receives its full required base regardless of the partner's discount


def test_partner_discounting_all_the_way_to_the_base_price_still_pays_anyaicam_in_full():
    base = bpp.get_base_price("continuous", "2mp", "7")
    quote = bpp.calculate_customer_quote("continuous", "2mp", customer_price=base, retention_days="7", partner_id="partner-acme")
    assert quote.anyaicam_owed == base
    assert quote.partner_earnings == 0.0  # the partner just makes no markup at this price -- still a valid, allowed quote


# ============================================================ 18. installation/support/warranty/service charges are structurally separate from subscription markup


@pytest.mark.parametrize("service_type", ["installation", "support", "extended_warranty", "maintenance", "networking", "monitoring"])
def test_service_charges_cover_every_named_service_type(service_type):
    charge = bpp.calculate_service_charge(service_type, partner_price=150.0)
    assert charge.service_type == service_type
    assert charge.partner_price == 150.0
    assert charge.anyaicam_cost == 0.0


def test_service_charge_has_no_relationship_to_subscription_base_price_or_partner_earnings():
    """Structural proof: ServiceCharge and CustomerQuote share zero
    fields, so a service charge can never be silently added into or
    confused with subscription partner_earnings."""
    quote_fields = set(bpp.CustomerQuote.__dataclass_fields__)
    service_fields = set(bpp.ServiceCharge.__dataclass_fields__)
    assert quote_fields.isdisjoint(service_fields)


def test_an_enhanced_package_keeps_subscription_and_service_charges_as_separate_line_items():
    """Directly models the example from the approved design: a partner
    selling an 'enhanced package' at $12 where the base subscription
    is $8 and installation is billed separately, rather than folding
    everything into one undifferentiated subscription markup number."""
    subscription = bpp.calculate_customer_quote("continuous", "2mp", customer_price=8.00, retention_days="2", partner_id="partner-acme")
    installation = bpp.calculate_service_charge("installation", partner_price=4.00)
    assert subscription.customer_price == 8.00
    assert installation.partner_price == 4.00
    # The two are never summed into a single subscription price by this module -- a caller must deliberately combine them for an invoice total, they don't merge implicitly.
    assert not hasattr(subscription, "total_with_services")


# ============================================================ 19. structural: three-tier fields present, correctly ordered, on every quote


@pytest.mark.parametrize("product,resolution,retention", [
    ("local", "2mp", None), ("motion", "4mp", "14"), ("continuous", "8mp", "30"),
])
def test_quote_carries_all_three_price_tiers_in_correct_order(product, resolution, retention):
    base = bpp.get_base_price(product, resolution, retention)
    retail = bpp.get_standard_retail_price(product, resolution, retention)
    quote = bpp.calculate_customer_quote(product, resolution, customer_price=retail, retention_days=retention, partner_id="partner-acme")
    assert quote.base_price == base
    assert quote.standard_retail_price == retail
    assert quote.base_price <= quote.standard_retail_price <= quote.customer_price or quote.customer_price == quote.standard_retail_price


# ============================================================ 20. partner attribution: QR/link/code checkout, hijack protection, transfers


def test_new_customer_with_valid_token_is_attributed_to_that_partner():
    token = bpp.generate_attribution_token("partner-acme", "tok-1", now=datetime(2026, 1, 1))
    decision = bpp.resolve_attribution(None, token, now=datetime(2026, 1, 2))
    assert decision.resolved_partner_id == "partner-acme"
    assert decision.previous_partner_id is None
    assert decision.reason == "new_customer_attributed_via_token"


def test_new_customer_with_no_token_defaults_to_anyaicam_direct():
    decision = bpp.resolve_attribution(None, None)
    assert decision.resolved_partner_id == bpp.ANYAICAM_DIRECT_PARTNER_ID
    assert decision.reason == "new_customer_no_token_direct"


def test_new_customer_with_expired_token_falls_back_to_direct_without_blocking_checkout():
    token = bpp.generate_attribution_token("partner-acme", "tok-old", valid_days=30, now=datetime(2026, 1, 1))
    decision = bpp.resolve_attribution(None, token, now=datetime(2026, 3, 1))  # well past the 30-day expiry
    assert decision.resolved_partner_id == bpp.ANYAICAM_DIRECT_PARTNER_ID
    assert decision.reason == "invalid_token_defaulted_to_direct"


def test_new_customer_with_revoked_token_falls_back_to_direct():
    token = bpp.AttributionToken(token="tok-x", partner_id="partner-acme", created_at="2026-01-01T00:00:00", expires_at=None, revoked=True)
    decision = bpp.resolve_attribution(None, token)
    assert decision.resolved_partner_id == bpp.ANYAICAM_DIRECT_PARTNER_ID
    assert decision.reason == "invalid_token_defaulted_to_direct"


def test_previously_direct_customer_can_be_reattributed_to_a_partner():
    """A customer who checked out with no partner isn't a protected
    relationship -- a later valid token can attribute them."""
    token = bpp.generate_attribution_token("partner-acme", "tok-2")
    decision = bpp.resolve_attribution(bpp.ANYAICAM_DIRECT_PARTNER_ID, token)
    assert decision.resolved_partner_id == "partner-acme"
    assert decision.previous_partner_id == bpp.ANYAICAM_DIRECT_PARTNER_ID
    assert decision.reason == "reattributed_from_direct_to_partner"


def test_established_partner_customer_is_protected_from_a_different_partners_token():
    """The core anti-hijack rule: partner-acme already owns this
    customer; partner-rival's valid token must NOT move the customer."""
    rival_token = bpp.generate_attribution_token("partner-rival", "tok-3")
    decision = bpp.resolve_attribution("partner-acme", rival_token)
    assert decision.resolved_partner_id == "partner-acme"
    assert decision.reason == "hijack_attempt_blocked_existing_partner_protected"


def test_established_partner_customer_stays_attributed_with_no_token_presented():
    """Models renewals, plan changes, camera count changes, and
    cancel-then-reactivate: no new token is typically presented, and
    attribution must simply persist."""
    decision = bpp.resolve_attribution("partner-acme", None)
    assert decision.resolved_partner_id == "partner-acme"
    assert decision.reason == "existing_attribution_preserved"


def test_established_partner_customer_returning_directly_still_keeps_their_partner():
    """'Customer leaves and later returns directly to AnyAiCam.com':
    once server-side attribution exists, a session with no token at all
    (as if they typed the URL in fresh) still resolves to their partner."""
    decision = bpp.resolve_attribution("partner-acme", None)
    assert decision.resolved_partner_id == "partner-acme"


def test_transfer_requires_an_approving_admin_identity():
    with pytest.raises(ValueError):
        bpp.transfer_customer_attribution("partner-acme", "partner-beta", approved_by="", reason="customer requested")


def test_transfer_requires_an_explicit_reason():
    with pytest.raises(ValueError):
        bpp.transfer_customer_attribution("partner-acme", "partner-beta", approved_by="admin-jane", reason="")


def test_explicit_admin_transfer_moves_an_established_customer_between_partners():
    """The ONLY path that can move a protected, established customer --
    an explicit admin action, never a token."""
    decision = bpp.transfer_customer_attribution("partner-acme", "partner-beta", approved_by="admin-jane", reason="customer requested new installer")
    assert decision.resolved_partner_id == "partner-beta"
    assert decision.previous_partner_id == "partner-acme"
    assert "admin-jane" in decision.reason
    assert "customer requested new installer" in decision.reason


def test_attribution_decision_always_carries_a_reason_for_audit_history():
    """Every resolution -- not just transfers -- produces an
    inspectable reason string, the basis of a real audit trail."""
    for decision in (
        bpp.resolve_attribution(None, None),
        bpp.resolve_attribution("partner-acme", None),
        bpp.transfer_customer_attribution("partner-acme", "partner-beta", "admin-jane", "reason"),
    ):
        assert isinstance(decision.reason, str) and len(decision.reason) > 0
        assert decision.decided_at  # timestamped


# ============================================================ 21. discounts and promotions: centrally controlled, explicit economic treatment


def test_ordinary_partner_quote_still_cannot_go_below_base_price_at_all():
    """Reconfirms (post-attribution/promotion additions) that a partner
    calling the ordinary, non-promotional path has zero way to create
    a below-base discount -- centralized admin control is the only
    door, and it isn't this one."""
    with pytest.raises(bpp.BasePriceError):
        bpp.calculate_customer_quote("motion", "2mp", customer_price=1.00, retention_days="2", partner_id="partner-acme")


def test_promotional_quote_without_a_promotion_object_still_blocks_below_base():
    with pytest.raises(bpp.BasePriceError):
        bpp.calculate_promotional_quote("motion", "2mp", customer_price=1.00, retention_days="2")


def test_promotional_quote_with_an_unapproved_promotion_still_blocks_below_base():
    promo = bpp.Promotion(promotion_id="promo-1", absorbed_by="anyaicam", admin_approved=False, created_by="staff-jane")
    with pytest.raises(bpp.BasePriceError):
        bpp.calculate_promotional_quote("motion", "2mp", customer_price=1.00, retention_days="2", promotion=promo)


def test_promotion_rejects_an_unknown_absorbed_by_value():
    with pytest.raises(ValueError):
        bpp.Promotion(promotion_id="promo-bad", absorbed_by="nobody", admin_approved=True, created_by="staff-jane")


def test_shared_promotion_requires_a_valid_partner_share():
    with pytest.raises(ValueError):
        bpp.Promotion(promotion_id="promo-bad", absorbed_by="shared", admin_approved=True, created_by="staff-jane")  # missing partner_share
    with pytest.raises(ValueError):
        bpp.Promotion(promotion_id="promo-bad2", absorbed_by="shared", admin_approved=True, created_by="staff-jane", partner_share=1.5)


def test_approved_promotion_absorbed_by_anyaicam_reduces_anyaicam_net_not_partner_earnings():
    base = bpp.get_base_price("motion", "2mp", "2")  # $2.00
    promo = bpp.Promotion(promotion_id="promo-launch", absorbed_by="anyaicam", admin_approved=True, created_by="staff-jane")
    quote = bpp.calculate_promotional_quote("motion", "2mp", customer_price=1.00, retention_days="2", partner_id="partner-acme", promotion=promo)
    assert quote.anyaicam_net == 1.00  # AnyAiCam receives less than its normal base price
    assert quote.subsidized_amount == round(base - 1.00, 2)
    assert quote.absorbed_by == "anyaicam"
    assert quote.partner_earnings == 0.0  # partner is not penalized for an AnyAiCam-funded promotion


def test_approved_promotion_absorbed_by_partner_keeps_anyaicam_net_at_full_base_price():
    base = bpp.get_base_price("motion", "2mp", "2")
    promo = bpp.Promotion(promotion_id="promo-partner-funded", absorbed_by="partner", admin_approved=True, created_by="staff-jane")
    quote = bpp.calculate_promotional_quote("motion", "2mp", customer_price=1.00, retention_days="2", partner_id="partner-acme", promotion=promo)
    assert quote.anyaicam_net == base  # AnyAiCam is made whole regardless
    assert quote.partner_earnings < 0  # the partner absorbs the shortfall
    assert quote.subsidized_amount == round(base - 1.00, 2)
    assert quote.absorbed_by == "partner"


def test_approved_promotion_shared_splits_the_shortfall_by_partner_share():
    base = bpp.get_base_price("motion", "2mp", "2")  # 2.00
    customer_price = 1.00
    shortfall = round(base - customer_price, 2)  # 1.00
    promo = bpp.Promotion(promotion_id="promo-shared", absorbed_by="shared", admin_approved=True, created_by="staff-jane", partner_share=0.25)
    quote = bpp.calculate_promotional_quote("motion", "2mp", customer_price=customer_price, retention_days="2", partner_id="partner-acme", promotion=promo)
    partner_shortfall = round(shortfall * 0.25, 2)
    assert quote.partner_earnings == round(-partner_shortfall, 2)
    assert quote.anyaicam_net == round(base - (shortfall - partner_shortfall), 2)
    assert quote.absorbed_by == "shared"


def test_promotional_quote_at_or_above_base_never_subsidizes_anything_even_with_a_promotion_attached():
    """A promotion object being present doesn't force a subsidy -- if
    the customer price already clears base, nothing is subsidized."""
    promo = bpp.Promotion(promotion_id="promo-unused", absorbed_by="anyaicam", admin_approved=True, created_by="staff-jane")
    quote = bpp.calculate_promotional_quote("motion", "2mp", customer_price=8.99, retention_days="2", partner_id="partner-acme", promotion=promo)
    assert quote.subsidized_amount == 0.0
    assert quote.absorbed_by is None
    assert quote.anyaicam_net == quote.base_price


def test_economic_treatment_is_stored_explicitly_not_inferred_from_retail_price():
    """Two promotions with the identical discounted customer_price but
    different absorbed_by produce different anyaicam_net/partner_earnings
    -- proving the treatment comes from the stored Promotion field, not
    from any formula involving standard_retail_price."""
    promo_a = bpp.Promotion(promotion_id="a", absorbed_by="anyaicam", admin_approved=True, created_by="staff-jane")
    promo_b = bpp.Promotion(promotion_id="b", absorbed_by="partner", admin_approved=True, created_by="staff-jane")
    quote_a = bpp.calculate_promotional_quote("motion", "2mp", customer_price=1.00, retention_days="2", promotion=promo_a)
    quote_b = bpp.calculate_promotional_quote("motion", "2mp", customer_price=1.00, retention_days="2", promotion=promo_b)
    assert quote_a.customer_price == quote_b.customer_price == 1.00
    assert quote_a.anyaicam_net != quote_b.anyaicam_net
    assert quote_a.partner_earnings != quote_b.partner_earnings


def test_service_charges_remain_untouched_by_promotion_logic():
    """Confirms promotions apply only to subscription pricing --
    ServiceCharge has no admin_approved/absorbed_by concept at all,
    consistent with services being a wholly separate line item."""
    assert not hasattr(bpp.ServiceCharge, "admin_approved")
    assert not hasattr(bpp.ServiceCharge, "absorbed_by")
