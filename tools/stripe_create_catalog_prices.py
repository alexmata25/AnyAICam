"""Create the Stripe TEST-mode Products, Prices and Friends & Family coupons
for the approved catalog (app/pricing_catalog.py), without touching any
existing Price: Stripe Price amounts are immutable, so new Price objects
are created and old ones are left as they are.

Idempotent: each Price carries a lookup_key derived from the catalog key
and amount, so a rerun finds and reuses it instead of creating a duplicate.
Coupons use fixed ids. Prints `ENV_VAR=value` lines (Price/coupon ids are
not secrets) for the portal's environment. The secret key is read from
STRIPE_SECRET_KEY and is never printed. A live-mode key is refused: this
script is for test mode only until a production environment exists.

Usage (on the staging host, inside the portal container):
    python tools/stripe_create_catalog_prices.py            # dry run: list what would exist
    python tools/stripe_create_catalog_prices.py --apply    # create missing objects
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import pricing_catalog as pc  # noqa: E402

API = "https://api.stripe.com"


def catalog_prices() -> list[dict]:
    """Every Price the catalog needs: (env var, lookup key, product name,
    amount in cents, recurring interval or None for one-time)."""
    items = []
    for plan in pc.base_plans():
        items.append({"env": plan["price_env_var"], "lookup_key": f"anyaicam_{plan['plan_type']}_{plan['camera_slot_maximum']}_{plan['monthly_cents']}",
                      "name": f"AnyAiCam {plan['label']}", "amount": plan["monthly_cents"], "interval": "month"})
    for addon in pc.addons():
        if addon["monthly_cents"] is None:
            continue  # Cloud Overflow: no approved price -- never invented
        items.append({"env": addon["price_env_var"], "lookup_key": f"anyaicam_{addon['addon_key']}_{addon['monthly_cents']}",
                      "name": f"AnyAiCam {addon['label']}", "amount": addon["monthly_cents"], "interval": "month"})
    for tier in pc.face_access_tiers():
        items.append({"env": tier["price_env_var"], "lookup_key": f"anyaicam_face_access_{tier['size']}_{tier['monthly_cents_per_door']}",
                      "name": f"AnyAiCam {tier['label']} (per door)", "amount": tier["monthly_cents_per_door"], "interval": "month"})
    for lic in pc.vms_licenses():
        items.append({"env": lic["price_env_var"], "lookup_key": f"anyaicam_vms_license_{lic['capacity']}_{lic['one_time_cents']}",
                      "name": lic["label"], "amount": lic["one_time_cents"], "interval": None})
    return items


def catalog_coupons() -> list[dict]:
    return [
        {"env": pc.FRIENDS_FAMILY_COUPON_ENV[cls], "id": f"anyaicam_friends_family_{cls}_{pc.FRIENDS_FAMILY_PERCENT_OFF[cls]}",
         "percent_off": pc.FRIENDS_FAMILY_PERCENT_OFF[cls], "name": f"Friends & Family ({cls})"}
        for cls in ("base", "analytics")
    ]


def _call(key: str, method: str, path: str, fields: dict | None = None) -> dict:
    data = urllib.parse.urlencode(fields or {}).encode() if method == "POST" else None
    url = API + path + ("?" + urllib.parse.urlencode(fields) if method == "GET" and fields else "")
    request = urllib.request.Request(url, data=data, method=method, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        body = json.loads(error.read() or b"{}")
        if error.code == 404:
            return {"_missing": True}
        raise RuntimeError(f"Stripe {method} {path}: {body.get('error', {}).get('message', error.code)}") from None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="create missing objects (default: dry run)")
    args = parser.parse_args()
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        print("STRIPE_SECRET_KEY is not set.", file=sys.stderr)
        return 2
    if not key.startswith(("sk_test_", "rk_test_")):
        print("Refusing: this script only runs with a Stripe TEST-mode key.", file=sys.stderr)
        return 2
    for item in catalog_prices():
        found = _call(key, "GET", "/v1/prices", {"lookup_keys[]": item["lookup_key"], "limit": 1}).get("data") or []
        if found:
            print(f"{item['env']}={found[0]['id']}")
            continue
        if not args.apply:
            print(f"# would create {item['lookup_key']} ({item['amount']} cents, {item['interval'] or 'one-time'})")
            continue
        product = _call(key, "POST", "/v1/products", {"name": item["name"], "metadata[anyaicam_lookup_key]": item["lookup_key"]})
        fields = {"product": product["id"], "currency": "usd", "unit_amount": item["amount"], "lookup_key": item["lookup_key"]}
        if item["interval"]:
            fields["recurring[interval]"] = item["interval"]
        price = _call(key, "POST", "/v1/prices", fields)
        print(f"{item['env']}={price['id']}")
    for coupon in catalog_coupons():
        existing = _call(key, "GET", f"/v1/coupons/{coupon['id']}")
        if existing.get("_missing"):
            if not args.apply:
                print(f"# would create coupon {coupon['id']}")
                continue
            _call(key, "POST", "/v1/coupons", {"id": coupon["id"], "percent_off": coupon["percent_off"],
                                               "duration": "forever", "name": coupon["name"]})
        print(f"{coupon['env']}={coupon['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
