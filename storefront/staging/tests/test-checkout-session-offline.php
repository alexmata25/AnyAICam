<?php
declare(strict_types=1);

/**
 * Offline end-to-end test of checkout-session.php (2026-10-04): the real
 * endpoint, served by `php -S`, against tests/fake-stripe.php -- no network,
 * no Stripe key. Run through tests/run-offline-tests.sh.
 *
 * Proves: every product the storefront sells opens a Checkout Session on
 * exactly the Price the staging portal recognizes (deploy/staging-stripe-
 * test-prices.json) with the metadata the portal reads; a Stripe Price that
 * does not charge the published amount (or is recurring/inactive/unknown)
 * refuses checkout before any session exists; client-supplied price IDs and
 * amounts are ignored; unsellable or unknown items are refused; a double click
 * in one journey reuses one session; the legacy cart endpoint is retired.
 */

$base = 'http://127.0.0.1:8099/staging';
$state = rtrim((string)getenv('FAKE_STRIPE_STATE'), '/');
$portal = json_decode(file_get_contents((string)getenv('FAKE_STRIPE_PRICES')), true)['prices'];
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

function reset_fake(array $overrides = []): void {
    global $state;
    foreach (['log.jsonl', 'customers.json', 'sessions.json', 'overrides.json'] as $f) {
        @unlink("$state/$f");
    }
    file_put_contents("$state/overrides.json", json_encode((object)$overrides));
}

function stripe_log(): array {
    global $state;
    $lines = is_file("$state/log.jsonl") ? file("$state/log.jsonl", FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) : [];
    return array_map(fn($l) => json_decode($l, true), $lines);
}

function session_posts(): array {
    return array_values(array_filter(stripe_log(), fn($r) => $r['method'] === 'POST' && $r['path'] === '/v1/checkout/sessions'));
}

function sessions_created(): int {
    global $state;
    $s = is_file("$state/sessions.json") ? json_decode(file_get_contents("$state/sessions.json"), true) : [];
    return count($s['items'] ?? []);
}

function post_checkout(array $body, string $endpoint = 'checkout-session.php'): array {
    global $base;
    $ch = curl_init("$base/$endpoint");
    curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER => true, CURLOPT_POST => true,
                            CURLOPT_HTTPHEADER => ['Content-Type: application/json'], CURLOPT_POSTFIELDS => json_encode($body)]);
    $response = curl_exec($ch);
    $status = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    return ['status' => $status, 'data' => json_decode((string)$response, true) ?? []];
}

function journey(): string {
    return bin2hex(random_bytes(12));
}

function buy(string $kind, string $key, array $extraLeg = [], ?string $journey = null): array {
    return post_checkout(['journey_id' => $journey ?? journey(), 'customer_email' => 'buyer@example.test', 'stripe_customer_id' => '',
                          'legs' => [['kind' => $kind, 'key' => $key] + $extraLeg]]);
}

// [kind, key, portal env var, Checkout mode, sku/class metadata]
$products = [
    ['hardware', 'AIC-APPLIANCE-RYZEN-STARTER', 'ANYAICAM_STRIPE_PRICE_RYZEN_STARTER', 'payment'],
    ['hardware', 'AIC-APPLIANCE-RYZEN-ENTERPRISE', 'ANYAICAM_STRIPE_PRICE_RYZEN_ENTERPRISE', 'payment'],
    ['hardware', 'AIC-APPLIANCE-RYZEN-AAC-FACIAL', 'ANYAICAM_STRIPE_PRICE_RYZEN_AAC_FACIAL', 'payment'],
    ['hardware', 'AIC-RELAY-NUMATO-3CH', 'ANYAICAM_STRIPE_PRICE_RELAY_NUMATO_3CH', 'payment'],
    ['vms_license', '8', 'ANYAICAM_STRIPE_PRICE_VMS_LICENSE_8', 'payment'],
    ['vms_license', '16', 'ANYAICAM_STRIPE_PRICE_VMS_LICENSE_16', 'payment'],
    ['vms_license', '32', 'ANYAICAM_STRIPE_PRICE_VMS_LICENSE_32', 'payment'],
    ['vms_license', '64', 'ANYAICAM_STRIPE_PRICE_VMS_LICENSE_64', 'payment'],
    ['analytics', 'ai_essentials', 'ANYAICAM_STRIPE_PRICE_ANALYTICS_AI_ESSENTIALS', 'subscription'],
    ['analytics', 'ai_professional', 'ANYAICAM_STRIPE_PRICE_ANALYTICS_AI_PROFESSIONAL', 'subscription'],
    ['analytics', 'vehicle_intelligence', 'ANYAICAM_STRIPE_PRICE_ANALYTICS_VEHICLE_INTELLIGENCE', 'subscription'],
    ['analytics', 'advanced_analytics', 'ANYAICAM_STRIPE_PRICE_ADVANCED_ANALYTICS', 'subscription'],
    ['analytics', 'talk_down', 'ANYAICAM_STRIPE_PRICE_ANALYTICS_TALK_DOWN', 'subscription'],
];

// ------------------------------------------------ every product, every price
foreach ($products as [$kind, $key, $envVar, $mode]) {
    reset_fake();
    $response = buy($kind, $key);
    $label = "$kind $key";
    check($response['status'] === 200, "$label: checkout opens (HTTP {$response['status']} " . json_encode($response['data']) . ")");
    $posts = session_posts();
    check(count($posts) === 1, "$label: exactly one Checkout Session request");
    if (!$posts) {
        continue;
    }
    $fields = $posts[0]['fields'];
    $expected = $portal[$envVar]['price_id'];
    check(($fields['line_items'][0]['price'] ?? '') === $expected, "$label: charges the portal-recognized Price $expected");
    check(($fields['metadata']['anyaicam_stripe_price_id'] ?? '') === $expected, "$label: metadata names the same Price the portal maps");
    check(($fields['mode'] ?? '') === $mode, "$label: Checkout mode is $mode");
    check(($fields['line_items'][0]['quantity'] ?? '') === '1', "$label: quantity is always 1");
    check(str_starts_with((string)($posts[0]['key'] ?? ''), 'storefront-session-'), "$label: the session request carries an Idempotency-Key");
    if ($kind === 'hardware') {
        check(($fields['metadata']['anyaicam_hardware_sku'] ?? '') === $key, "$label: hardware SKU metadata for hardware_orders.py");
    }
    if ($kind === 'vms_license') {
        check(($fields['metadata']['anyaicam_product_class'] ?? '') === 'vms_license' && ($fields['metadata']['anyaicam_vms_license_capacity'] ?? '') === $key,
            "$label: license metadata for customer_entitlements.py");
    }
    $priceReads = array_values(array_filter(stripe_log(), fn($r) => $r['method'] === 'GET' && $r['path'] === "/v1/prices/$expected"));
    check(count($priceReads) === 1, "$label: the Stripe Price was verified before checkout");
}

// --------------------------- camera plans: per camera, chosen in the account
// The fixed 8/16/32/64-camera Local/Hybrid tiers were removed (2026-10-08):
// a camera-plan leg is refused and never reaches Stripe.
foreach (['local_1_8', 'local_9_16', 'local_17_32', 'local_33_64', 'hybrid_1_8', 'hybrid_9_16', 'hybrid_17_32', 'hybrid_33_64'] as $key) {
    reset_fake();
    $response = buy('camera_plan', $key);
    check($response['status'] === 400 && str_contains((string)($response['data']['error'] ?? ''), 'My subscription'),
        "camera_plan $key is refused, pointing to My subscription");
    check(sessions_created() === 0 && count(session_posts()) === 0, "camera_plan $key: no Checkout Session is ever requested");
}

// ------------------------------------------------------ price verification
$starter = $portal['ANYAICAM_STRIPE_PRICE_RYZEN_STARTER']['price_id'];
$talkDown = $portal['ANYAICAM_STRIPE_PRICE_ANALYTICS_TALK_DOWN']['price_id'];
$priceObject = fn(string $id, int $cents, $recurring = null, bool $active = true) =>
    ['id' => $id, 'object' => 'price', 'active' => $active, 'currency' => 'usd', 'unit_amount' => $cents, 'recurring' => $recurring];

foreach ([
    'a Stripe Price at another amount' => [$starter, $priceObject($starter, 50)],
    'a recurring Price for a one-time appliance' => [$starter, $priceObject($starter, 124999, ['interval' => 'month', 'interval_count' => 1])],
    'an inactive Price' => [$starter, $priceObject($starter, 124999, null, false)],
] as $what => [$id, $object]) {
    reset_fake([$id => $object]);
    $response = buy('hardware', 'AIC-APPLIANCE-RYZEN-STARTER');
    check($response['status'] === 503 && str_contains((string)($response['data']['error'] ?? ''), 'PRICE_MISMATCH'), "$what refuses checkout (PRICE_MISMATCH)");
    check(sessions_created() === 0 && count(session_posts()) === 0, "$what: no Checkout Session is ever requested");
}
reset_fake([$talkDown => $priceObject($talkDown, 499, null)]);
$response = buy('analytics', 'talk_down');
check($response['status'] === 503 && sessions_created() === 0, 'a one-time Price for a monthly add-on refuses checkout');
reset_fake([$talkDown => $priceObject($talkDown, 499, ['interval' => 'year', 'interval_count' => 1])]);
check(buy('analytics', 'talk_down')['status'] === 503 && sessions_created() === 0, 'a yearly Price for a monthly add-on refuses checkout');
reset_fake([$starter => ['_status' => 404]]);
$response = buy('hardware', 'AIC-APPLIANCE-RYZEN-STARTER');
check($response['status'] === 502 && sessions_created() === 0, 'a Price Stripe does not know refuses checkout, no session');

// ------------------------------------------- tampered and unsellable input
reset_fake();
$response = buy('hardware', 'AIC-APPLIANCE-RYZEN-STARTER', ['price_id' => 'price_evil', 'amount' => 1, 'unit_amount' => 1, 'label' => 'Free']);
$posts = session_posts();
check($response['status'] === 200 && $posts && ($posts[0]['fields']['line_items'][0]['price'] ?? '') === $starter,
    'a client-supplied price_id/amount is ignored: the server charges its own Price');
check(!str_contains(json_encode($posts), 'price_evil'), 'the tampered Price ID never reaches Stripe');
foreach ([
    ['hardware', 'AIC-CAM-5MP-DOME-001', 'a camera (no Price in this Stripe account)'],
    ['hardware', 'AIC-POE-MOKER-8', 'a PoE switch (no Price in this Stripe account)'],
    ['hardware', 'AIC-APPLIANCE-RYZEN-FREE', 'an unknown appliance SKU'],
    ['camera_plan', 'local_1_8', 'a fixed-tier camera plan (per camera, chosen in the account)'],
    ['analytics', 'smart_motion', 'Smart Motion (included, never sold)'],
    ['analytics', 'facial_recognition', 'the retired Facial Recognition SKU'],
    ['vms_license', '12', 'an unknown license size'],
    ['coupon', 'FREE', 'an unknown leg kind'],
] as [$kind, $key, $what]) {
    reset_fake();
    $response = buy($kind, $key);
    check(in_array($response['status'], [400, 503], true) && sessions_created() === 0, "$what is refused, no session (HTTP {$response['status']})");
}

// --------------------------------------------------- duplicate-click safety
reset_fake();
$j = journey();
$first = buy('hardware', 'AIC-APPLIANCE-RYZEN-STARTER', [], $j);
$second = buy('hardware', 'AIC-APPLIANCE-RYZEN-STARTER', [], $j);
$keys = array_column(session_posts(), 'key');
check($first['status'] === 200 && $second['status'] === 200, 'both clicks answer');
check(($first['data']['session_id'] ?? 'a') === ($second['data']['session_id'] ?? 'b'), 'a double click in one journey returns the same Checkout Session');
check(count($keys) === 2 && $keys[0] === $keys[1], 'both requests carried the same Idempotency-Key');
check(sessions_created() === 1, 'Stripe created exactly one session');
$customerKeys = array_column(array_values(array_filter(stripe_log(), fn($r) => $r['path'] === '/v1/customers')), 'key');
check(count(array_unique($customerKeys)) === 1, 'and exactly one Stripe Customer for the journey');
$other = buy('hardware', 'AIC-APPLIANCE-RYZEN-STARTER');
check(($other['data']['session_id'] ?? '') !== ($first['data']['session_id'] ?? ''), 'a new visit (new journey) is a new purchase');
reset_fake();
$noJourney = post_checkout(['customer_email' => 'buyer@example.test', 'legs' => [['kind' => 'hardware', 'key' => 'AIC-APPLIANCE-RYZEN-STARTER']]]);
check($noJourney['status'] === 400 && sessions_created() === 0, 'a request without a journey id (an out-of-date page) is refused');
$badCustomer = post_checkout(['journey_id' => journey(), 'stripe_customer_id' => 'cus_x"; DROP', 'legs' => [['kind' => 'hardware', 'key' => 'AIC-APPLIANCE-RYZEN-STARTER']]]);
check($badCustomer['status'] === 400, 'a malformed Stripe customer id is refused');

// --------------------------------------------------------- DIY journey legs
reset_fake();
$j = journey();
$plan = post_checkout(['journey_id' => $j, 'customer_email' => 'diy@example.test',
                       'legs' => [['kind' => 'vms_license', 'key' => '16'], ['kind' => 'analytics', 'key' => 'ai_essentials']]]);
check($plan['status'] === 200 && ($plan['data']['remaining_legs_count'] ?? 0) === 1, 'a DIY journey starts with the license and keeps the add-on leg');
$continueQuery = [];
parse_str((string)parse_url((string)(session_posts()[0]['fields']['success_url'] ?? ''), PHP_URL_QUERY), $continueQuery);
check(($continueQuery['journey_id'] ?? '') === $j, 'the journey id travels to checkout-continue.html');

// ------------------------------------------------ legacy cart is retired
$legacy = post_checkout(['adapter_type' => '8ch', 'camera_quantity' => 1], 'stripe-checkout.php');
check($legacy['status'] === 410, 'the legacy stripe-checkout.php cart (cameras/PoE/adapters, ad-hoc price_data) is retired');

echo "\n{$passed} passed, {$failures} failed.\n";
exit($failures > 0 ? 1 : 0);
