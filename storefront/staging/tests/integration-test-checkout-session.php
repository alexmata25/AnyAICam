<?php
declare(strict_types=1);

/**
 * STAGING/REVIEW COPY -- NOT the live Bluehost file.
 *
 * Real integration test for checkout-session.php against the live
 * Stripe TEST API -- not a mock. Creates real (uncompleted) Checkout
 * Sessions for every required combination, verifies each one via a
 * fresh GET back from Stripe (livemode=false, correct mode, correct
 * reused Stripe Customer), then expires it. Never completes payment.
 *
 * Run from this directory's parent (staging/):
 *   1. In one terminal: STRIPE_SECRET_KEY=sk_test_... php -S 127.0.0.1:8099
 *      (run from staging/, so checkout-session.php resolves its own
 *      require_once paths correctly)
 *   2. In another terminal: STRIPE_SECRET_KEY=sk_test_... php tests/integration-test-checkout-session.php
 *
 * Requires a real Stripe TEST secret key -- never run this against a
 * live key (checkout-session.php itself also refuses to run against
 * one, as a second layer of protection).
 */

$secretKey = getenv('STRIPE_SECRET_KEY');
if (!$secretKey) {
    fwrite(STDERR, "STRIPE_SECRET_KEY env var required\n");
    exit(1);
}

function call_endpoint(array $body): array {
    $ch = curl_init('http://127.0.0.1:8099/checkout-session.php');
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_POST => true,
        CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
        CURLOPT_POSTFIELDS => json_encode($body),
    ]);
    $response = curl_exec($ch);
    $status = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    $data = json_decode($response, true);
    return ['status' => $status, 'data' => $data];
}

function stripe_get(string $path, string $secretKey): array {
    $ch = curl_init("https://api.stripe.com{$path}");
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER => ['Authorization: Bearer ' . $secretKey],
    ]);
    $response = curl_exec($ch);
    curl_close($ch);
    return json_decode($response, true);
}

function stripe_expire(string $sessionId, string $secretKey): void {
    $ch = curl_init("https://api.stripe.com/v1/checkout/sessions/{$sessionId}/expire");
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_POST => true,
        CURLOPT_HTTPHEADER => ['Authorization: Bearer ' . $secretKey],
    ]);
    curl_exec($ch);
    curl_close($ch);
}

$failures = 0;
function check(bool $condition, string $description): void {
    global $failures;
    echo ($condition ? "PASS" : "FAIL") . ": {$description}\n";
    if (!$condition) $GLOBALS['failures']++;
}

/**
 * Walks a full leg journey exactly like checkout-continue.html would:
 * calls the endpoint once per leg, feeding back stripe_customer_id and
 * the shrinking remaining-legs list, collecting every created session
 * for verification + cleanup.
 */
function walk_journey(array $legs, string $email, string $secretKey, string $label): void {
    $stripeCustomerId = '';
    $sessions = [];
    $remaining = $legs;
    while (count($remaining) > 0) {
        $result = call_endpoint([
            'customer_email' => $email,
            'stripe_customer_id' => $stripeCustomerId,
            'legs' => $remaining,
        ]);
        check($result['status'] === 200, "{$label}: leg '{$remaining[0]['key']}' -- HTTP 200");
        if ($result['status'] !== 200) {
            echo "  response: " . json_encode($result['data']) . "\n";
            return;
        }
        $data = $result['data'];
        check(!empty($data['checkout_url']) && str_starts_with($data['checkout_url'], 'https://checkout.stripe.com/'), "{$label}: leg '{$remaining[0]['key']}' -- real checkout.stripe.com URL");
        $sessions[] = $data['session_id'];

        if ($stripeCustomerId === '') {
            check(!empty($data['stripe_customer_id']), "{$label}: first leg creates a Stripe Customer");
            $stripeCustomerId = $data['stripe_customer_id'];
        } else {
            check($data['stripe_customer_id'] === $stripeCustomerId, "{$label}: leg '{$remaining[0]['key']}' -- reuses the SAME Stripe Customer ({$stripeCustomerId})");
        }
        check($data['remaining_legs_count'] === count($remaining) - 1, "{$label}: leg '{$remaining[0]['key']}' -- remaining_legs_count is correct");

        $remaining = array_slice($remaining, 1);
    }

    // Verify every created session against the real Stripe API: livemode
    // false, correct price/mode, correct Stripe Customer, then expire.
    foreach ($sessions as $i => $sessionId) {
        $session = stripe_get("/v1/checkout/sessions/{$sessionId}", $secretKey);
        check($session['livemode'] === false, "{$label}: session {$i} livemode=false");
        check($session['customer'] === $stripeCustomerId, "{$label}: session {$i} attached to the reused Stripe Customer");
        check($session['mode'] === $legs[$i]['expected_mode'], "{$label}: session {$i} mode={$legs[$i]['expected_mode']} as expected");
        stripe_expire($sessionId, $secretKey);
    }
    echo "  {$label}: created and verified " . count($sessions) . " Checkout Session(s), all expired.\n\n";
}

// ---------------------------------------------------------- test cases

walk_journey([
    ['kind' => 'hardware', 'key' => 'AIC-APPLIANCE-RYZEN-STARTER', 'expected_mode' => 'payment'],
], 'sandbox-hw-only@example.test', $secretKey, 'Hardware only');

walk_journey([
    ['kind' => 'camera_plan', 'key' => 'local_1_8', 'expected_mode' => 'subscription'],
], 'sandbox-local-only@example.test', $secretKey, 'Local only');

walk_journey([
    ['kind' => 'camera_plan', 'key' => 'hybrid_1_8', 'expected_mode' => 'subscription'],
], 'sandbox-hybrid-only@example.test', $secretKey, 'Hybrid only');

walk_journey([
    ['kind' => 'analytics', 'key' => 'smart_motion', 'expected_mode' => 'subscription'],
], 'sandbox-analytics-only@example.test', $secretKey, 'One analytics only');

walk_journey([
    ['kind' => 'hardware', 'key' => 'AIC-APPLIANCE-RYZEN-STARTER', 'expected_mode' => 'payment'],
    ['kind' => 'camera_plan', 'key' => 'local_1_8', 'expected_mode' => 'subscription'],
    ['kind' => 'analytics', 'key' => 'smart_motion', 'expected_mode' => 'subscription'],
    ['kind' => 'analytics', 'key' => 'people_counting', 'expected_mode' => 'subscription'],
    ['kind' => 'analytics', 'key' => 'talk_down', 'expected_mode' => 'subscription'],
], 'sandbox-hw-local-3analytics@example.test', $secretKey, 'Hardware + Local + 3 analytics');

walk_journey([
    ['kind' => 'hardware', 'key' => 'AIC-APPLIANCE-RYZEN-ENTERPRISE', 'expected_mode' => 'payment'],
    ['kind' => 'hardware', 'key' => 'AIC-RELAY-NUMATO-3CH', 'expected_mode' => 'payment'],
    ['kind' => 'camera_plan', 'key' => 'hybrid_9_16', 'expected_mode' => 'subscription'],
    ['kind' => 'analytics', 'key' => 'lpr', 'expected_mode' => 'subscription'],
    ['kind' => 'analytics', 'key' => 'facial_recognition', 'expected_mode' => 'subscription'],
], 'sandbox-hw-hybrid-analytics@example.test', $secretKey, 'Hardware + relay + Hybrid + 2 analytics');

// -------------------------------------------------------- fail-closed

$unknownResult = call_endpoint(['customer_email' => 'x@example.test', 'legs' => [['kind' => 'analytics', 'key' => 'totally_unknown']]]);
check($unknownResult['status'] >= 400, 'unknown analytics key fails closed (non-200)');

$unknownCameraResult = call_endpoint(['customer_email' => 'x@example.test', 'legs' => [['kind' => 'camera_plan', 'key' => 'local_1_9000']]]);
check($unknownCameraResult['status'] >= 400, 'unknown camera plan key fails closed (non-200)');

echo "\n" . ($failures === 0 ? "ALL PASSED" : "{$failures} FAILURE(S)") . "\n";
exit($failures === 0 ? 0 : 1);
