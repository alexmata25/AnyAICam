<?php
declare(strict_types=1);

/**
 * STAGING/REVIEW COPY — NOT the live Bluehost file.
 *
 * This is stripe-checkout.php (live at OneDrive/Documents/anyaicam/
 * stripe-checkout.php) with four new entries added to $hardwareCatalog:
 * ryzen_starter, ryzen_enterprise, ryzen_aac_facial_recognition, and
 * numato_3_channel_relay. The existing $hardwareCatalog + normalize_
 * hardware_items() + hardware_items line-item loop already handles an
 * arbitrary SKU generically (see the unmodified code below), so these
 * four hardware products ride the exact same one-time `mode: payment`
 * checkout path every camera/PoE SKU already uses -- no new checkout
 * logic was needed for them. Requires staging/stripe-config.php (this
 * directory) to be require_once'd in place of the live stripe-config.php
 * when actually running this file -- see the one changed require_once
 * line below.
 *
 * One deliberate omission from the live file: the `$orderId !== ''`
 * partner-order MySQL verification block (config.php DB lookup, locked
 * order re-derivation) is left out here. It's orthogonal to these four
 * SKUs -- it exists to let a partner-assisted quote lock down what a
 * customer can actually check out for, and isn't exercised by the direct
 * Build-Your-System hardware-selection flow this staging pass targets.
 * Re-add it unchanged from the live file if/when this staging copy is
 * promoted and the partner-order path needs to cover these SKUs too.
 *
 * Every new SKU's price_id resolves to '' (see staging/stripe-config.php)
 * until a real Stripe TEST Price ID is created -- normalize_hardware_
 * items()/the line-item loop below already fall back to inline
 * price_data (see the existing camera fallback pattern a few lines down)
 * when price_id is empty, so this file is exercise-able in Stripe TEST
 * mode today without inventing a Price ID, exactly like a not-yet-
 * configured camera SKU already behaves live.
 */

ini_set('display_errors', '0');
ini_set('log_errors', '1');
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
ob_start();

function json_fail(string $message, int $status = 400): never {
    http_response_code($status);
    if (ob_get_length()) {
        ob_clean();
    }
    echo json_encode(['error' => $message], JSON_UNESCAPED_SLASHES);
    exit;
}

set_exception_handler(function (Throwable $e): void {
    error_log('stripe-checkout (staging) fatal: ' . $e->getMessage());
    json_fail('Stripe checkout server configuration error.', 500);
});

require_once __DIR__ . '/stripe-config.php'; // staging config -- see file header

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['error' => 'Method not allowed']);
    exit;
}

function fail(string $message, int $status = 400): never {
    json_fail($message, $status);
}

$input = json_decode(file_get_contents('php://input') ?: '{}', true);
if (!is_array($input)) {
    fail('Invalid JSON request.');
}

$adapterType = (string)($input['adapter_type'] ?? '');
$quantity = max(1, min(20, (int)($input['quantity'] ?? 1)));
$cameraQuantity = max(0, min(64, (int)($input['camera_quantity'] ?? 0)));
$requestedHardwareItems = is_array($input['hardware_items'] ?? null) ? $input['hardware_items'] : [];
$orderId = trim((string)($input['order_id'] ?? ''));
$quoteId = trim((string)($input['quote_id'] ?? ''));
$partnerId = trim((string)($input['partner_id'] ?? ''));
$customerEmail = trim((string)($input['customer_email'] ?? ''));
$analyticsMetadata = '';
$cameraUnitAmount = 34999;

function config_value(string $name, string $fallback = ''): string {
    return defined($name) ? trim((string)constant($name)) : $fallback;
}

if (config_value('STRIPE_SECRET_KEY') === '') {
    fail('Stripe secret key is not configured.', 500);
}
// Extracted to hardware-price-catalog.php (verbatim, zero behavior
// change) so checkout-session.php -- the new Local/Hybrid/analytics
// checkout endpoint -- can resolve the same appliance/relay SKUs
// without requiring this whole HTTP-handling file. See that file's own
// header for why.
$hardwareCatalog = require __DIR__ . '/hardware-price-catalog.php';
function normalize_hardware_items(array $requestedItems, array $catalog): array {
    $quantities = [];

    foreach ($requestedItems as $row) {
        if (!is_array($row)) continue;

        $sku = trim((string)($row['sku'] ?? ''));
        $qty = max(0, min(64, (int)($row['quantity'] ?? 0)));

        if ($sku === '' || $qty < 1 || !isset($catalog[$sku])) continue;
        $quantities[$sku] = min(64, ($quantities[$sku] ?? 0) + $qty);
    }

    $items = [];
    foreach ($quantities as $sku => $qty) {
        $items[] = [
            'sku' => $sku,
            'quantity' => $qty,
        ] + $catalog[$sku];
    }

    return $items;
}

$hardwareItems = normalize_hardware_items($requestedHardwareItems, $hardwareCatalog);


$priceMap = [
    'webhook_test' => config_value('WEBHOOK_TEST_PRICE_ID'),
    '8channel'  => config_value('ADAPTER_8CH_PRICE_ID'),
    '16channel' => config_value('ADAPTER_16CH_PRICE_ID'),
    '32channel' => config_value('ADAPTER_32CH_PRICE_ID'),
    '64channel' => config_value('ADAPTER_64CH_PRICE_ID'),
    'software' => '',
    'ownedadapter' => '',
];

if (!array_key_exists($adapterType, $priceMap)) {
    fail('Invalid adapter selection.');
}
if ($priceMap[$adapterType] === '' && $cameraQuantity < 1 && count($hardwareItems) < 1) {
    fail('No payable hardware was selected.');
}
if ($customerEmail !== '' && !filter_var($customerEmail, FILTER_VALIDATE_EMAIL)) {
    fail('Invalid customer email.');
}
if ($orderId !== '' && !preg_match('/^AIC-O-[A-Z0-9-]+$/', $orderId)) {
    fail('Invalid order ID.');
}

$baseUrl = defined('BASE_URL') ? rtrim(BASE_URL, '/') : 'https://anyaicam.com';
$successParams = [];
if ($orderId !== '') {
    $successParams['order_id'] = $orderId;
} elseif ($quoteId !== '') {
    $successParams['quote_id'] = $quoteId;
}
$successUrl = $baseUrl . '/stripe-success.php' . ($successParams ? '?' . http_build_query($successParams) : '');
$cancelUrl = $baseUrl . '/customer-checkout.html?checkout=cancel';

$postFields = [
    'mode' => 'payment',
    'success_url' => $successUrl,
    'cancel_url' => $cancelUrl,
    'client_reference_id' => $orderId !== '' ? $orderId : ($quoteId !== '' ? $quoteId : 'ANYAICAM'),
    'metadata[order_id]' => $orderId,
    'metadata[quote_id]' => $quoteId,
    'metadata[partner_id]' => $partnerId,
    'metadata[adapter_type]' => $adapterType,
    'metadata[analytics_quantities]' => $analyticsMetadata,
    'metadata[camera_quantity]' => (string)$cameraQuantity,
    'metadata[camera_warranty]' => '1 year',
    'metadata[hardware_items]' => substr(
        implode(',', array_map(
            static fn(array $item): string => $item['sku'] . ':' . $item['quantity'],
            $hardwareItems
        )),
        0,
        450
    ),
];

$nextLineItemIndex = 0;
if ($priceMap[$adapterType] !== '') {
    $postFields["line_items[$nextLineItemIndex][price]"] = $priceMap[$adapterType];
    $postFields["line_items[$nextLineItemIndex][quantity]"] = (string)$quantity;
    $nextLineItemIndex++;
}

if ($cameraQuantity > 0 && count($hardwareItems) === 0) {
    $cameraIndex = $nextLineItemIndex;
    if (config_value('CAMERA_5MP_DOME_PRICE_ID') !== '') {
        $postFields["line_items[$cameraIndex][price]"] = config_value('CAMERA_5MP_DOME_PRICE_ID');
    } else {
        $postFields["line_items[$cameraIndex][price_data][currency]"] = 'usd';
        $postFields["line_items[$cameraIndex][price_data][unit_amount]"] = (string)$cameraUnitAmount;
        $postFields["line_items[$cameraIndex][price_data][product_data][name]"] = 'ANY AI CAM 5MP Outdoor Fixed Dome AI Camera';
        $postFields["line_items[$cameraIndex][price_data][product_data][description]"] = 'VIVOTEK FD9380-HTV-V2';
    }
    $postFields["line_items[$cameraIndex][quantity]"] = (string)$cameraQuantity;
}


foreach ($hardwareItems as $item) {
    $i=$nextLineItemIndex++;
    if (!empty($item['price_id'])) {
        $postFields["line_items[$i][price]"]=(string)$item['price_id'];
    } else {
        $postFields["line_items[$i][price_data][currency]"]='usd';
        $postFields["line_items[$i][price_data][unit_amount]"]=(string)$item['unit_amount'];
        $postFields["line_items[$i][price_data][product_data][name]"]=$item['name'];
        $postFields["line_items[$i][price_data][product_data][metadata][sku]"]=$item['sku'];
    }
    $postFields["line_items[$i][quantity]"]=(string)$item['quantity'];
}

if ($customerEmail !== '') {
    $postFields['customer_email'] = $customerEmail;
    $postFields['payment_intent_data[receipt_email]'] = $customerEmail;
}

$ch = curl_init();
curl_setopt_array($ch, [
    CURLOPT_URL => 'https://api.stripe.com/v1/checkout/sessions',
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_POST => true,
    CURLOPT_CONNECTTIMEOUT => 10,
    CURLOPT_TIMEOUT => 30,
    CURLOPT_HTTPHEADER => [
        'Authorization: Bearer ' . config_value('STRIPE_SECRET_KEY'),
        'Content-Type: application/x-www-form-urlencoded',
    ],
    CURLOPT_POSTFIELDS => http_build_query($postFields),
]);

$response = curl_exec($ch);
$curlError = curl_error($ch);
$httpCode = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
curl_close($ch);

if ($response === false || $curlError !== '') {
    error_log('Stripe cURL error: ' . $curlError);
    fail('Stripe could not be reached.', 502);
}

$data = json_decode($response, true);
if ($httpCode < 200 || $httpCode >= 300 || !is_array($data) || empty($data['url'])) {
    error_log('Stripe session creation failed: HTTP ' . $httpCode . ' - ' . $response);

    $stripeMessage = 'Failed to create Stripe Checkout Session.';
    if (is_array($data) && isset($data['error']) && is_array($data['error'])) {
        $message = trim((string)($data['error']['message'] ?? ''));
        $parameter = trim((string)($data['error']['param'] ?? ''));
        $type = trim((string)($data['error']['type'] ?? ''));

        if ($message !== '') {
            $stripeMessage = $message;
        }
        if ($parameter !== '') {
            $stripeMessage .= ' [Parameter: ' . $parameter . ']';
        }
        if ($type !== '') {
            $stripeMessage .= ' [' . $type . ']';
        }
    } elseif ($response !== '') {
        $stripeMessage = 'Stripe returned an unreadable response. Check the PHP error log.';
    }

    fail($stripeMessage, 502);
}

if (ob_get_length()) {
    ob_clean();
}
echo json_encode([
    'url' => $data['url'],
], JSON_UNESCAPED_SLASHES);
