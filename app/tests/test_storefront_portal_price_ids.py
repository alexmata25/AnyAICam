"""The staging storefront sells only on Prices the staging portal recognizes
(2026-10-04).

Before: storefront/staging/stripe-config.php hard-coded separate cent-priced
sandbox Prices (and some IDs from another Stripe account). The portal maps a
paid Checkout Session to an order, plan or license only through its own
ANYAICAM_STRIPE_PRICE_* values, so storefront purchases were silently ignored.

deploy/staging-stripe-test-prices.json is the record of what the staging
portal recognizes (read from the staging environment). These tests fail if
the storefront ever drifts from it, if any amount differs from the portal's
own catalog, or if an unsellable item is enabled. The PHP behaviour (Stripe
Price verification, tampering, duplicate clicks) is covered by
storefront/staging/tests/run-offline-tests.sh.
"""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STAGING = ROOT / "storefront" / "staging"
SNAPSHOT = json.loads((ROOT / "deploy" / "staging-stripe-test-prices.json").read_text(encoding="utf-8"))["prices"]
ACCOUNT_FRAGMENT = "GllhK80H2n"  # every Price of acct_1U87e9GllhK80H2n carries it

# storefront constant -> the portal's environment variable for the same item
STOREFRONT_TO_PORTAL = {
    "RYZEN_STARTER_PRICE_ID": "ANYAICAM_STRIPE_PRICE_RYZEN_STARTER",
    "RYZEN_ENTERPRISE_PRICE_ID": "ANYAICAM_STRIPE_PRICE_RYZEN_ENTERPRISE",
    "RYZEN_AAC_FACIAL_PRICE_ID": "ANYAICAM_STRIPE_PRICE_RYZEN_AAC_FACIAL",
    "RELAY_MODULE_3CH_PRICE_ID": "ANYAICAM_STRIPE_PRICE_RELAY_NUMATO_3CH",
    **{f"VMS_LICENSE_{n}_PRICE_ID": f"ANYAICAM_STRIPE_PRICE_VMS_LICENSE_{n}" for n in (8, 16, 32, 64)},
    **{f"{kind}_{band}_PRICE_ID": f"ANYAICAM_STRIPE_PRICE_{kind}_{band}"
       for kind in ("LOCAL", "HYBRID") for band in ("1_8", "9_16", "17_32", "33_64")},
    "ANALYTICS_AI_ESSENTIALS_PRICE_ID": "ANYAICAM_STRIPE_PRICE_ANALYTICS_AI_ESSENTIALS",
    "ANALYTICS_AI_PROFESSIONAL_PRICE_ID": "ANYAICAM_STRIPE_PRICE_ANALYTICS_AI_PROFESSIONAL",
    "ANALYTICS_VEHICLE_INTELLIGENCE_PRICE_ID": "ANYAICAM_STRIPE_PRICE_ANALYTICS_VEHICLE_INTELLIGENCE",
    "ANALYTICS_ADVANCED_PRICE_ID": "ANYAICAM_STRIPE_PRICE_ADVANCED_ANALYTICS",
    "ANALYTICS_TALK_DOWN_PRICE_ID": "ANYAICAM_STRIPE_PRICE_ANALYTICS_TALK_DOWN",
}


def _php_defines() -> dict:
    text = (STAGING / "stripe-config.php").read_text(encoding="utf-8")
    return dict(re.findall(r"define\('([A-Z0-9_]+_PRICE_ID)',\s*'([^']*)'\)", text))


def _portal_amounts() -> dict:
    """Env var -> (cents, interval) from the portal's own catalog code."""
    import hardware_orders
    import pricing_catalog as pc
    amounts = {var: (cents, None) for _sku, _p, _n, cents, var in hardware_orders.HARDWARE_CATALOG}
    amounts.update({p["price_env_var"]: (p["monthly_cents"], "month") for p in pc.base_plans()})
    amounts.update({a["price_env_var"]: (a["monthly_cents"], "month") for a in pc.addons() if a["monthly_cents"] is not None})
    amounts.update({lic["price_env_var"]: (lic["one_time_cents"], None) for lic in pc.vms_licenses()})
    return amounts


def test_every_storefront_price_is_the_one_the_portal_recognizes():
    defines = _php_defines()
    for constant, env_var in STOREFRONT_TO_PORTAL.items():
        assert defines.get(constant) == SNAPSHOT[env_var]["price_id"], f"{constant} != portal {env_var}"


def test_every_enabled_storefront_price_id_is_portal_recognized_and_in_this_stripe_account():
    recognized = {entry["price_id"] for entry in SNAPSHOT.values()}
    enabled = {name: value for name, value in _php_defines().items() if value}
    assert set(enabled) == set(STOREFRONT_TO_PORTAL), "an unexpected storefront Price ID is enabled"
    for name, value in enabled.items():
        assert value in recognized, f"{name}={value} is not a Price the staging portal recognizes"
        assert ACCOUNT_FRAGMENT in value, f"{name}={value} belongs to another Stripe account"


def test_unsellable_items_have_no_price():
    defines = _php_defines()
    for prefix in ("ADAPTER_", "CAMERA_", "POE_", "WEBHOOK_TEST_"):
        for name, value in defines.items():
            if name.startswith(prefix):
                assert value == "", f"{name} must stay empty (no Price in this Stripe account)"
    for retired in ("SMART_MOTION", "PEOPLE_COUNTING", "LPR", "PPE", "CLOUD_OVERFLOW", "FACIAL_RECOGNITION"):
        assert defines[f"ANALYTICS_{retired}_PRICE_ID"] == ""


def test_the_snapshot_amounts_are_the_portals_published_catalog():
    amounts = _portal_amounts()
    assert len(SNAPSHOT) == 21
    for env_var, entry in SNAPSHOT.items():
        assert (entry["amount_cents"], entry["interval"]) == amounts[env_var], env_var
        assert entry["lookup_key"].endswith(f"_{entry['amount_cents']}"), env_var  # tools/stripe_create_catalog_prices.py naming


@pytest.mark.parametrize("env_var,cents", [
    ("ANYAICAM_STRIPE_PRICE_RYZEN_STARTER", 124999), ("ANYAICAM_STRIPE_PRICE_RYZEN_ENTERPRISE", 174999),
    ("ANYAICAM_STRIPE_PRICE_RYZEN_AAC_FACIAL", 224999), ("ANYAICAM_STRIPE_PRICE_RELAY_NUMATO_3CH", 14999),
    ("ANYAICAM_STRIPE_PRICE_VMS_LICENSE_8", 4999), ("ANYAICAM_STRIPE_PRICE_VMS_LICENSE_16", 7999),
    ("ANYAICAM_STRIPE_PRICE_VMS_LICENSE_32", 12999), ("ANYAICAM_STRIPE_PRICE_VMS_LICENSE_64", 19999),
    ("ANYAICAM_STRIPE_PRICE_LOCAL_1_8", 1499), ("ANYAICAM_STRIPE_PRICE_LOCAL_9_16", 2499),
    ("ANYAICAM_STRIPE_PRICE_LOCAL_17_32", 3999), ("ANYAICAM_STRIPE_PRICE_LOCAL_33_64", 6999),
    ("ANYAICAM_STRIPE_PRICE_HYBRID_1_8", 2499), ("ANYAICAM_STRIPE_PRICE_HYBRID_9_16", 3999),
    ("ANYAICAM_STRIPE_PRICE_HYBRID_17_32", 6999), ("ANYAICAM_STRIPE_PRICE_HYBRID_33_64", 9999),
])
def test_the_launch_prices_are_unchanged(env_var, cents):
    assert SNAPSHOT[env_var]["amount_cents"] == cents


def test_storefront_charged_and_displayed_amounts_equal_the_catalog():
    catalog = (STAGING / "checkout-catalog.php").read_text(encoding="utf-8")
    for key, cents in re.findall(r"'([a-z0-9_]+|\d+)'?\s*=> \[[^\]]*?'amount_cents' => (\d+)", catalog):
        display = re.search(rf"(?:'{key}'|\n    {key})\s*=> \[[^\]]*?'display_price' => ([0-9.]+)", catalog).group(1)
        assert abs(float(display) - int(cents) / 100) < 0.001, key
    found = {k: int(c) for k, c in re.findall(r"'([a-z0-9_]+)'\s*=> \[[^\]]*?'amount_cents' => (\d+)", catalog)}
    found.update({int(k): int(c) for k, c in re.findall(r"\n    (\d+)\s*=> \[[^\]]*?'amount_cents' => (\d+)", catalog)})
    assert found == {"local_1_8": 1499, "local_9_16": 2499, "local_17_32": 3999, "local_33_64": 6999,
                     "hybrid_1_8": 2499, "hybrid_9_16": 3999, "hybrid_17_32": 6999, "hybrid_33_64": 9999,
                     "ai_essentials": 799, "ai_professional": 1499, "vehicle_intelligence": 1499,
                     "advanced_analytics": 2499, "talk_down": 499, 8: 4999, 16: 7999, 32: 12999, 64: 19999}
    hardware = (STAGING / "hardware-price-catalog.php").read_text(encoding="utf-8")
    for sku, cents in (("AIC-APPLIANCE-RYZEN-STARTER", 124999), ("AIC-APPLIANCE-RYZEN-ENTERPRISE", 174999),
                       ("AIC-APPLIANCE-RYZEN-AAC-FACIAL", 224999), ("AIC-RELAY-NUMATO-3CH", 14999)):
        assert re.search(rf"'{sku}'=>\[[^\]]*'unit_amount'=>{cents},", hardware), sku


def test_the_build_page_shows_the_published_prices():
    page = (STAGING / "build-your-system.html").read_text(encoding="utf-8")
    for sku, price in (("AIC-APPLIANCE-RYZEN-STARTER", "1249.99"), ("AIC-APPLIANCE-RYZEN-ENTERPRISE", "1749.99"),
                       ("AIC-APPLIANCE-RYZEN-AAC-FACIAL", "2249.99"), ("AIC-RELAY-NUMATO-3CH", "149.99")):
        assert re.search(rf"sku:'{sku}'[^}}]*price:{re.escape(price)}\}}", page), sku
    assert "const LOCAL_PRICES = {'1_8':14.99,'9_16':24.99,'17_32':39.99,'33_64':69.99};" in page
    assert "const HYBRID_PRICES = {'1_8':24.99,'9_16':39.99,'17_32':69.99,'33_64':99.99};" in page
    assert "const VMS_LICENSE_PRICES = {'1_8':49.99,'9_16':79.99,'17_32':129.99,'33_64':199.99};" in page
    for key, price in (("ai_essentials", "7.99"), ("ai_professional", "14.99"), ("vehicle_intelligence", "14.99"),
                       ("advanced_analytics", "24.99"), ("talk_down", "4.99")):
        assert re.search(rf"key:'{key}'[^}}]*price:{re.escape(price)}\}}", page), key
    assert not re.search(r"price:0\.\d\d\b", page) and "journey_id: JOURNEY_ID" in page


def test_the_legacy_cart_is_retired_and_old_vms_downloads_are_gone():
    legacy = (STAGING / "stripe-checkout.php").read_text(encoding="utf-8")
    assert legacy.index("json_fail('This checkout is not available. Please use Build Your System.', 410);") < legacy.index("require_once __DIR__ . '/stripe-config.php';")
    for page in ("vms.html", "vms.live.html"):
        text = (ROOT / "storefront" / page).read_text(encoding="utf-8")
        assert "0.1.3" not in text and "releases/download" not in text and ".deb" not in text and "Setup-" not in text, page
        assert 'id="vms-release-status"' in text and "f1862e" not in text, page


SITE = ROOT / "storefront"
CUSTOMER_PAGES = sorted(p for p in SITE.rglob("*.html") if "tests" not in p.parts)


def _visible(html: str) -> str:
    """Page text minus comments, URLs and link targets (a /staging/ path is not wording)."""
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    html = re.sub(r"^\s*//.*$", "", html, flags=re.M)
    return re.sub(r"https?://\S+|(?:href|src)=\"[^\"]*\"|url=/\S*", "", html)


def test_the_vms_page_shows_the_published_camera_slot_prices():
    text = (SITE / "vms.html").read_text(encoding="utf-8")
    shown = re.findall(r'<dd style="margin:0;font-weight:900">\$([0-9.,]+)/month</dd>', text)
    assert shown == ["14.99", "24.99", "39.99", "69.99", "24.99", "39.99", "69.99", "99.99"]
    for price in ("1,249.99", "1,749.99", "2,249.99"):
        assert f"${price}" in text
    assert "SANDBOX TEST PRICES" not in text


def test_the_vms_pages_make_no_free_download_or_windows_claims():
    for page in ("vms.html", "vms.live.html"):
        text = (SITE / page).read_text(encoding="utf-8")
        visible = _visible(text).lower()
        for stale in ("free download", "for free", "free vms", "free software", "windows", "software-download fee"):
            assert stale not in visible, f"{page}: {stale!r}"
        assert "one-time AnyAiCam VMS software licence" in text and "Ubuntu 24.04" in text, page
        for anchor in re.findall(r'href="#([\w-]+)"', text):
            assert f'id="{anchor}"' in text, f"{page}: #{anchor} points nowhere"


def test_the_hardware_catalog_script_has_the_published_prices():
    js = (STAGING / "hardware-catalog.js").read_text(encoding="utf-8")
    for sku, price in (("AIC-APPLIANCE-RYZEN-STARTER", "1249.99"), ("AIC-APPLIANCE-RYZEN-ENTERPRISE", "1749.99"),
                       ("AIC-APPLIANCE-RYZEN-AAC-FACIAL", "2249.99"), ("AIC-RELAY-NUMATO-3CH", "149.99")):
        assert re.search(rf"sku: '{sku}',[^}}]*price: {re.escape(price)},", js), sku
    assert not re.search(r"price: 0\.\d", js)


@pytest.mark.parametrize("page", CUSTOMER_PAGES, ids=lambda p: str(p.relative_to(SITE)))
def test_no_customer_visible_staging_or_sandbox_wording(page):
    visible = _visible(page.read_text(encoding="utf-8"))
    assert not re.search(r"(?i)\bstaging\b|\bsandbox\b", visible), page.name


def test_no_cross_account_price_ids_or_old_releases_anywhere_in_the_storefront():
    for path in SITE.rglob("*"):
        if path.is_file() and path.suffix in (".html", ".js", ".php") and "tests" not in path.parts:
            text = path.read_text(encoding="utf-8")
            assert "LeOpYDAzwe" not in text and "0.1.3" not in text, path.name
            for price_id in re.findall(r"price_1[A-Za-z0-9]{10,}", text):
                assert ACCOUNT_FRAGMENT in price_id, f"{path.name}: {price_id}"


def test_the_storefront_image_never_serves_tests_or_internal_notes():
    dockerfile = (SITE / "Dockerfile").read_text(encoding="utf-8")
    copy = dockerfile.index("COPY . /var/www/html/")
    strip = dockerfile.index("RUN rm -rf /var/www/html/staging/tests")
    assert copy < strip and "find /var/www/html -name '*.md' -delete" in dockerfile[strip:]


@pytest.mark.parametrize("page", CUSTOMER_PAGES, ids=lambda p: str(p.relative_to(SITE)))
def test_every_customer_page_is_mobile_sized(page):
    text = page.read_text(encoding="utf-8")
    if 'http-equiv="refresh"' in text:
        pytest.skip("redirect stub, never rendered")
    assert 'name="viewport" content="width=device-width' in text, page.name


@pytest.mark.parametrize("page", sorted((STAGING).rglob("*.html")), ids=lambda p: p.name)
def test_checkout_pages_only_link_to_complete_pages_in_this_storefront(page):
    """store-staging serves only storefront/: a link to a page whose stylesheet
    lives on the main website would open unstyled."""
    if "tests" in page.parts:
        pytest.skip("not served")
    for ref in re.findall(r'(?:href|src)="([^"#]+)"', page.read_text(encoding="utf-8")):
        if ref.startswith(("http", "mailto:", "tel:", "javascript:")) or "${" in ref:
            continue
        target = (SITE / ref.lstrip("/")) if ref.startswith("/") else (page.parent / ref)
        assert target.resolve().is_file(), f"{page.name} -> {ref}"
        if target.suffix == ".html":
            for css in re.findall(r'<link rel="stylesheet" href="([^"]+)"', target.read_text(encoding="utf-8")):
                assert css.startswith("http") or (target.parent / css).is_file(), f"{page.name} -> {ref} needs missing {css}"
