<?php
declare(strict_types=1);

/**
 * STAGING/REVIEW COPY -- NOT the live Bluehost file. New file: no live
 * equivalent exists yet. Creates exactly ONE Stripe Checkout Session per
 * call, for the FIRST leg of whatever "legs" list is posted -- see
 * checkout-catalog.php's header for why a cart with multiple selections
 * (appliance + camera plan + several analytics) becomes a sequence of
 * single-Price-ID Checkout Sessions rather than one multi-item session.
 *
 * Called twice over the lifetime of one customer journey:
 *  1. From the Build Your System page, with the full cart (server
 *     normalizes it into a leg list via checkout-catalog.php).
 *  2. From checkout-continue.html, after each leg's Stripe redirect
 *     lands back, with whatever legs remain.
 *
 * One Stripe Customer reused across the whole journey (task requirement):
 * the first call (no stripe_customer_id yet) creates a real Stripe
 * Customer object once via POST /v1/customers and returns its id in the
 * response; every subsequent call passes that same id back in, and this
 * file always attaches `customer=<that id>` to the Checkout Session
 * instead of `customer_email`, so Stripe never creates a second Customer
 * object for the same journey the way the very first sandbox purchase
 * test did (four separate subscription checkouts, four separate ad-hoc
 * Stripe Customers).
 *
 * Metadata contract preserved exactly, never invented:
 *  - hardware leg: anyaicam_stripe_price_id, anyaicam_hardware_sku,
 *    anyaicam_hardware_quantity -- matches hardware_orders.py's
 *    sync_hardware_order_from_stripe_event()/_extract fields exactly,
 *    same as the existing /api/payments/hardware-checkout endpoint.
 *  - camera_plan / analytics legs: anyaicam_stripe_price_id only --
 *    matches customer_entitlements.py's and analytics_entitlements.py's
 *    _extract_checkout_fields() exactly. No anyaicam_customer_id is set
 *    (this is an anonymous storefront journey, not an authenticated VMS
 *    session) -- both modules' checkout-before-registration pending-link
 *    fallback (keyed by customer_details.email) is what attributes the
 *    purchase once the customer's AnyAiCam account exists/is approved.
 *
 * SANDBOX/TEST ONLY: refuses to run against a live-mode secret key (see
 * the explicit check below) so this file can never accidentally create a
 * real charge even if staging/stripe-config.php were ever misconfigured.
 */

ini_set('display_errors', '0');
ini_set('log_errors', '1');
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
ob_start();

function json_fail_checkout(string $message, int $status = 400): never {
    http_response_code($status);
    if (ob_get_length()) {
        ob_clean();
    }
    echo json_encode(['error' => $message], JSON_UNESCAPED_SLASHES);
    exit;
}

set_exception_handler(function (Throwable $e): void {
    error_log('checkout-session (staging) fatal: ' . $e->getMessage());
    json_fail_checkout('Checkout server configuration error.', 500);
});

require_once __DIR__ . '/checkout-catalog.php';

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['error' => 'Method not allowed']);
    exit;
}

$secretKey = config_value_checked('STRIPE_SECRET_KEY');
if ($secretKey === '') {
    json_fail_checkout('Stripe secret key is not configured.', 500);
}
// Hard refusal, not a soft warning: this file must never run against a
// live-mode key. sk_live_ is the only prefix a real live secret key
// uses; sk_test_ (and Stripe's newer restricted-key prefixes for test
// mode) never match this check.
if (str_starts_with($secretKey, 'sk_live_')) {
    error_log('checkout-session (staging) refused: STRIPE_SECRET_KEY looks like a LIVE key.');
    json_fail_checkout('Refusing to run: this staging endpoint requires a Stripe TEST secret key.', 500);
}

// Offline tests only (2026-10-04): tests/run-offline-tests.sh points this at a
// fake Stripe on loopback. Anything that is not a 127.0.0.1 URL is ignored, so a
// deployed storefront always talks to api.stripe.com.
function stripe_api_base(): string {
    $override = (string)(getenv('ANYAICAM_STOREFRONT_TEST_STRIPE_BASE') ?: '');
    return preg_match('#^http://127\.0\.0\.1:[0-9]{2,5}$#', $override) ? $override : 'https://api.stripe.com';
}

function stripe_api_call(string $method, string $path, array $fields, string $secretKey, string $idempotencyKey = ''): array {
    $ch = curl_init();
    $url = stripe_api_base() . $path;
    $headers = [
        'Authorization: Bearer ' . $secretKey,
        'Content-Type: application/x-www-form-urlencoded',
    ];
    if ($idempotencyKey !== '') {
        // Stripe answers a repeated key with the first result: a double click
        // gets the same Customer / Checkout Session, never a second payable one.
        $headers[] = 'Idempotency-Key: ' . $idempotencyKey;
    }
    $opts = [
        CURLOPT_URL => $url,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_CONNECTTIMEOUT => 10,
        CURLOPT_TIMEOUT => 30,
        CURLOPT_HTTPHEADER => $headers,
    ];
    if ($method === 'POST') {
        $opts[CURLOPT_POST] = true;
        $opts[CURLOPT_POSTFIELDS] = http_build_query($fields);
    }
    curl_setopt_array($ch, $opts);
    $response = curl_exec($ch);
    $curlError = curl_error($ch);
    $httpCode = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);

    if ($response === false || $curlError !== '') {
        error_log('Stripe cURL error: ' . $curlError);
        json_fail_checkout('Stripe could not be reached.', 502);
    }
    $data = json_decode($response, true);
    if ($httpCode < 200 || $httpCode >= 300 || !is_array($data)) {
        error_log('Stripe API call failed: HTTP ' . $httpCode . ' - ' . $response);
        $message = 'Stripe request failed.';
        if (is_array($data) && isset($data['error']['message'])) {
            $message = (string)$data['error']['message'];
        }
        json_fail_checkout($message, 502);
    }
    return $data;
}

$input = json_decode(file_get_contents('php://input') ?: '{}', true);
if (!is_array($input)) {
    json_fail_checkout('Invalid JSON request.');
}

$customerEmail = trim((string)($input['customer_email'] ?? ''));
if ($customerEmail !== '' && !filter_var($customerEmail, FILTER_VALIDATE_EMAIL)) {
    json_fail_checkout('Invalid customer email.');
}
$stripeCustomerId = trim((string)($input['stripe_customer_id'] ?? ''));
if ($stripeCustomerId !== '' && !preg_match('/^cus_[A-Za-z0-9]{6,64}$/', $stripeCustomerId)) {
    json_fail_checkout('Invalid checkout state. Please start again from Build Your System.');
}
// One id per Build Your System visit (generated by the page, carried through
// checkout-continue.html). Every Stripe write of this journey derives its
// Idempotency-Key from it, so a double click or a repeated continue request
// gets the same Checkout Session back instead of a second payable one.
$journeyId = trim((string)($input['journey_id'] ?? ''));
if (!preg_match('/^[A-Za-z0-9_-]{16,64}$/', $journeyId)) {
    json_fail_checkout('This checkout page is out of date. Please reload Build Your System and try again.');
}
$legs = is_array($input['legs'] ?? null) ? $input['legs'] : [];
if (count($legs) < 1) {
    json_fail_checkout('No checkout legs supplied.');
}

// Re-validate every leg's price_id server-side against the real catalog
// constants -- never trust a price_id string round-tripped through the
// client's own query string as authoritative on its own. A leg is only
// ever accepted if its price_id matches what THIS server would resolve
// for its own (kind, key) right now.
$hardwareCatalog = require __DIR__ . '/hardware-price-catalog.php';
function verified_leg(array $leg, array $hardwareCatalog): array {
    $kind = (string)($leg['kind'] ?? '');
    $key = (string)($leg['key'] ?? '');
    if ($kind === 'hardware') {
        if (!isset($hardwareCatalog[$key]) || !in_array($key, SELLABLE_HARDWARE_SKUS, true)) {
            json_fail_checkout('Unknown hardware selection in checkout leg.');
        }
        $item = $hardwareCatalog[$key];
        if (empty($item['price_id'])) {
            json_fail_checkout("PRICE_ID_REQUIRED: no Stripe Price ID configured for {$key}.", 503);
        }
        return ['kind' => 'hardware', 'key' => $key, 'label' => $item['name'], 'price_id' => $item['price_id'], 'mode' => 'payment',
                'amount_cents' => (int)$item['unit_amount']];
    }
    if ($kind === 'camera_plan') {
        $resolved = resolve_camera_plan($key);
        if ($resolved === null) {
            json_fail_checkout('Unknown or unconfigured camera-slot plan in checkout leg.');
        }
        return ['kind' => 'camera_plan', 'key' => $key, 'label' => $resolved['label'], 'price_id' => $resolved['price_id'], 'mode' => 'subscription',
                'amount_cents' => (int)$resolved['amount_cents']];
    }
    if ($kind === 'analytics') {
        $resolved = resolve_analytic($key);
        if ($resolved === null) {
            json_fail_checkout('Unknown or unconfigured analytics selection in checkout leg.');
        }
        return ['kind' => 'analytics', 'key' => $key, 'label' => $resolved['label'], 'price_id' => $resolved['price_id'], 'mode' => 'subscription',
                'amount_cents' => (int)$resolved['amount_cents']];
    }
    if ($kind === 'vms_license') {
        $resolved = resolve_vms_license((int)$key);
        if ($resolved === null) {
            json_fail_checkout('Unknown or unconfigured VMS software license in checkout leg.');
        }
        return ['kind' => 'vms_license', 'key' => $key, 'label' => $resolved['label'], 'price_id' => $resolved['price_id'], 'mode' => 'payment',
                'amount_cents' => (int)$resolved['amount_cents']];
    }
    json_fail_checkout('Unknown checkout leg kind.');
}

$currentLeg = verified_leg($legs[0], $hardwareCatalog);
$remainingLegs = array_slice($legs, 1);

// Price guard (2026-10-04): the Stripe Price must charge exactly the published
// catalog amount, in USD, active, one-time or monthly as the leg requires.
// Anything else -- including a Price Stripe does not know -- refuses checkout.
$stripePrice = stripe_api_call('GET', '/v1/prices/' . rawurlencode($currentLeg['price_id']), [], $secretKey);
$priceProblem = stripe_price_problem($stripePrice, $currentLeg['price_id'], $currentLeg['amount_cents'], $currentLeg['mode']);
if ($priceProblem !== null) {
    error_log("checkout-session (staging) PRICE_MISMATCH for {$currentLeg['kind']} {$currentLeg['key']}: {$priceProblem}");
    json_fail_checkout('PRICE_MISMATCH: this item\'s Stripe price does not match the published price, so checkout is paused.', 503);
}

// One Stripe Customer reused across the whole journey.
if ($stripeCustomerId === '' && $customerEmail !== '') {
    $customer = stripe_api_call('POST', '/v1/customers', ['email' => $customerEmail], $secretKey,
        'storefront-customer-' . hash('sha256', $journeyId . '|' . strtolower($customerEmail)));
    $stripeCustomerId = (string)($customer['id'] ?? '');
}

$baseUrl = defined('BASE_URL') ? rtrim(BASE_URL, '/') : 'https://anyaicam.com';
$continueParams = [
    'journey_id' => $journeyId,
    'customer_email' => $customerEmail,
    'stripe_customer_id' => $stripeCustomerId,
    'remaining_legs' => base64_encode(json_encode($remainingLegs, JSON_UNESCAPED_SLASHES)),
    'completed_leg_label' => $currentLeg['label'],
    'session_id' => '{CHECKOUT_SESSION_ID}',
];
// success_url's {CHECKOUT_SESSION_ID} placeholder must reach Stripe
// un-encoded (Stripe substitutes it literally) -- build the query string
// by hand for that one field instead of running the whole thing through
// http_build_query(), which would percent-encode the braces.
$successQuery = http_build_query(array_diff_key($continueParams, ['session_id' => null]))
    . '&session_id={CHECKOUT_SESSION_ID}';
$successUrl = $baseUrl . '/staging/checkout-continue.html?' . $successQuery;
$cancelUrl = $baseUrl . '/staging/build-your-system.html?checkout=cancelled';

$fields = [
    'mode' => $currentLeg['mode'],
    'success_url' => $successUrl,
    'cancel_url' => $cancelUrl,
    'line_items[0][price]' => $currentLeg['price_id'],
    'line_items[0][quantity]' => '1',
    'metadata[anyaicam_stripe_price_id]' => $currentLeg['price_id'],
];
if ($currentLeg['kind'] === 'vms_license') {
    $fields['metadata[anyaicam_product_class]'] = 'vms_license';
    $fields['metadata[anyaicam_vms_license_capacity]'] = $currentLeg['key'];
}
if ($currentLeg['kind'] === 'hardware') {
    $fields['metadata[anyaicam_hardware_sku]'] = $currentLeg['key'];
    $fields['metadata[anyaicam_hardware_quantity]'] = '1';
}
if ($stripeCustomerId !== '') {
    $fields['customer'] = $stripeCustomerId;
} elseif ($customerEmail !== '') {
    $fields['customer_email'] = $customerEmail;
}

$session = stripe_api_call('POST', '/v1/checkout/sessions', $fields, $secretKey,
    'storefront-session-' . hash('sha256', implode('|', [$journeyId, $currentLeg['kind'], $currentLeg['key'],
                                                         $stripeCustomerId, strtolower($customerEmail), (string)count($remainingLegs)])));
$sessionId = (string)($session['id'] ?? '');
$checkoutUrl = (string)($session['url'] ?? '');
if ($sessionId === '' || $checkoutUrl === '') {
    json_fail_checkout('Stripe did not return a Checkout Session URL.', 502);
}

if (ob_get_length()) {
    ob_clean();
}
echo json_encode([
    'checkout_url' => $checkoutUrl,
    'session_id' => $sessionId,
    'stripe_customer_id' => $stripeCustomerId,
    'leg' => ['kind' => $currentLeg['kind'], 'key' => $currentLeg['key'], 'label' => $currentLeg['label']],
    'remaining_legs_count' => count($remainingLegs),
], JSON_UNESCAPED_SLASHES);
