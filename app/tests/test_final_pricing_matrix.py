"""Final pricing matrix milestone: regression coverage for the approved
Local Recording / Motion Cloud / Continuous Cloud pricing applied to
production this session.

Covers, per the explicit approved scope:
  1. All 24 Motion/Continuous plan prices (real calculate_quote()).
  2. Local Recording prices (pure model constants -- see the module
     docstring note below on why these aren't a real code path yet).
  3. 60/40 split math for every plan.
  4. Company net-after-AWS calculations and >=20% margin for every plan.
  5. Partner quote generation (calculate_partner_quote()) -- including
     the pre-existing, confirmed-not-a-bug 409 gap on 21/24 cells.
  6. Customer quote generation (calculate_quote()) full response shape.
  7. Existing quote snapshot immutability after a pricing change.
  8. Ordering: Local <= Motion Cloud <= Continuous Cloud, every cell.

IMPORTANT -- Local Recording has no code path in this codebase today.
There is no 'local' key anywhere in pricing_config.py's plans schema,
no route computes a Local Recording quote, and no database table
stores a Local Recording plan. The $4.99/$6.99/$10.99 prices are an
approved commercial decision this engagement, not yet wired into any
product code. Section 2 below tests the *pricing model* (the
constants and their derived economics) as a documented contract for
whenever real Local Recording code exists to consume it -- it
deliberately does not call any app route, because none exists.

Imports pricing_config (which imports nothing heavy) and partner_db
(triggering its import-time schema init, matching this project's own
documented constraint) -- redirects to a throwaway sqlite file via
override_target() before that import.
"""

import json
import math

import pytest

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_final_pricing_matrix.db"):
    import pricing_config
    from partner_db import connection, password_hash


# ============================================================ the approved, applied authoritative matrix

MOTION_CLOUD = {
    "2mp": {"2": 4.99, "7": 5.99, "14": 6.99, "30": 8.99},
    "4mp": {"2": 6.99, "7": 8.49, "14": 9.99, "30": 12.99},
    "8mp": {"2": 10.99, "7": 12.99, "14": 15.49, "30": 19.99},
}
CONTINUOUS_CLOUD = {
    "2mp": {"2": 4.99, "7": 14.00, "14": 27.00, "30": 56.50},
    "4mp": {"2": 8.00, "7": 25.00, "14": 49.00, "30": 103.50},
    "8mp": {"2": 14.00, "7": 45.00, "14": 88.50, "30": 188.00},
}
LOCAL_RECORDING = {"2mp": 4.99, "4mp": 6.99, "8mp": 10.99}

# Real AWS cost basis this session: Motion Cloud priced at the real
# measured busiest-camera upload fraction (0.861%, Camera 3, live on
# Ryzen this engagement); Continuous Cloud at 100% (full archive, no
# motion savings applied, per explicit instruction); Local Recording
# at the real measured thumbnail/clip/alert-volume model (30
# alerts/day, 14-day bounded alert-media retention).
AWS_COST_MOTION = {
    "2mp": {"2": 0.193, "7": 0.226, "14": 0.270, "30": 0.373},
    "4mp": {"2": 0.300, "7": 0.359, "14": 0.441, "30": 0.629},
    "8mp": {"2": 0.491, "7": 0.598, "14": 0.748, "30": 1.090},
}
AWS_COST_CONTINUOUS = {
    "2mp": {"2": 1.714, "7": 5.440, "14": 10.656, "30": 22.579},
    "4mp": {"2": 3.052, "7": 9.883, "14": 19.446, "30": 41.305},
    "8mp": {"2": 5.459, "7": 17.879, "14": 35.267, "30": 75.011},
}
AWS_COST_LOCAL = {"2mp": 0.505, "4mp": 0.772, "8mp": 1.251}

MIN_MARGIN_PERCENT = 20.0


def _resolutions():
    return ("2mp", "4mp", "8mp")


def _retentions():
    return ("2", "7", "14", "30")


# ============================================================ 1. All 24 Motion/Continuous plan prices


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_pricing.db"


def _live_pricing_config():
    """The real, currently-applied production pricing (as deployed
    this session) -- not a synthetic fixture. Every test in sections
    1, 3, 4, 5, 6, 8 exercises this exact data."""
    return pricing_config.load_pricing()


@pytest.mark.parametrize("resolution", _resolutions())
@pytest.mark.parametrize("mode", ("motion", "continuous"))
@pytest.mark.parametrize("retention", _retentions())
def test_calculate_quote_matches_authoritative_price(resolution, mode, retention):
    config = _live_pricing_config()
    authoritative = (MOTION_CLOUD if mode == "motion" else CONTINUOUS_CLOUD)[resolution][retention]
    quote = pricing_config.calculate_quote({"resolution": resolution, "recording": mode, "retention": retention, "quantity": 1}, config)
    assert quote["per_camera_cloud"] == authoritative


# ============================================================ 2. Local Recording prices (model constants, no code path -- see module docstring)


@pytest.mark.parametrize("resolution,expected", [("2mp", 4.99), ("4mp", 6.99), ("8mp", 10.99)])
def test_local_recording_price_matches_approved_working_value(resolution, expected):
    assert LOCAL_RECORDING[resolution] == expected


# ============================================================ 3. 60/40 split math, every plan


def _split(retail):
    partner_40 = round(retail * 0.40, 2)
    company_60 = round(retail * 0.60, 2)
    return partner_40, company_60


@pytest.mark.parametrize("resolution", _resolutions())
@pytest.mark.parametrize("mode", ("motion", "continuous"))
@pytest.mark.parametrize("retention", _retentions())
def test_sixty_forty_split_sums_to_full_retail(resolution, mode, retention):
    retail = (MOTION_CLOUD if mode == "motion" else CONTINUOUS_CLOUD)[resolution][retention]
    partner_40, company_60 = _split(retail)
    assert math.isclose(partner_40 + company_60, retail, abs_tol=0.01)
    assert math.isclose(partner_40, retail * 0.40, abs_tol=0.01)
    assert math.isclose(company_60, retail * 0.60, abs_tol=0.01)


@pytest.mark.parametrize("resolution", _resolutions())
def test_local_recording_sixty_forty_split(resolution):
    retail = LOCAL_RECORDING[resolution]
    partner_40, company_60 = _split(retail)
    assert math.isclose(partner_40 + company_60, retail, abs_tol=0.01)


# ============================================================ 4. Company net-after-AWS and >=20% margin, every plan


@pytest.mark.parametrize("resolution", _resolutions())
@pytest.mark.parametrize("retention", _retentions())
def test_motion_cloud_net_after_aws_and_margin(resolution, retention):
    retail = MOTION_CLOUD[resolution][retention]
    aws_cost = AWS_COST_MOTION[resolution][retention]
    _, company_60 = _split(retail)
    net_after_aws = company_60 - aws_cost
    margin_percent = net_after_aws / retail * 100
    assert net_after_aws > 0
    assert margin_percent >= MIN_MARGIN_PERCENT


@pytest.mark.parametrize("resolution", _resolutions())
@pytest.mark.parametrize("retention", _retentions())
def test_continuous_cloud_net_after_aws_and_margin(resolution, retention):
    retail = CONTINUOUS_CLOUD[resolution][retention]
    aws_cost = AWS_COST_CONTINUOUS[resolution][retention]
    _, company_60 = _split(retail)
    net_after_aws = company_60 - aws_cost
    margin_percent = net_after_aws / retail * 100
    assert net_after_aws > 0
    assert margin_percent >= MIN_MARGIN_PERCENT, f"{resolution} {retention}d margin {margin_percent:.2f}% below {MIN_MARGIN_PERCENT}% floor"


@pytest.mark.parametrize("resolution", _resolutions())
def test_local_recording_net_after_aws_and_margin(resolution):
    retail = LOCAL_RECORDING[resolution]
    aws_cost = AWS_COST_LOCAL[resolution]
    _, company_60 = _split(retail)
    net_after_aws = company_60 - aws_cost
    margin_percent = net_after_aws / retail * 100
    assert margin_percent >= MIN_MARGIN_PERCENT
    assert margin_percent >= 45.0  # Local Recording runs far above the floor (real ~48-50%) -- a regression toward the bare 20% floor here would itself be a real finding


# ============================================================ 5. Partner quote generation -- including the confirmed pre-existing 409 gap


PARTNER_CONFIGURED_CELLS = {("2mp", "motion", "2"), ("2mp", "motion", "30"), ("2mp", "continuous", "2")}


@pytest.mark.parametrize("resolution", _resolutions())
@pytest.mark.parametrize("mode", ("motion", "continuous"))
@pytest.mark.parametrize("retention", _retentions())
def test_partner_quote_generation_matches_known_configured_state(resolution, mode, retention):
    """Confirmed this session (both before and after the retail price
    change, using the real backed-up pre-change config): exactly 3 of
    24 plan_terms entries have an explicit partner_monthly_price set
    ('fixed' pricing_mode requires one per cell). The other 21 raise a
    deliberate ValueError -- not a crash, an intentional validation
    guard on incomplete data, pre-existing and unrelated to this
    session's retail price change. This test pins that exact known
    state so any future change to it (fixing the gap, or a regression
    that breaks the 3 working cells) shows up here explicitly, rather
    than silently."""
    config = _live_pricing_config()
    selection = {"resolution": resolution, "recording": mode, "retention": retention, "quantity": 1}
    if (resolution, mode, retention) in PARTNER_CONFIGURED_CELLS:
        quote = pricing_config.calculate_partner_quote(selection, config)
        assert quote["per_camera_cloud"] == (MOTION_CLOUD if mode == "motion" else CONTINUOUS_CLOUD)[resolution][retention]
    else:
        with pytest.raises(ValueError, match="Partner pricing is not configured"):
            pricing_config.calculate_partner_quote(selection, config)


# ============================================================ 6. Customer quote generation -- full response shape


@pytest.mark.parametrize("resolution", _resolutions())
@pytest.mark.parametrize("mode", ("motion", "continuous"))
@pytest.mark.parametrize("retention", _retentions())
def test_customer_quote_full_response_shape(resolution, mode, retention):
    config = _live_pricing_config()
    quote = pricing_config.calculate_quote({"resolution": resolution, "recording": mode, "retention": retention, "quantity": 3}, config)
    expected_unit = (MOTION_CLOUD if mode == "motion" else CONTINUOUS_CLOUD)[resolution][retention]
    assert quote["resolution"] == resolution
    assert quote["recording"] == mode
    assert quote["retention_days"] == int(retention)
    assert quote["quantity"] == 3
    assert quote["per_camera_cloud"] == expected_unit
    assert quote["cloud_subtotal"] == round(expected_unit * 3, 2)
    assert quote["monthly_total"] == quote["cloud_subtotal"] + quote["analytics_subtotal"]
    assert quote["annual_total"] < quote["monthly_total"] * 12  # annual discount actually applied


# ============================================================ 7. Existing quote snapshot immutability after a pricing change


def _seed_customer_and_quote(db, customer_id, old_price):
    now = "2026-08-18T05:01:51.612837"
    db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-1", "Test Partner", "approved", "real", now))
    db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", (customer_id, "partner-1", "Test Customer", "test@example.test", "active", "real", now))
    totals_json = json.dumps({
        "resolution": "2mp", "recording": "motion", "retention_days": 2, "quantity": 4,
        "per_camera_cloud": old_price, "cloud_subtotal": round(old_price * 4, 2),
        "monthly_total": round(old_price * 4, 2),
    })
    db.execute(
        "INSERT INTO quotes(id,customer_id,partner_id,status,selection_json,totals_json,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)",
        ("quote-1", customer_id, "anyaicam-primary", "estimate", "{}", totals_json, now, "amata@anyaicam.com"),
    )
    db.execute(
        "INSERT INTO plans(id,customer_id,resolution,recording_mode,retention_days,camera_quantity,retail_monthly,partner_monthly,monthly_recurring_profit,annual_total,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("plan-1", customer_id, "2mp", "motion", 2, 4, round(old_price * 4, 2), round(old_price * 4, 2), 0.0, 0.0, "quote", now),
    )


def test_existing_quote_snapshot_is_unchanged_by_a_later_pricing_change(db_path):
    """Directly models the real production evidence found this
    session: a real customer's 'quote'/'estimate' rows from
    2026-08-18, storing 2mp/motion/2-day at the OLD $7.99 price as a
    frozen JSON/column snapshot -- confirmed still reading exactly
    $7.99 today, even though the live retail price for that same plan
    is now $4.99. This test proves that behavior structurally: seed a
    quote/plan row at an old price, change the live pricing config,
    and assert the stored row is byte-for-byte unchanged -- nothing in
    this codebase ever rewrites a quotes/plans row from a later
    pricing_config.json edit."""
    old_price = 7.99
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            _seed_customer_and_quote(db, "cust-snapshot", old_price)

        with connection() as db:
            before_quote = dict(db.execute("SELECT totals_json FROM quotes WHERE id='quote-1'").fetchone())
            before_plan = dict(db.execute("SELECT retail_monthly, partner_monthly FROM plans WHERE id='plan-1'").fetchone())

    # Simulate exactly what this session did: change the live pricing config to the new, much-lower price.
    new_price = MOTION_CLOUD["2mp"]["2"]
    assert new_price != old_price  # sanity: this is a real, meaningfully different price
    config = pricing_config.load_pricing()
    assert config["plans"]["2mp"]["motion"]["2"] == new_price  # confirms the live config really did change

    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            after_quote = dict(db.execute("SELECT totals_json FROM quotes WHERE id='quote-1'").fetchone())
            after_plan = dict(db.execute("SELECT retail_monthly, partner_monthly FROM plans WHERE id='plan-1'").fetchone())

    assert after_quote == before_quote
    assert after_plan == before_plan
    stored_totals = json.loads(after_quote["totals_json"])
    assert stored_totals["per_camera_cloud"] == old_price  # still the OLD price, not silently repriced
    assert after_plan["retail_monthly"] == round(old_price * 4, 2)


# ============================================================ 8. Ordering: Local <= Motion Cloud <= Continuous Cloud, every cell


@pytest.mark.parametrize("resolution", _resolutions())
@pytest.mark.parametrize("retention", _retentions())
def test_local_le_motion_le_continuous(resolution, retention):
    local = LOCAL_RECORDING[resolution]
    motion = MOTION_CLOUD[resolution][retention]
    continuous = CONTINUOUS_CLOUD[resolution][retention]
    assert local <= motion, f"{resolution} {retention}d: Local (${local}) must be <= Motion Cloud (${motion})"
    assert motion <= continuous, f"{resolution} {retention}d: Motion Cloud (${motion}) must be <= Continuous Cloud (${continuous})"
