"""Own-PC Build Your System orders placed BEFORE the owner billing rule of 2026-10-22 (test helper, not product code).

Until then an own-PC order was ONE Stripe Checkout in mode=subscription: the per-camera plan started at checkout, plus
the one-time VMS licence as a one-time line on the first invoice. Such orders still exist (TEST-mode accounts) and their
webhooks can still be replayed, so the suites that prove their refund / dispute / R7 / C5 / race handling keep running
on exactly that shape: this is build_orders._checkout_fields as of e69106cd, verbatim, swapped in by the
legacy_own_pc_checkout fixture (conftest). The licence-only order of the new rule is covered by
test_activation_billing_127.py.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException

import per_camera_billing as billing
from build_orders import APPLIANCE_ORDER, OWN_PC_ORDER, _configured, _hardware_catalog, order_quote, vms_license_for

logger = logging.getLogger("anyaicam.build_orders")


def legacy_checkout_fields(customer_id: str, email: str | None, progress: dict) -> tuple[list, str, int]:
    import main
    import friends_family
    import checkout_guard
    plan_key = progress["plan"]
    quote = order_quote(progress)
    cameras = quote["cameras"]
    base = main.PUBLIC_BASE_URL
    if not base:
        raise HTTPException(status_code=503, detail="ANYAICAM_PUBLIC_URL is required for Stripe Checkout.")
    common = [
        ("success_url", f"{base}{billing.ORDER_COMPLETE_PATH}?session_id={{CHECKOUT_SESSION_ID}}"),
        ("cancel_url", f"{base}{billing.ORDER_SUMMARY_PATH}?checkout=cancelled"),
        ("client_reference_id", customer_id),
        ("metadata[anyaicam_customer_id]", customer_id),
        ("metadata[anyaicam_build_order]", quote["kind"]),
        ("metadata[anyaicam_plan_key]", plan_key),
        ("metadata[anyaicam_camera_quantity]", str(cameras)),
    ]
    if quote["kind"] == APPLIANCE_ORDER:
        # F2 (2026-10-21): create_checkout() no longer opens this kind (no shipping address was collected). Kept as
        # the exact shape of appliance orders paid before then, which the webhook below still records and starts.
        catalog = _hardware_catalog()
        fields = [("mode", "payment"), *common,
                  # Saves the card on the account's Stripe customer for the
                  # storage plan that starts at activation.
                  ("payment_intent_data[setup_future_usage]", "off_session"),
                  ("payment_intent_data[metadata][anyaicam_customer_id]", customer_id),
                  ("payment_intent_data[metadata][anyaicam_build_order]", APPLIANCE_ORDER)]
        items, prices = [], []
        for index, line in enumerate(quote["one_time"]):
            entry = catalog[line["sku"]]
            price_id = _configured(entry["env"], entry["name"])
            main.require_stripe_price_matches_catalog(price_id, entry["cents"], None)
            fields += [(f"line_items[{index}][price]", price_id), (f"line_items[{index}][quantity]", str(line["quantity"]))]
            items.append(f"{line['sku']}:{line['quantity']}")
            prices.append(price_id)
        fields += [("metadata[anyaicam_hardware_items]", ",".join(items)),
                   ("allow_promotion_codes", "false")]  # as the existing hardware checkout
        primary_price, primary_quantity = prices[0], 1
    else:
        plan = billing.PLANS[plan_key]
        plan_price = billing.price_id_for(plan_key)
        if not plan_price:
            raise HTTPException(status_code=503, detail=f"No Stripe Price is configured for {plan_key}.")
        main.require_stripe_price_matches_catalog(plan_price, plan["monthly_cents_per_camera"], "month")
        fields = [("mode", "subscription"), *common,
                  ("line_items[0][price]", plan_price), ("line_items[0][quantity]", str(cameras)),
                  ("metadata[anyaicam_billing_version]", "2"), ("metadata[anyaicam_stripe_price_id]", plan_price),
                  ("subscription_data[metadata][anyaicam_customer_id]", customer_id),
                  ("subscription_data[metadata][anyaicam_stripe_price_id]", plan_price),
                  ("subscription_data[metadata][anyaicam_billing_version]", "2"),
                  ("subscription_data[metadata][anyaicam_plan_key]", plan_key),
                  ("subscription_data[metadata][anyaicam_camera_quantity]", str(cameras))]
        if quote["kind"] == OWN_PC_ORDER:
            license_line = quote["one_time"][0]
            license_price = _configured(vms_license_for(cameras)["env"], "the VMS software license")
            main.require_stripe_price_matches_catalog(license_price, license_line["cents"], None)
            # A one-time price in a subscription Checkout is charged once, on the first invoice.
            fields += [("line_items[1][price]", license_price), ("line_items[1][quantity]", "1"),
                       ("metadata[anyaicam_vms_license_price_id]", license_price),
                       ("metadata[anyaicam_vms_license_capacity]", str(license_line["capacity"]))]
            # The one-time license is never discounted (Friends & Family: 50%
            # of the recurring plan only). A session-level coupon would cover
            # every line, so the coupon must be verified on Stripe as limited
            # to the plan's Product; otherwise checkout is refused (fail
            # closed). Promotion codes cannot be scoped the same way, so this
            # mixed order does not accept them.
            discount = friends_family.checkout_discount(customer_id, "base")
            if discount["coupon"]:
                try:
                    friends_family.verify_coupon_scope(discount["coupon"], eligible_price_ids=[plan_price],
                                                       excluded_price_ids=[license_price])
                except friends_family.CouponScopeError as error:
                    logger.error("build_order.friends_family_coupon_scope customer_id=%s reason=%s", customer_id, error)
                    raise HTTPException(status_code=503, detail="Your Friends & Family pricing can't be applied to this order "
                                                                "safely right now, so checkout is paused. Please contact AnyAiCam support.")
                fields.append(("discounts[0][coupon]", discount["coupon"]))
            else:
                fields.append(("allow_promotion_codes", "false"))
            fields.append(("metadata[anyaicam_friends_family]", "approved" if discount["friends_family"] else "none"))
        else:
            friends_family.apply_to_checkout_fields(fields, customer_id, "base")
            # 30-day free storage trial (storage_trial.py): storage-only
            # subscription Checkout; nothing is added without an active trial.
            import storage_trial
            fields += storage_trial.subscription_trial_fields(customer_id, checkout=True, plan_key=plan_key)
        primary_price, primary_quantity = plan_price, cameras
    fields.append(("customer", checkout_guard.canonical_stripe_customer(customer_id, email=email)))
    return fields, primary_price, primary_quantity
