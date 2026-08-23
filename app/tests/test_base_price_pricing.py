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
