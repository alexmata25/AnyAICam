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

check(count(CAMERA_PLAN_CATALOG) === 8, 'camera plan catalog has all 8 tiers (4 Local + 4 Hybrid)');
check(count(ANALYTICS_CATALOG) === 10, 'analytics catalog has all 10 products');

foreach (CAMERA_PLAN_CATALOG as $key => $entry) {
    $resolved = resolve_camera_plan($key);
    check($resolved !== null, "camera plan '{$key}' resolves");
    check($resolved !== null && $resolved['price_id'] !== '', "camera plan '{$key}' has a non-empty price_id");
}
foreach (ANALYTICS_CATALOG as $key => $entry) {
    $resolved = resolve_analytic($key);
    check($resolved !== null, "analytic '{$key}' resolves");
    check($resolved !== null && $resolved['price_id'] !== '', "analytic '{$key}' has a non-empty price_id");
}

check(resolve_camera_plan('local_1_8')['price_id'] === 'price_1UD2xKGllhK80H2nFJwtFJvw', 'local_1_8 price_id matches the exact TEST Price ID');
check(resolve_camera_plan('hybrid_33_64')['price_id'] === 'price_1UD33SGllhK80H2nxrbBT2ch', 'hybrid_33_64 price_id matches the exact TEST Price ID');
check(resolve_analytic('facial_recognition')['price_id'] === 'price_1UD3UuGllhK80H2nAdGKN4ff', 'facial_recognition price_id matches the exact TEST Price ID');
check(resolve_analytic('talk_down')['price_id'] === 'price_1UD377GllhK80H2nC1Z2VNh0', 'talk_down price_id matches the exact TEST Price ID');

// Fail-closed on unknown keys -- never a guess.
check(resolve_camera_plan('local_1_9000') === null, 'unknown camera plan key fails closed (null)');
check(resolve_analytic('not_a_real_analytic') === null, 'unknown analytic key fails closed (null)');

// No live Price ID ever accepted: every resolved price_id must be
// TEST-shaped (this repo's convention: never sk_live_/price_live_-style;
// Stripe Price IDs don't carry a mode prefix themselves, so the real
// guard is that these are the exact, previously-reconciled TEST IDs --
// assert none of them match any known-live ID pattern this repo has
// ever used, e.g. accidental live-key-style secrets leaking in as a
// price id).
foreach (array_merge(array_column(CAMERA_PLAN_CATALOG, 'price_id_const'), array_column(ANALYTICS_CATALOG, 'price_id_const')) as $const) {
    $value = config_value_checked($const);
    check(str_starts_with($value, 'price_'), "{$const} looks like a Price ID, not a live secret key or blank value");
}

// ------------------------------------------------------ cart validation

check(throws(fn() => normalize_cart([], $hardwareCatalog)) !== null, 'an empty cart is rejected');
check(throws(fn() => normalize_cart(['appliance_sku' => 'NOT-A-REAL-SKU'], $hardwareCatalog)) !== null, 'unknown appliance SKU is rejected');
check(throws(fn() => normalize_cart(['appliance_sku' => 'AIC-RELAY-NUMATO-3CH'], $hardwareCatalog)) !== null, 'relay SKU cannot be selected as the appliance');
check(throws(fn() => normalize_cart(['camera_plan' => 'local_1_9000'], $hardwareCatalog)) !== null, 'unknown camera plan is rejected');
check(throws(fn() => normalize_cart(['analytics' => ['not_real']], $hardwareCatalog)) !== null, 'unknown analytic is rejected');
check(throws(fn() => normalize_cart(['appliance_sku' => 'AIC-APPLIANCE-RYZEN-STARTER', 'customer_email' => 'not-an-email'], $hardwareCatalog)) !== null, 'invalid email is rejected');

$validCart = normalize_cart([
    'customer_email' => 'sandbox-buyer@example.test',
    'appliance_sku' => 'AIC-APPLIANCE-RYZEN-STARTER',
    'relay' => true,
    'camera_plan' => 'local_1_8',
    'analytics' => ['smart_motion', 'people_counting', 'talk_down'],
], $hardwareCatalog);
check($validCart['appliance_sku'] === 'AIC-APPLIANCE-RYZEN-STARTER', 'valid cart keeps the appliance selection');
check($validCart['camera_plan']['key'] === 'local_1_8', 'valid cart keeps the camera plan selection');
check(count($validCart['analytics']) === 3, 'valid cart keeps all 3 analytics selections');

// One Local/Hybrid plan maximum -- the cart shape itself only ever
// accepts a single 'camera_plan' string, never an array, so "Local AND
// Hybrid simultaneously" is structurally impossible to express, not
// merely rejected after the fact.
check(!is_array($validCart['camera_plan']) || isset($validCart['camera_plan']['key']), 'camera_plan is a single selection, never a list');

// Duplicate analytics keys collapse to one, not an error and not two legs.
$dedupedCart = normalize_cart(['analytics' => ['smart_motion', 'smart_motion', 'people_counting']], $hardwareCatalog);
check(count($dedupedCart['analytics']) === 2, 'duplicate analytics keys are deduplicated, not doubled');

// ---------------------------------------------------------------- legs

$legs = build_checkout_legs($validCart, $hardwareCatalog);
check(count($legs) === 6, 'appliance + relay + camera plan + 3 analytics = 6 legs (one Price ID each)');
check($legs[0]['kind'] === 'hardware' && $legs[0]['mode'] === 'payment', 'leg 1 is the appliance, one-time');
check($legs[1]['kind'] === 'hardware' && $legs[1]['key'] === 'AIC-RELAY-NUMATO-3CH', 'leg 2 is the relay, one-time');
check($legs[2]['kind'] === 'camera_plan' && $legs[2]['mode'] === 'subscription', 'leg 3 is the camera plan, recurring');
check($legs[3]['kind'] === 'analytics' && $legs[4]['kind'] === 'analytics' && $legs[5]['kind'] === 'analytics', 'legs 4-6 are analytics, recurring');
check(count(array_unique(array_column($legs, 'price_id'))) === 6, 'every leg has a distinct Price ID -- no leg silently shares another\'s');

$hardwareOnlyCart = normalize_cart(['appliance_sku' => 'AIC-APPLIANCE-RYZEN-STARTER'], $hardwareCatalog);
$hardwareOnlyLegs = build_checkout_legs($hardwareOnlyCart, $hardwareCatalog);
check(count($hardwareOnlyLegs) === 1 && $hardwareOnlyLegs[0]['mode'] === 'payment', 'hardware alone stays a single one-time leg');

$localOnlyCart = normalize_cart(['camera_plan' => 'local_1_8'], $hardwareCatalog);
check(count(build_checkout_legs($localOnlyCart, $hardwareCatalog)) === 1 && build_checkout_legs($localOnlyCart, $hardwareCatalog)[0]['mode'] === 'subscription', 'Local alone stays a single recurring leg');

$hybridOnlyCart = normalize_cart(['camera_plan' => 'hybrid_1_8'], $hardwareCatalog);
check(build_checkout_legs($hybridOnlyCart, $hardwareCatalog)[0]['price_id'] === 'price_1UD31AGllhK80H2nMKtYEmVw', 'Hybrid alone resolves the correct recurring Price ID');

$analyticsOnlyCart = normalize_cart(['analytics' => ['facial_recognition']], $hardwareCatalog);
check(count(build_checkout_legs($analyticsOnlyCart, $hardwareCatalog)) === 1, 'one analytic alone stays a single recurring leg');

// ------------------------------------------------------- order summary

$summary = order_summary($legs);
check(count($summary['one_time']['items']) === 2, 'summary separates the 2 one-time items (appliance + relay)');
check(count($summary['monthly']['items']) === 4, 'summary separates the 4 monthly items (camera plan + 3 analytics)');
check(abs($summary['one_time']['subtotal'] - 1.03) < 0.001, 'one-time subtotal is $0.50 + $0.53 = $1.03');
check(abs($summary['monthly']['subtotal'] - (0.60 + 0.80 + 0.81 + 0.84)) < 0.001, 'monthly subtotal is $0.60 + $0.80 + $0.81 + $0.84 (Local + Smart Motion + People Counting + Talk Down)');
// Never combined into one misleading total: assert the two buckets are
// genuinely separate keys, not summed into a single 'total' field.
check(!array_key_exists('total', $summary), 'order summary never exposes a combined one-time+monthly total');

echo "\n{$passed} passed, {$failures} failed.\n";
if ($failures > 0) {
    exit(1);
}
echo "ALL PASSED\n";
exit(0);
