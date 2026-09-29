<?php
declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

/*
 * AnyAiCam VMS Software TEST Checkout
 * Stripe Sandbox only
 */

$configFile = dirname(__DIR__) . '/anyaicam_private/config.php';

if (!is_file($configFile)) {
    http_response_code(500);
    echo json_encode(['error' => 'Private configuration file not found.']);
    exit;
}

require_once $configFile;

/*
 * Safety check — NEVER allow a live Stripe key here.
 */
if (
    !defined('STRIPE_MODE') ||
    STRIPE_MODE !== 'test' ||
    !defined('STRIPE_SECRET_KEY') ||
    strpos(STRIPE_SECRET_KEY, 'sk_test_') !== 0
) {
    http_response_code(500);
    echo json_encode(['error' => 'Stripe TEST configuration is invalid.']);
    exit;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['error' => 'POST required.']);
    exit;
}

/*
 * We will add this $1 recurring Stripe test price
 * to the private config next.
 */
if (!defined('AIC_VMS_SOFTWARE_TEST')) {
    http_response_code(500);
    echo json_encode(['error' => 'VMS software test price is not configured.']);
    exit;
}

$baseUrl = defined('AIC_BASE_URL')
    ? AIC_BASE_URL
    : 'https://anyaicam.com';

$postFields = [
    'mode' => 'subscription',

    'success_url' =>
        $baseUrl . '/build-your-system.html?checkout=test-success',

    'cancel_url' =>
        $baseUrl . '/build-your-system.html?checkout=test-cancel',

    'line_items[0][price]' =>
        AIC_VMS_SOFTWARE_TEST,

    'line_items[0][quantity]' => 1,

    'metadata[anyaicam_flow]' =>
        'vms_software_test',

    'metadata[environment]' =>
        'test'
];

/*
 * Create Stripe Checkout Session.
 */
$ch = curl_init();

curl_setopt_array($ch, [
    CURLOPT_URL =>
        'https://api.stripe.com/v1/checkout/sessions',

    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_POST => true,
    CURLOPT_CONNECTTIMEOUT => 10,
    CURLOPT_TIMEOUT => 30,

    CURLOPT_HTTPHEADER => [
        'Authorization: Bearer ' . STRIPE_SECRET_KEY,
        'Content-Type: application/x-www-form-urlencoded'
    ],

    CURLOPT_POSTFIELDS =>
        http_build_query($postFields)
]);

$response = curl_exec($ch);
$curlError = curl_error($ch);
$httpCode = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);

curl_close($ch);

if ($response === false || $curlError !== '') {
    http_response_code(502);
    echo json_encode(['error' => 'Unable to reach Stripe.']);
    exit;
}

$data = json_decode($response, true);

if (
    $httpCode < 200 ||
    $httpCode >= 300 ||
    !is_array($data) ||
    empty($data['url'])
) {
    http_response_code(502);

    echo json_encode([
        'error' => 'Stripe Checkout Session could not be created.',
        'stripe_status' => $httpCode
    ]);

    exit;
}

echo json_encode([
    'ok' => true,
    'environment' => 'test',
    'url' => $data['url']
], JSON_UNESCAPED_SLASHES);