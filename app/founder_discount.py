"""Server-authorized Founder testing discount for Vehicle Intelligence.

Eligibility is a server configuration keyed by immutable AnyAiCam customer
account ID. The Stripe coupon is also configured server-side and verified
against the configured LPR Price before it is attached to checkout.
"""
from __future__ import annotations

import os
from urllib.parse import quote


CUSTOMER_IDS_ENV = "ANYAICAM_FOUNDER_LPR_CUSTOMER_IDS"
COUPON_ID_ENV = "ANYAICAM_STRIPE_COUPON_FOUNDER_LPR"
DURATION_ENV = "ANYAICAM_FOUNDER_LPR_COUPON_DURATION"
DURATION_MONTHS_ENV = "ANYAICAM_FOUNDER_LPR_COUPON_DURATION_MONTHS"
LPR_ADDON_KEY = "vehicle_intelligence"


class FounderDiscountConfigurationError(RuntimeError):
    """An eligible checkout cannot safely verify its configured discount."""


def eligible_for(customer_id: str, addon_key: str) -> bool:
    """Return true only for an explicitly allowlisted account and LPR SKU."""
    if addon_key != LPR_ADDON_KEY or not customer_id:
        return False
    authorized_ids = {
        item.strip()
        for item in os.environ.get(CUSTOMER_IDS_ENV, "").split(",")
        if item.strip()
    }
    return customer_id in authorized_ids


def _configured_duration() -> tuple[str, int | None]:
    duration = os.environ.get(DURATION_ENV, "").strip().lower()
    months_raw = os.environ.get(DURATION_MONTHS_ENV, "").strip()
    # Owner-approved Founder pricing applies to the first invoice only.
    # Fail closed if configuration drifts to repeating or forever.
    if duration != "once":
        raise FounderDiscountConfigurationError(
            "Founder discount duration must be configured as once."
        )
    if months_raw:
        raise FounderDiscountConfigurationError(
            "Founder discount duration months must be unset for a once-only coupon."
        )
    return "once", None


def _stripe_id(value) -> str:
    raw = value.get("id") if isinstance(value, dict) else value
    return str(raw or "").strip()


def _verify_coupon(*, price_id: str, coupon_id: str, expected_duration: str) -> None:
    """Fail closed unless Stripe confirms a valid, exact 100% product coupon."""
    import main

    encoded_coupon = quote(coupon_id, safe="")
    encoded_price = quote(price_id, safe="")
    try:
        price = main.stripe_api_get(f"/v1/prices/{encoded_price}")
        coupon = main.stripe_api_get(f"/v1/coupons/{encoded_coupon}?expand[]=applies_to")
    except Exception as exc:
        raise FounderDiscountConfigurationError(
            "Founder discount could not be verified with Stripe."
        ) from exc

    try:
        percent = float(coupon.get("percent_off"))
    except (TypeError, ValueError):
        percent = None
    product_id = _stripe_id(price.get("product"))
    products = {
        str(item).strip()
        for item in ((coupon.get("applies_to") or {}).get("products") or [])
        if str(item).strip()
    }
    actual_duration = str(coupon.get("duration") or "").strip().lower()
    actual_months = coupon.get("duration_in_months")
    valid_months = actual_months is None
    if (
        coupon.get("valid") is not True
        or percent != 100.0
        or coupon.get("amount_off") is not None
        or actual_duration != expected_duration
        or not valid_months
        or not product_id
        or products != {product_id}
    ):
        raise FounderDiscountConfigurationError(
            "Founder Stripe coupon does not match the approved 100% LPR discount configuration."
        )


def apply_to_checkout_fields(fields: list[tuple[str, str]], *, customer_id: str,
                             addon_key: str, price_id: str) -> dict | None:
    """Attach the verified Founder coupon to the eligible LPR subscription.

    Returns its safe audit metadata, or ``None`` when this account/SKU is
    not eligible. It never accepts a coupon ID, eligibility flag, or duration
    from the browser.
    """
    if not eligible_for(customer_id, addon_key):
        return None
    coupon_id = os.environ.get(COUPON_ID_ENV, "").strip()
    if not coupon_id:
        raise FounderDiscountConfigurationError("Founder Stripe coupon is not configured.")
    duration, months = _configured_duration()
    _verify_coupon(price_id=price_id, coupon_id=coupon_id, expected_duration=duration)

    fields[:] = [
        item for item in fields
        if item[0] not in {"discounts[0][coupon]", "allow_promotion_codes"}
    ]
    fields.extend([
        ("discounts[0][coupon]", coupon_id),
        ("allow_promotion_codes", "false"),
        ("metadata[anyaicam_discount_program]", "founder_lpr_testing"),
        ("subscription_data[metadata][anyaicam_discount_program]", "founder_lpr_testing"),
        ("metadata[anyaicam_friends_family]", "none"),
    ])
    return {"program": "founder_lpr_testing", "duration": duration,
            "duration_months": months}
