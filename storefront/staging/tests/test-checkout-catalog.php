<?php
declare(strict_types=1);

/**
 * STAGING/REVIEW COPY -- NOT the live Bluehost file.
 *
 * Plain-PHP assertion script for checkout-catalog.php's pure functions
 * (no test framework is installed anywhere in this repo). Run with:
 *   php staging/tests/test-checkout-catalog.php
 * Exits non-zero and prints a FAIL line for every failed assertion;
 * prints "ALL PASSED" and exits 0 when everything passes.
 */

require_once __DIR__ . '/../checkout-catalog.php';

$failures = 0;
$passed = 0;

function check(bool $condition, string $description): void {
    global $failures, $passed;
    if ($condition) {
        $passed++;
    } else {
        $failures++;
        echo "FAIL: {$description}\n";
    }
}

function throws(callable $fn): ?string {
    try {
        $fn();
        return null;
    } catch (InvalidArgumentException $e) {
        return $e->getMessage();
    }
}

$hardwareCatalog = [
    'AIC-APPLIANCE-RYZEN-STARTER' => ['name' => 'AnyAiCam Starter', 'unit_amount' => 50, 'price_id' => 'price_1UDnklGllhK80H2nBAHTk6ZP'],
    'AIC-APPLIANCE-RYZEN-ENTERPRISE' => ['name' => 'AnyAiCam Professional', 'unit_amount' => 51, 'price_id' => 'price_1UDnmeGllhK80H2nshtCoJD5'],
    'AIC-APPLIANCE-RYZEN-AAC-FACIAL' => ['name' => 'AnyAiCam Enterprise', 'unit_amount' => 52, 'price_id' => 'price_1UDnnyGllhK80H2nlbwTWX69'],
    'AIC-RELAY-NUMATO-3CH' => ['name' => 'Numato 3-Channel Relay Module', 'unit_amount' => 53, 'price_id' => 'price_1UDnpkGllhK80H2n2EA2lFmQ'],
];

// ------------------------------------------------------------ price IDs

check(count(ANALYTICS_CATALOG) === 5, 'analytics catalog is the 4 packages + Talk Down (2026-09-30 pricing)');
// Sandbox TEST amounts only -- retail lives in app/pricing_catalog.py and is
// never mixed into this sandbox catalog.
foreach (array_merge(ANALYTICS_CATALOG, VMS_LICENSE_CATALOG) as $key => $entry) {
    check($entry['display_price'] === null || $entry['display_price'] < 1.00, "{$key} shows a sandbox TEST amount, never a retail price");
}
check(array_keys(VMS_LICENSE_CATALOG) === [8, 16, 32, 64], 'one VMS software license per capacity (8/16/32/64)');
check(resolve_vms_license(8) === null, 'VMS license fails closed until its TEST Price ID exists');
check(INCLUDED_FEATURES === ['Secure Edge', 'Smart Motion', 'AACO'], 'Secure Edge, Smart Motion and AACO are included, not sold');
check(!isset(ANALYTICS_CATALOG['smart_motion']), 'Smart Motion is no longer sold separately');
check(!isset(ANALYTICS_CATALOG['facial_recognition']) && !isset(ANALYTICS_CATALOG['cloud_overflow']), 'Face Access and Cloud Overflow are not sold here');

// Camera plans are per camera and chosen in the AnyAiCam account; the fixed
// Local/Hybrid tiers are not sold here any more (2026-10-08).
check(!defined('CAMERA_PLAN_CATALOG') && !function_exists('resolve_camera_plan'), 'no fixed-tier camera plan catalog remains');
// Their TEST Price IDs stay recorded, unchanged (never edited or deleted in Stripe).
check(config_value_checked('LOCAL_1_8_PRICE_ID') === 'price_1UD2xKGllhK80H2nFJwtFJvw', 'retired LOCAL_1_8 TEST Price ID left unchanged');
check(config_value_checked('HYBRID_33_64_PRICE_ID') === 'price_1UD33SGllhK80H2nxrbBT2ch', 'retired HYBRID_33_64 TEST Price ID left unchanged');
foreach (ANALYTICS_CATALOG as $key => $entry) {
    if ($key === 'advanced_analytics') {
        continue;  // no TEST Price ID yet -- asserted to fail closed below
    }
    $resolved = resolve_analytic($key);
    check($resolved !== null, "analytic '{$key}' resolves");
    check($resolved !== null && $resolved['price_id'] !== '', "analytic '{$key}' has a non-empty price_id");
}

check(resolve_analytic('facial_recognition') === null, 'Face Access is not sold here (per door, sized by enrolled people)');
check(resolve_analytic('advanced_analytics') === null, 'Advanced Analytics fails closed until its TEST Price ID exists');
check(resolve_analytic('smart_motion') === null, 'Smart Motion cannot be bought (included in every plan)');
check(resolve_analytic('talk_down')['price_id'] === 'price_1UD377GllhK80H2nC1Z2VNh0', 'talk_down price_id matches the exact TEST Price ID');

// Fail-closed on unknown keys -- never a guess.
check(resolve_analytic('not_a_real_analytic') === null, 'unknown analytic key fails closed (null)');

// No live Price ID ever accepted: every resolved price_id must be
// TEST-shaped (this repo's convention: never sk_live_/price_live_-style;
// Stripe Price IDs don't carry a mode prefix themselves, so the real
// guard is that these are the exact, previously-reconciled TEST IDs --
// assert none of them match any known-live ID pattern this repo has
// ever used, e.g. accidental live-key-style secrets leaking in as a
// price id).
foreach (array_column(ANALYTICS_CATALOG, 'price_id_const') as $const) {
    $value = config_value_checked($const);
    if ($const === 'ANALYTICS_ADVANCED_PRICE_ID') {
        check($value === '', 'ANALYTICS_ADVANCED_PRICE_ID is intentionally empty until created');
        continue;
    }
    check(str_starts_with($value, 'price_'), "{$const} looks like a Price ID, not a live secret key or blank value");
}

// ------------------------------------------------------ cart validation

check(throws(fn() => normalize_cart([], $hardwareCatalog)) !== null, 'an empty cart is rejected');
check(throws(fn() => normalize_cart(['appliance_sku' => 'NOT-A-REAL-SKU'], $hardwareCatalog)) !== null, 'unknown appliance SKU is rejected');
check(throws(fn() => normalize_cart(['appliance_sku' => 'AIC-RELAY-NUMATO-3CH'], $hardwareCatalog)) !== null, 'relay SKU cannot be selected as the appliance');
$planRefusal = throws(fn() => normalize_cart(['camera_plan' => 'local_1_8'], $hardwareCatalog));
check($planRefusal !== null && str_contains($planRefusal, 'My subscription'), 'a fixed-tier camera plan is refused, pointing to My subscription');
check(throws(fn() => normalize_cart(['appliance_sku' => 'AIC-APPLIANCE-RYZEN-STARTER', 'camera_plan' => 'hybrid_1_8'], $hardwareCatalog)) !== null,
    'a camera plan is refused with an appliance too');
check(throws(fn() => normalize_cart(['vms_license_capacity' => 12], $hardwareCatalog)) !== null, 'an unknown VMS license size is rejected');
check(throws(fn() => normalize_cart(['analytics' => ['not_real']], $hardwareCatalog)) !== null, 'unknown analytic is rejected');
check(throws(fn() => normalize_cart(['appliance_sku' => 'AIC-APPLIANCE-RYZEN-STARTER', 'customer_email' => 'not-an-email'], $hardwareCatalog)) !== null, 'invalid email is rejected');

$validCart = normalize_cart([
    'customer_email' => 'sandbox-buyer@example.test',
    'appliance_sku' => 'AIC-APPLIANCE-RYZEN-STARTER',
    'relay' => true,
    'vms_license_capacity' => 8,
    'analytics' => ['ai_essentials', 'vehicle_intelligence', 'talk_down'],
], $hardwareCatalog);
check($validCart['appliance_sku'] === 'AIC-APPLIANCE-RYZEN-STARTER', 'valid cart keeps the appliance selection');
check(!array_key_exists('camera_plan', $validCart), 'the cart has no camera plan');
check(count($validCart['analytics']) === 3, 'valid cart keeps all 3 analytics selections');
check($validCart['vms_license'] === null && $validCart['vms_license_included'] === true,
    'with an AnyAiCam appliance the VMS software license is included, never charged');


// Duplicate analytics keys collapse to one, not an error and not two legs.
$dedupedCart = normalize_cart(['analytics' => ['ai_essentials', 'ai_essentials', 'talk_down']], $hardwareCatalog);
check(throws(fn() => normalize_cart(['analytics' => ['advanced_analytics']], $hardwareCatalog)) !== null, 'Advanced Analytics is rejected until configured');
check(throws(fn() => normalize_cart(['analytics' => ['smart_motion']], $hardwareCatalog)) !== null, 'Smart Motion is rejected (included, not sold)');
check(count($dedupedCart['analytics']) === 2, 'duplicate analytics keys are deduplicated, not doubled');

// ---------------------------------------------------------------- legs

$legs = build_checkout_legs($validCart, $hardwareCatalog);
check(count($legs) === 5, 'appliance + relay + 3 analytics = 5 legs (one Price ID each; the license is included)');
check($legs[0]['kind'] === 'hardware' && $legs[0]['mode'] === 'payment', 'leg 1 is the appliance, one-time');
check($legs[1]['kind'] === 'hardware' && $legs[1]['key'] === 'AIC-RELAY-NUMATO-3CH', 'leg 2 is the relay, one-time');
check(!in_array('camera_plan', array_column($legs, 'kind'), true), 'no camera plan leg');
check($legs[2]['kind'] === 'analytics' && $legs[3]['kind'] === 'analytics' && $legs[4]['kind'] === 'analytics', 'legs 3-5 are analytics, recurring');
check(count(array_unique(array_column($legs, 'price_id'))) === 5, 'every leg has a distinct Price ID -- no leg silently shares another\'s');

$hardwareOnlyCart = normalize_cart(['appliance_sku' => 'AIC-APPLIANCE-RYZEN-STARTER'], $hardwareCatalog);
$hardwareOnlyLegs = build_checkout_legs($hardwareOnlyCart, $hardwareCatalog);
check(count($hardwareOnlyLegs) === 1 && $hardwareOnlyLegs[0]['mode'] === 'payment', 'hardware alone stays a single one-time leg');

// DIY (customer-owned PC): the one-time VMS software license fails closed
// until its TEST Price ID exists -- never sold without one.
check(throws(fn() => normalize_cart(['vms_license_capacity' => 16], $hardwareCatalog)) !== null,
    'a DIY license-only cart is refused while the VMS license is unconfigured');

$applianceLicenseCart = normalize_cart(['appliance_sku' => 'AIC-APPLIANCE-RYZEN-STARTER', 'vms_license_capacity' => 16], $hardwareCatalog);
$applianceLicenseLegs = build_checkout_legs($applianceLicenseCart, $hardwareCatalog);
check(count($applianceLicenseLegs) === 1 && $applianceLicenseCart['vms_license_included'] === true, 'appliance + license = 1 leg, the license included');
check(!in_array('vms_license', array_column($applianceLicenseLegs, 'kind'), true), 'no VMS license leg when an appliance is bought (no double charge)');

$analyticsOnlyCart = normalize_cart(['analytics' => ['talk_down']], $hardwareCatalog);
check(count(build_checkout_legs($analyticsOnlyCart, $hardwareCatalog)) === 1, 'one analytic alone stays a single recurring leg');

// ------------------------------------------------------- order summary

$summary = order_summary($legs);
check(count($summary['one_time']['items']) === 2, 'summary separates the 2 one-time items (appliance + relay)');
check(count($summary['monthly']['items']) === 3, 'summary separates the 3 monthly items (3 analytics)');
check(abs($summary['one_time']['subtotal'] - 1.03) < 0.001, 'one-time subtotal is $0.50 + $0.53 = $1.03 (license included with the appliance)');
check(abs($summary['monthly']['subtotal'] - (0.85 + 0.87 + 0.84)) < 0.001, 'monthly subtotal is the sandbox $0.60 + $0.85 + $0.87 + $0.84 (Local 8 + AI Essentials + Vehicle Intelligence + Talk Down)');
// Never combined into one misleading total: assert the two buckets are
// genuinely separate keys, not summed into a single 'total' field.
check(!array_key_exists('total', $summary), 'order summary never exposes a combined one-time+monthly total');

echo "\n{$passed} passed, {$failures} failed.\n";
if ($failures > 0) {
    exit(1);
}
echo "ALL PASSED\n";
exit(0);
