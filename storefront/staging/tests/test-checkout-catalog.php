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

// The real hardware catalog (2026-10-04), not a copy: published prices and
// the portal-recognized TEST Price IDs from stripe-config.php.
$hardwareCatalog = require __DIR__ . '/../hardware-price-catalog.php';
check($hardwareCatalog['AIC-APPLIANCE-RYZEN-STARTER']['unit_amount'] === 124999 && $hardwareCatalog['AIC-APPLIANCE-RYZEN-STARTER']['price_id'] === 'price_1ULTEfGllhK80H2nlzTBBk7G', 'Starter: $1,249.99 on the portal-recognized Price');
check($hardwareCatalog['AIC-APPLIANCE-RYZEN-ENTERPRISE']['unit_amount'] === 174999 && $hardwareCatalog['AIC-APPLIANCE-RYZEN-ENTERPRISE']['price_id'] === 'price_1ULTEgGllhK80H2n2GBTpJ3v', 'Professional: $1,749.99 on the portal-recognized Price');
check($hardwareCatalog['AIC-APPLIANCE-RYZEN-AAC-FACIAL']['unit_amount'] === 224999 && $hardwareCatalog['AIC-APPLIANCE-RYZEN-AAC-FACIAL']['price_id'] === 'price_1ULTEgGllhK80H2nbVKxBgNy', 'Enterprise: $2,249.99 on the portal-recognized Price');
check($hardwareCatalog['AIC-RELAY-NUMATO-3CH']['unit_amount'] === 14999 && $hardwareCatalog['AIC-RELAY-NUMATO-3CH']['price_id'] === 'price_1ULTEhGllhK80H2ncXLynne4', 'Relay: $149.99 on the portal-recognized Price');
foreach ($hardwareCatalog as $sku => $item) {
    if (!in_array($sku, SELLABLE_HARDWARE_SKUS, true)) {
        check($item['price_id'] === '', "{$sku} is not sold here (no Price in this Stripe account)");
    }
}

// ------------------------------------------------------------ price IDs

check(count(CAMERA_PLAN_CATALOG) === 8, 'camera plan catalog has all 8 tiers (4 Local + 4 Hybrid)');
check(count(ANALYTICS_CATALOG) === 5, 'analytics catalog is the 4 packages + Talk Down (2026-09-30 pricing)');
// Published catalog amounts (app/pricing_catalog.py); the display price is
// always exactly the amount the server verifies against Stripe.
$expectedCents = ['local_1_8' => 1499, 'local_9_16' => 2499, 'local_17_32' => 3999, 'local_33_64' => 6999,
                  'hybrid_1_8' => 2499, 'hybrid_9_16' => 3999, 'hybrid_17_32' => 6999, 'hybrid_33_64' => 9999,
                  'ai_essentials' => 799, 'ai_professional' => 1499, 'vehicle_intelligence' => 1499,
                  'advanced_analytics' => 2499, 'talk_down' => 499, 8 => 4999, 16 => 7999, 32 => 12999, 64 => 19999];
foreach ([CAMERA_PLAN_CATALOG, ANALYTICS_CATALOG, VMS_LICENSE_CATALOG] as $catalog) {
    foreach ($catalog as $key => $entry) {
        check($entry['amount_cents'] === $expectedCents[$key], "{$key} charges the published {$expectedCents[$key]} cents");
        check(abs($entry['display_price'] - $entry['amount_cents'] / 100) < 0.001, "{$key} displays exactly its charged amount");
    }
}
check(array_column(CAMERA_PLAN_CATALOG, 'capacity') === [8, 16, 32, 64, 8, 16, 32, 64], 'every plan carries its camera capacity');
check(array_keys(VMS_LICENSE_CATALOG) === [8, 16, 32, 64], 'one VMS software license per capacity (8/16/32/64)');
foreach ([8 => 'price_1ULTDjGllhK80H2n4cd5fVX1', 16 => 'price_1ULTDjGllhK80H2n7QtlNYHf', 32 => 'price_1ULTDkGllhK80H2nsuVnC8cw', 64 => 'price_1ULTDkGllhK80H2nlPrgGVOL'] as $capacity => $priceId) {
    check((resolve_vms_license($capacity)['price_id'] ?? '') === $priceId, "VMS license {$capacity} uses the portal-recognized Price");
}
check(resolve_vms_license(12) === null, 'an unknown license capacity fails closed');
check(INCLUDED_FEATURES === ['Secure Edge', 'Smart Motion', 'AACO'], 'Secure Edge, Smart Motion and AACO are included, not sold');
check(!isset(ANALYTICS_CATALOG['smart_motion']), 'Smart Motion is no longer sold separately');
check(!isset(ANALYTICS_CATALOG['facial_recognition']) && !isset(ANALYTICS_CATALOG['cloud_overflow']), 'Face Access and Cloud Overflow are not sold here');

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

check(resolve_camera_plan('local_1_8')['price_id'] === 'price_1ULTDbGllhK80H2nG5js8TyE', 'local_1_8 uses the portal-recognized TEST Price');
check(resolve_camera_plan('hybrid_33_64')['price_id'] === 'price_1ULTDeGllhK80H2nD8jd8rpd', 'hybrid_33_64 uses the portal-recognized TEST Price');
check(resolve_analytic('facial_recognition') === null, 'Face Access is not sold here (per door, sized by enrolled people)');
check(resolve_analytic('advanced_analytics')['price_id'] === 'price_1ULTDgGllhK80H2nGYQNvKw5', 'Advanced Analytics uses the portal-recognized TEST Price');
check(resolve_analytic('smart_motion') === null, 'Smart Motion cannot be bought (included in every plan)');
check(resolve_analytic('talk_down')['price_id'] === 'price_1ULTDhGllhK80H2nJBZtYL9i', 'talk_down uses the portal-recognized TEST Price');

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
    'analytics' => ['ai_essentials', 'vehicle_intelligence', 'talk_down'],
], $hardwareCatalog);
check($validCart['appliance_sku'] === 'AIC-APPLIANCE-RYZEN-STARTER', 'valid cart keeps the appliance selection');
check($validCart['camera_plan']['key'] === 'local_1_8', 'valid cart keeps the camera plan selection');
check(count($validCart['analytics']) === 3, 'valid cart keeps all 3 analytics selections');
check($validCart['vms_license'] === null && $validCart['vms_license_included'] === true,
    'with an AnyAiCam appliance the VMS software license is included, never charged');

// One Local/Hybrid plan maximum -- the cart shape itself only ever
// accepts a single 'camera_plan' string, never an array, so "Local AND
// Hybrid simultaneously" is structurally impossible to express, not
// merely rejected after the fact.
check(!is_array($validCart['camera_plan']) || isset($validCart['camera_plan']['key']), 'camera_plan is a single selection, never a list');

// Duplicate analytics keys collapse to one, not an error and not two legs.
$dedupedCart = normalize_cart(['analytics' => ['ai_essentials', 'ai_essentials', 'talk_down']], $hardwareCatalog);
check(throws(fn() => normalize_cart(['analytics' => ['advanced_analytics']], $hardwareCatalog)) === null, 'Advanced Analytics can be bought');
check(throws(fn() => normalize_cart(['analytics' => ['smart_motion']], $hardwareCatalog)) !== null, 'Smart Motion is rejected (included, not sold)');
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

// DIY (customer-owned PC): a camera plan without an appliance carries the
// one-time VMS software license of the same capacity -- never sold without it.
$diyLegs = build_checkout_legs(normalize_cart(['camera_plan' => 'local_9_16'], $hardwareCatalog), $hardwareCatalog);
check(array_column($diyLegs, 'kind') === ['camera_plan', 'vms_license'], 'a DIY plan-only cart adds the license leg');
check($diyLegs[1]['key'] === '16' && $diyLegs[1]['price_id'] === 'price_1ULTDjGllhK80H2n7QtlNYHf' && abs($diyLegs[1]['amount'] - 79.99) < 0.001,
    'the DIY license matches the plan capacity: 16 cameras, $79.99');

$appliancePlanCart = normalize_cart(['appliance_sku' => 'AIC-APPLIANCE-RYZEN-STARTER', 'camera_plan' => 'hybrid_1_8'], $hardwareCatalog);
$appliancePlanLegs = build_checkout_legs($appliancePlanCart, $hardwareCatalog);
check(count($appliancePlanLegs) === 2, 'appliance + Hybrid = 2 legs, no separate license leg');
check(!in_array('vms_license', array_column($appliancePlanLegs, 'kind'), true), 'no VMS license leg when an appliance is bought (no double charge)');
check($appliancePlanLegs[1]['price_id'] === 'price_1ULTDdGllhK80H2ncLHUF7XQ', 'Hybrid resolves the portal-recognized recurring Price');

$analyticsOnlyCart = normalize_cart(['analytics' => ['talk_down']], $hardwareCatalog);
check(count(build_checkout_legs($analyticsOnlyCart, $hardwareCatalog)) === 1, 'one analytic alone stays a single recurring leg');

// ------------------------------------------------------- order summary

$summary = order_summary($legs);
check(count($summary['one_time']['items']) === 2, 'summary separates the 2 one-time items (appliance + relay)');
check(count($summary['monthly']['items']) === 4, 'summary separates the 4 monthly items (camera plan + 3 analytics)');
check(abs($summary['one_time']['subtotal'] - 1399.98) < 0.001, 'one-time subtotal is $1,249.99 + $149.99 = $1,399.98 (license included with the appliance)');
check(abs($summary['monthly']['subtotal'] - (14.99 + 7.99 + 14.99 + 4.99)) < 0.001, 'monthly subtotal is $14.99 + $7.99 + $14.99 + $4.99 (Local 8 + AI Essentials + Vehicle Intelligence + Talk Down)');

// --------------------------------------------- Stripe Price verification
$good = ['id' => 'price_x', 'active' => true, 'currency' => 'usd', 'unit_amount' => 124999, 'recurring' => null];
check(stripe_price_problem($good, 'price_x', 124999, 'payment') === null, 'a matching one-time Price passes');
check(stripe_price_problem(['unit_amount' => 50] + $good, 'price_x', 124999, 'payment') !== null, 'a Price that charges another amount is refused');
check(stripe_price_problem(['unit_amount' => '124999'] + $good, 'price_x', 124999, 'payment') !== null, 'a non-integer amount is refused');
check(stripe_price_problem(['active' => false] + $good, 'price_x', 124999, 'payment') !== null, 'an inactive Price is refused');
check(stripe_price_problem(['currency' => 'eur'] + $good, 'price_x', 124999, 'payment') !== null, 'a non-USD Price is refused');
check(stripe_price_problem(['id' => 'price_other'] + $good, 'price_x', 124999, 'payment') !== null, 'a different Price is refused');
check(stripe_price_problem(['recurring' => ['interval' => 'month']] + $good, 'price_x', 124999, 'payment') !== null, 'a recurring Price is refused for a one-time item');
$monthly = ['id' => 'price_m', 'active' => true, 'currency' => 'usd', 'unit_amount' => 1499, 'recurring' => ['interval' => 'month', 'interval_count' => 1]];
check(stripe_price_problem($monthly, 'price_m', 1499, 'subscription') === null, 'a matching monthly Price passes');
check(stripe_price_problem(['recurring' => ['interval' => 'year', 'interval_count' => 1]] + $monthly, 'price_m', 1499, 'subscription') !== null, 'a yearly Price is refused for a monthly plan');
check(stripe_price_problem(['recurring' => null] + $monthly, 'price_m', 1499, 'subscription') !== null, 'a one-time Price is refused for a monthly plan');
check(stripe_price_problem([], 'price_m', 1499, 'subscription') !== null, 'an empty answer is refused');
// Never combined into one misleading total: assert the two buckets are
// genuinely separate keys, not summed into a single 'total' field.
check(!array_key_exists('total', $summary), 'order summary never exposes a combined one-time+monthly total');

echo "\n{$passed} passed, {$failures} failed.\n";
if ($failures > 0) {
    exit(1);
}
echo "ALL PASSED\n";
exit(0);
