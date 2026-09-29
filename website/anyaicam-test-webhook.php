<?php
declare(strict_types=1);

/*
 * AnyAiCam VMS Software TEST Webhook
 * Stripe Sandbox only
 */

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

$configFile = dirname(__DIR__) . '/anyaicam_private/config.php';

if (!is_file($configFile)) {
    http_response_code(500);
    echo json_encode(['error' => 'Private configuration not found.']);
    exit;
}

require_once $configFile;

/*
 * TEST MODE SAFETY
 */
if (
    !defined('STRIPE_MODE') ||
    STRIPE_MODE !== 'test' ||
    !defined('STRIPE_SECRET_KEY') ||
    strpos(STRIPE_SECRET_KEY, 'sk_test_') !== 0 ||
    !defined('STRIPE_WEBHOOK_SECRET') ||
    strpos(STRIPE_WEBHOOK_SECRET, 'whsec_') !== 0
) {
    http_response_code(500);
    echo json_encode(['error' => 'Stripe TEST webhook configuration is invalid.']);
    exit;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['error' => 'POST required.']);
    exit;
}

$payload = file_get_contents('php://input');

if ($payload === false || $payload === '') {
    http_response_code(400);
    echo json_encode(['error' => 'Empty webhook payload.']);
    exit;
}

$signatureHeader = $_SERVER['HTTP_STRIPE_SIGNATURE'] ?? '';

if ($signatureHeader === '') {
    http_response_code(400);
    echo json_encode(['error' => 'Missing Stripe signature.']);
    exit;
}

/*
 * Parse Stripe-Signature header.
 */
$timestamp = null;
$signatures = [];

foreach (explode(',', $signatureHeader) as $part) {

    $pieces = explode('=', trim($part), 2);

    if (count($pieces) !== 2) {
        continue;
    }

    [$key, $value] = $pieces;

    if ($key === 't') {
        $timestamp = $value;
    }

    if ($key === 'v1') {
        $signatures[] = $value;
    }
}

if ($timestamp === null || empty($signatures)) {
    http_response_code(400);
    echo json_encode(['error' => 'Invalid Stripe signature header.']);
    exit;
}

/*
 * Protect against old/replayed webhook requests.
 * Allow 5 minutes.
 */
if (abs(time() - (int)$timestamp) > 300) {
    http_response_code(400);
    echo json_encode(['error' => 'Webhook timestamp outside tolerance.']);
    exit;
}

/*
 * Verify Stripe webhook signature.
 */
$signedPayload = $timestamp . '.' . $payload;

$expectedSignature = hash_hmac(
    'sha256',
    $signedPayload,
    STRIPE_WEBHOOK_SECRET
);

$signatureValid = false;

foreach ($signatures as $signature) {

    if (hash_equals($expectedSignature, $signature)) {
        $signatureValid = true;
        break;
    }
}

if (!$signatureValid) {
    http_response_code(400);
    echo json_encode(['error' => 'Webhook signature verification failed.']);
    exit;
}

/*
 * Decode verified Stripe event.
 */
$event = json_decode($payload, true);

if (!is_array($event)) {
    http_response_code(400);
    echo json_encode(['error' => 'Invalid Stripe event JSON.']);
    exit;
}

/*
 * NEVER accept LIVE Stripe events on this endpoint.
 */
if (($event['livemode'] ?? true) !== false) {
    http_response_code(400);
    echo json_encode(['error' => 'Live Stripe events are not allowed here.']);
    exit;
}

$eventId   = (string)($event['id'] ?? '');
$eventType = (string)($event['type'] ?? '');

$allowedEvents = [
    'checkout.session.completed',
    'checkout.session.async_payment_succeeded'
];

/*
 * Ignore unrelated verified Sandbox events.
 */
if (!in_array($eventType, $allowedEvents, true)) {

    http_response_code(200);

    echo json_encode([
        'received' => true,
        'ignored' => true
    ]);

    exit;
}

$session = $event['data']['object'] ?? [];

if (!is_array($session)) {
    http_response_code(400);
    echo json_encode(['error' => 'Checkout session missing.']);
    exit;
}

/*
 * Verify this belongs to our AnyAiCam VMS TEST flow.
 */
$metadata = $session['metadata'] ?? [];

if (
    !is_array($metadata) ||
    ($metadata['anyaicam_flow'] ?? '') !== 'vms_software_test' ||
    ($metadata['environment'] ?? '') !== 'test'
) {
    http_response_code(200);

    echo json_encode([
        'received' => true,
        'ignored' => true
    ]);

    exit;
}

/*
 * For this first test we DO NOT provision cameras,
 * activate AWS resources, or create production accounts.
 *
 * We only record that a valid signed Sandbox payment
 * event reached our server successfully.
 */
$privateDir = dirname(__DIR__) . '/anyaicam_private';

$logFile = $privateDir . '/vms-test-webhook.log';

$logEntry = [
    'received_at' => gmdate('c'),
    'event_id' => $eventId,
    'event_type' => $eventType,
    'checkout_session_id' => (string)($session['id'] ?? ''),
    'payment_status' => (string)($session['payment_status'] ?? ''),
    'customer_id' => (string)($session['customer'] ?? ''),
    'subscription_id' => (string)($session['subscription'] ?? ''),
];

file_put_contents(
    $logFile,
    json_encode($logEntry, JSON_UNESCAPED_SLASHES) . PHP_EOL,
    FILE_APPEND | LOCK_EX
);

http_response_code(200);

echo json_encode([
    'received' => true,
    'environment' => 'test'
]);