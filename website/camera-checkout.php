<?php
declare(strict_types=1);
header('Content-Type: application/json');

if (!is_file(__DIR__ . '/stripe-config.php')) {
    http_response_code(500);
    echo json_encode(['error' => 'Stripe configuration is unavailable.']);
    exit;
}
require_once __DIR__ . '/stripe-config.php';

if (!defined('STRIPE_SECRET_KEY') || STRIPE_SECRET_KEY === '') {
    http_response_code(500);
    echo json_encode(['error' => 'Stripe is not configured.']);
    exit;
}

$payload = json_decode((string)file_get_contents('php://input'), true);
$quantity = isset($payload['quantity']) ? (int)$payload['quantity'] : 1;
$quantity = max(1, min(64, $quantity));

$scheme = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') ? 'https' : 'http';
$host = $_SERVER['HTTP_HOST'] ?? 'www.anyaicam.com';
$basePath = rtrim(str_replace('\\', '/', dirname($_SERVER['SCRIPT_NAME'] ?? '/')), '/');
$baseUrl = $scheme . '://' . $host . ($basePath ? $basePath : '');

$postFields = [
    'mode' => 'payment',
    'success_url' => $baseUrl . '/payment-success.php?product=camera&session_id={CHECKOUT_SESSION_ID}',
    'cancel_url' => $baseUrl . '/cameras.html?checkout=cancelled',
    'billing_address_collection' => 'required',
    'shipping_address_collection[allowed_countries][0]' => 'US',
    'line_items[0][quantity]' => (string)$quantity,
    'line_items[0][price_data][currency]' => 'usd',
    'line_items[0][price_data][unit_amount]' => '19900',
    'line_items[0][price_data][product_data][name]' => 'ANY AI CAM 4MP Commercial Dome Camera',
    'line_items[0][price_data][product_data][description]' => '4MP PoE commercial dome camera with 2.8 mm lens',
    'metadata[order_type]' => 'camera_hardware',
    'metadata[camera_quantity]' => (string)$quantity
];

$ch = curl_init('https://api.stripe.com/v1/checkout/sessions');
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_POST => true,
    CURLOPT_POSTFIELDS => http_build_query($postFields),
    CURLOPT_HTTPHEADER => [
        'Authorization: Bearer ' . STRIPE_SECRET_KEY,
        'Content-Type: application/x-www-form-urlencoded'
    ],
    CURLOPT_TIMEOUT => 30
]);

$response = curl_exec($ch);
$status = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
$error = curl_error($ch);
curl_close($ch);

if ($response === false || $error !== '') {
    http_response_code(502);
    echo json_encode(['error' => 'Unable to contact Stripe.']);
    exit;
}

$data = json_decode($response, true);
if ($status < 200 || $status >= 300 || empty($data['url'])) {
    http_response_code(502);
    echo json_encode(['error' => $data['error']['message'] ?? 'Stripe checkout could not be created.']);
    exit;
}

echo json_encode(['url' => $data['url']]);
