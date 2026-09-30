"""The one authoritative AnyAiCam price, package, discount and commission
catalog (approved working pricing, 2026-09-30).

Every customer-visible price, every entitlement a purchase grants, the
Friends & Family discount rules and the salesperson commission amounts
live here and nowhere else. customer_entitlements.py (camera-slot tiers),
analytics_entitlements.py (analytics packages / Talk Down / Face Access),
friends_family.py, sales_commissions.py and GET /api/pricing/catalog all
read from this module; the storefront and website pages render its JSON.

Prices are integer cents so no float rounding can ever reach Stripe or a
commission record. Stripe Price IDs are NOT prices: each item names the
env var holding its Stripe Price ID, and an item whose Price ID is unset
is simply not purchasable (fail closed) -- it is never priced by guess.

Not in this module on purpose:
- pricing_config.py: the older per-camera Videoloft cloud pricing, which
  is a separate product line and is left unchanged.
- hardware_orders.HARDWARE_CATALOG: the approved appliance prices stay
  there (already authoritative and Stripe-wired); this module only adds
  the per-SKU salesperson hardware commission alongside it.
"""
from __future__ import annotations

import os
from typing import Optional


def _cents(dollars: str) -> int:
    whole, _, frac = dollars.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


def dollars(cents: int) -> float:
    return round(cents / 100, 2)


# --------------------------------------------------------------------------
# 1-2. Base VMS plans: Local and Hybrid, monthly, fixed camera-slot tiers.
# Camera counts are licensing limits only -- never a software assumption.
# --------------------------------------------------------------------------
# plan_type, tier_label, min_cameras, max_cameras (= licensed slots),
# monthly price (cents), Stripe Price ID env var
BASE_PLANS = [
    ("local", "1-8", 1, 8, _cents("14.99"), "ANYAICAM_STRIPE_PRICE_LOCAL_1_8"),
    ("local", "9-16", 9, 16, _cents("24.99"), "ANYAICAM_STRIPE_PRICE_LOCAL_9_16"),
    ("local", "17-32", 17, 32, _cents("39.99"), "ANYAICAM_STRIPE_PRICE_LOCAL_17_32"),
    ("local", "33-64", 33, 64, _cents("69.99"), "ANYAICAM_STRIPE_PRICE_LOCAL_33_64"),
    ("hybrid", "1-8", 1, 8, _cents("24.99"), "ANYAICAM_STRIPE_PRICE_HYBRID_1_8"),
    ("hybrid", "9-16", 9, 16, _cents("39.99"), "ANYAICAM_STRIPE_PRICE_HYBRID_9_16"),
    ("hybrid", "17-32", 17, 32, _cents("69.99"), "ANYAICAM_STRIPE_PRICE_HYBRID_17_32"),
    ("hybrid", "33-64", 33, 64, _cents("99.99"), "ANYAICAM_STRIPE_PRICE_HYBRID_33_64"),
]
PLAN_TYPE_LABELS = {"local": "Local", "hybrid": "Hybrid"}

# --------------------------------------------------------------------------
# One-time AnyAiCam VMS software license (approved 2026-09-30), sized to the
# camera capacity of the chosen plan. A separate one-time line item from
# the monthly Local/Hybrid plan. INCLUDED with an AnyAiCam appliance (never
# charged twice); charged for a customer-owned PC / DIY installation.
# Capacity is recorded as a customer_entitlements row (product
# "vms_license") -- licensing data, never a hard-coded VMS limit.
# --------------------------------------------------------------------------
VMS_LICENSE_PRODUCT = "vms_license"
VMS_LICENSES = [
    # camera capacity, one-time price (cents), Stripe Price ID env var
    (8, _cents("49.99"), "ANYAICAM_STRIPE_PRICE_VMS_LICENSE_8"),
    (16, _cents("79.99"), "ANYAICAM_STRIPE_PRICE_VMS_LICENSE_16"),
    (32, _cents("129.99"), "ANYAICAM_STRIPE_PRICE_VMS_LICENSE_32"),
    (64, _cents("199.99"), "ANYAICAM_STRIPE_PRICE_VMS_LICENSE_64"),
]

# --------------------------------------------------------------------------
# 3. Included in every paid Local or Hybrid plan -- never charged for.
# (feature key, label). smart_motion is also an analytics feature key
# (customer_analytics_panel.ANALYTIC_LABELS); secure_edge and aaco are
# product features that are simply on with any paid plan.
# --------------------------------------------------------------------------
INCLUDED_FEATURES = (
    ("secure_edge", "Secure Edge"),
    ("smart_motion", "Smart Motion"),
    ("aaco", "AACO"),
)
INCLUDED_FEATURE_KEYS = tuple(key for key, _ in INCLUDED_FEATURES)

# --------------------------------------------------------------------------
# 4-7. Add-ons. addon_key (the purchasable SKU), label, monthly price
# (cents, or None when no authoritative price exists yet), the internal
# feature keys the SKU grants, how it is priced, Stripe Price ID env var,
# discount class for Friends & Family, and whether it is sellable now.
# --------------------------------------------------------------------------
# Package contents approved 2026-09-30 (owner chose the proposed mapping).
ADDONS = [
    # addon_key, label, cents, grants, unit, env_var, discount_class, sellable_reason
    ("ai_essentials", "AI Essentials", _cents("7.99"), ("people_counting",),
     "per_account", "ANYAICAM_STRIPE_PRICE_ANALYTICS_AI_ESSENTIALS", "analytics", None),
    ("ai_professional", "AI Professional", _cents("14.99"), ("people_counting", "ppe"),
     "per_account", "ANYAICAM_STRIPE_PRICE_ANALYTICS_AI_PROFESSIONAL", "analytics", None),
    ("vehicle_intelligence", "Vehicle Intelligence", _cents("14.99"), ("lpr",),
     "per_account", "ANYAICAM_STRIPE_PRICE_ANALYTICS_VEHICLE_INTELLIGENCE", "analytics", None),
    # A FLAT bundle added to the base plan: never multiplied by camera
    # slots and never the sum of the individual packages.
    ("advanced_analytics", "Advanced Analytics", _cents("24.99"), ("people_counting", "lpr", "ppe"),
     "per_account", "ANYAICAM_STRIPE_PRICE_ADVANCED_ANALYTICS", "analytics", None),
    # Talk Down includes AAC Voice Call -- one charge, both entitlements.
    ("talk_down", "Talk Down (includes AAC Voice Call)", _cents("4.99"), ("talk_down", "voice_call"),
     "per_site", "ANYAICAM_STRIPE_PRICE_ANALYTICS_TALK_DOWN", "analytics", None),
    # No authoritative Cloud Overflow storage price exists anywhere in the
    # code or catalogs -- flagged, never invented.
    ("cloud_overflow", "Cloud Overflow", None, ("cloud_overflow",),
     "per_account", "ANYAICAM_STRIPE_PRICE_ANALYTICS_CLOUD_OVERFLOW", "analytics",
     "Cloud Overflow storage pricing has not been set."),
]
ANALYTICS_PACKAGE_KEYS = ("ai_essentials", "ai_professional", "vehicle_intelligence", "advanced_analytics")

# 6. Face Access: separate premium access-control product, priced per door
# by size. Size is decided by enrolled people count (owner decision
# 2026-09-30); the thresholds are not decided yet, so Face Access stays
# unsellable until both thresholds are configured.
FACE_ACCESS_TIERS = [
    # size, label, monthly price per door (cents), Stripe Price ID env var
    ("small", "Face Access Small", _cents("39.99"), "ANYAICAM_STRIPE_PRICE_FACE_ACCESS_SMALL"),
    ("medium", "Face Access Medium", _cents("49.99"), "ANYAICAM_STRIPE_PRICE_FACE_ACCESS_MEDIUM"),
    ("large", "Face Access Large", _cents("69.99"), "ANYAICAM_STRIPE_PRICE_FACE_ACCESS_LARGE"),
]
FACE_ACCESS_GRANTS = ("facial_recognition",)
# Enrolled-people thresholds: Small up to SMALL_MAX, Medium up to MEDIUM_MAX,
# Large above that.
FACE_ACCESS_SMALL_MAX_ENV = "ANYAICAM_FACE_ACCESS_SMALL_MAX_PEOPLE"
FACE_ACCESS_MEDIUM_MAX_ENV = "ANYAICAM_FACE_ACCESS_MEDIUM_MAX_PEOPLE"

# --------------------------------------------------------------------------
# 9. Friends & Family (approved by an administrator, never a promo code).
# --------------------------------------------------------------------------
# The one-time VMS license has no approved Friends & Family discount yet
# (class "vms_license" -> 0%); flagged for a decision, never invented.
FRIENDS_FAMILY_PERCENT_OFF = {"base": 50, "analytics": 25, "hardware": 0, "vms_license": 0}
FRIENDS_FAMILY_COUPON_ENV = {
    "base": "ANYAICAM_STRIPE_COUPON_FRIENDS_FAMILY_BASE",
    "analytics": "ANYAICAM_STRIPE_COUPON_FRIENDS_FAMILY_ANALYTICS",
}

# --------------------------------------------------------------------------
# 10-11. Salesperson commission (working structure). Never a customer
# discount; zero on any Friends & Family sale.
# --------------------------------------------------------------------------
ACTIVATION_COMMISSION_CENTS = {8: 4000, 16: 6000, 32: 9000, 64: 12500}
RECURRING_COMMISSION_PERCENT = 20
RECURRING_COMMISSION_MAX_PAID_MONTHS = 12
# Appliance SKU (hardware_orders.HARDWARE_CATALOG) -> commission cents. Any
# other SKU (e.g. the relay) and "no appliance" earn nothing.
HARDWARE_COMMISSION_CENTS = {
    "AIC-APPLIANCE-RYZEN-STARTER": 5000,
    "AIC-APPLIANCE-RYZEN-ENTERPRISE": 7500,
    "AIC-APPLIANCE-RYZEN-AAC-FACIAL": 10000,
}


# ------------------------------------------------------------------ lookups

def _price_id(env_var: str) -> Optional[str]:
    return os.environ.get(env_var, "").strip() or None


def base_plans() -> list[dict]:
    return [
        {
            "plan_type": plan_type, "tier_label": tier_label,
            "label": f"{PLAN_TYPE_LABELS[plan_type]} {max_cameras} cameras",
            "min_cameras": min_cameras, "max_cameras": max_cameras,
            "camera_slot_maximum": max_cameras, "monthly_cents": cents,
            "product": f"camera_slots_{plan_type}", "billing_type": "recurring",
            "price_env_var": env_var, "stripe_price_id": _price_id(env_var),
        }
        for plan_type, tier_label, min_cameras, max_cameras, cents, env_var in BASE_PLANS
    ]


def find_base_plan(plan_type: str, tier_label: str) -> Optional[dict]:
    return next((p for p in base_plans() if p["plan_type"] == plan_type and p["tier_label"] == tier_label), None)


def vms_licenses() -> list[dict]:
    return [
        {"capacity": capacity, "label": f"AnyAiCam VMS software license, {capacity} cameras", "one_time_cents": cents,
         "billing_type": "one_time", "product": VMS_LICENSE_PRODUCT, "price_env_var": env_var,
         "stripe_price_id": _price_id(env_var)}
        for capacity, cents, env_var in VMS_LICENSES
    ]


def find_vms_license(capacity: int) -> Optional[dict]:
    return next((lic for lic in vms_licenses() if lic["capacity"] == int(capacity)), None)


def addons() -> list[dict]:
    return [
        {
            "addon_key": key, "label": label, "monthly_cents": cents, "grants": grants,
            "unit": unit, "price_env_var": env_var, "discount_class": discount_class,
            "sellable": cents is not None and not reason, "unavailable_reason": reason,
            "stripe_price_id": _price_id(env_var),
        }
        for key, label, cents, grants, unit, env_var, discount_class, reason in ADDONS
    ]


def find_addon(addon_key: str) -> Optional[dict]:
    return next((a for a in addons() if a["addon_key"] == addon_key), None)


def face_access_thresholds() -> Optional[tuple[int, int]]:
    try:
        small = int(os.environ.get(FACE_ACCESS_SMALL_MAX_ENV, "").strip())
        medium = int(os.environ.get(FACE_ACCESS_MEDIUM_MAX_ENV, "").strip())
    except ValueError:
        return None
    return (small, medium) if 0 < small < medium else None


def face_access_size_for(enrolled_people: int) -> Optional[str]:
    """small/medium/large for this many enrolled people, or None while the
    thresholds are not configured (Face Access is then not sellable)."""
    thresholds = face_access_thresholds()
    if thresholds is None:
        return None
    small, medium = thresholds
    if enrolled_people <= small:
        return "small"
    return "medium" if enrolled_people <= medium else "large"


def face_access_tiers() -> list[dict]:
    sellable = face_access_thresholds() is not None
    return [
        {"size": size, "label": label, "monthly_cents_per_door": cents, "grants": FACE_ACCESS_GRANTS,
         "price_env_var": env_var, "stripe_price_id": _price_id(env_var), "sellable": sellable,
         "unavailable_reason": None if sellable else "Face Access size thresholds (enrolled people) have not been set."}
        for size, label, cents, env_var in FACE_ACCESS_TIERS
    ]


def all_granted_feature_keys() -> tuple[str, ...]:
    keys = {k for a in ADDONS for k in a[3]} | set(FACE_ACCESS_GRANTS)
    return tuple(sorted(keys))


# ------------------------------------------------------------------ quoting

def friends_family_percent(discount_class: str) -> int:
    return FRIENDS_FAMILY_PERCENT_OFF.get(discount_class, 0)


def discounted_cents(cents: int, percent_off: int) -> int:
    """Stripe percent-off coupons round the discount to the nearest cent;
    mirror that so the quote matches what Stripe charges."""
    return cents - int(round(cents * percent_off / 100))


def quote(*, plan_type: str, tier_label: str, addon_keys=(), talk_down_sites: int = 0,
          friends_family_approved: bool = False, with_appliance: Optional[bool] = None) -> dict:
    """Monthly subscription quote for one base plan plus flat add-ons, and
    (when with_appliance is given) the one-time VMS license for the plan's
    capacity: $0 "included" with an AnyAiCam appliance, charged for a DIY
    installation. Advanced Analytics (and every package) is flat -- never
    multiplied by camera slots. Talk Down is per site. Hardware is quoted
    separately and never discounted."""
    plan = find_base_plan(plan_type, tier_label)
    if plan is None:
        raise ValueError("Unknown base plan.")
    lines = []

    def add(label, cents, discount_class, quantity=1):
        percent = friends_family_percent(discount_class) if friends_family_approved else 0
        unit = discounted_cents(cents, percent)
        lines.append({"label": label, "quantity": quantity, "list_cents": cents * quantity,
                      "percent_off": percent, "cents": unit * quantity})

    add(plan["label"], plan["monthly_cents"], "base")
    for key in dict.fromkeys(addon_keys):
        if key == "talk_down":
            continue
        addon = find_addon(key)
        if addon is None or not addon["sellable"]:
            raise ValueError(f"Add-on not available: {key}")
        add(addon["label"], addon["monthly_cents"], addon["discount_class"])
    if talk_down_sites:
        talk_down = find_addon("talk_down")
        add(talk_down["label"], talk_down["monthly_cents"], "analytics", quantity=int(talk_down_sites))
    one_time = []
    if with_appliance is not None:
        license_ = find_vms_license(plan["camera_slot_maximum"])
        if with_appliance:
            one_time.append({"label": license_["label"] + " (included with appliance)", "cents": 0, "included": True})
        else:
            percent = friends_family_percent("vms_license") if friends_family_approved else 0
            one_time.append({"label": license_["label"], "cents": discounted_cents(license_["one_time_cents"], percent),
                             "included": False})
    return {
        "lines": lines,
        "monthly_list_cents": sum(line["list_cents"] for line in lines),
        "monthly_cents": sum(line["cents"] for line in lines),
        "one_time_lines": one_time,
        "one_time_cents": sum(line["cents"] for line in one_time),
        "friends_family_approved": friends_family_approved,
    }


def public_catalog() -> dict:
    """Customer-safe catalog JSON (no Stripe IDs, no commission data)."""
    return {
        "currency": "USD",
        "base_plans": [
            {k: p[k] for k in ("plan_type", "tier_label", "label", "min_cameras", "max_cameras")}
            | {"monthly": dollars(p["monthly_cents"])}
            for p in base_plans()
        ],
        "included_features": [{"key": k, "label": label} for k, label in INCLUDED_FEATURES],
        "vms_licenses": [
            {"capacity": lic["capacity"], "label": lic["label"], "one_time": dollars(lic["one_time_cents"]),
             "included_with_appliance": True}
            for lic in vms_licenses()
        ],
        "addons": [
            {"key": a["addon_key"], "label": a["label"], "unit": a["unit"], "grants": list(a["grants"]),
             "monthly": dollars(a["monthly_cents"]) if a["monthly_cents"] is not None else None,
             "sellable": a["sellable"], "unavailable_reason": a["unavailable_reason"]}
            for a in addons()
        ],
        "face_access": [
            {"size": t["size"], "label": t["label"], "monthly_per_door": dollars(t["monthly_cents_per_door"]),
             "sellable": t["sellable"], "unavailable_reason": t["unavailable_reason"]}
            for t in face_access_tiers()
        ],
        "friends_family": {"percent_off": dict(FRIENDS_FAMILY_PERCENT_OFF), "requires_administrator_approval": True},
    }
