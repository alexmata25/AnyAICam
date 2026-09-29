<?php
declare(strict_types=1);

/**
 * Phase 5: Create a Stripe Checkout Session and attach the internal order ID.
 * This replaces static Stripe Payment Links for adapter purchases.
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
    error_log('stripe-checkout fatal: ' . $e->getMessage());
    json_fail('Stripe checkout server configuration error.', 500);
});

require_once __DIR__ . '/stripe-config.php';

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
$hardwareCatalog = [
 'AIC-CAM-LTS-LXIP1142'=>['name'=>'LTS LXIP1142W-28MA - Pro-X 4MP Starlight Turret IP Camera','unit_amount'=>11499,'price_id'=>config_value('LTS_LXIP1142_PRICE_ID')],
 'AIC-CAM-LTS-CMIP3382'=>['name'=>'LTS CMIP3382WI-28SDL - Platinum Plus 8MP Active-Deterrence Turret','unit_amount'=>24999,'price_id'=>config_value('LTS_CMIP3382_PRICE_ID')],
 'AIC-CAM-LTS-CMIP3C42'=>['name'=>'LTS CMIP3C42WI-28SDL - 4MP Color 24/7 Active-Deterrence Turret','unit_amount'=>29999,'price_id'=>config_value('LTS_CMIP3C42_PRICE_ID')],
 'AIC-CAM-LTS-CMIP3C82'=>['name'=>'LTS CMIP3C82WI-28SDL - 8MP Color 24/7 Active-Deterrence Turret','unit_amount'=>40999,'price_id'=>config_value('LTS_CMIP3C82_PRICE_ID')],
 'AIC-CAM-LTS-CMHT1722'=>['name'=>'LTS CMHT1722-28LS - 2MP HD-TVI Active-Deterrence Turret','unit_amount'=>11499,'price_id'=>config_value('LTS_CMHT1722_PRICE_ID')],
 'AIC-CAM-LTS-CMHT1752'=>['name'=>'LTS CMHT1752-28LS - 5MP HD-TVI Dual-Light Turret','unit_amount'=>12999,'price_id'=>config_value('LTS_CMHT1752_PRICE_ID')],
 'AIC-CAM-LTS-CMHT1782'=>['name'=>'LTS CMHT1782-28LF - Platinum Plus 8MP HD-TVI Turret','unit_amount'=>14999,'price_id'=>config_value('LTS_CMHT1782_PRICE_ID')],
 'AIC-POE-LTS-8'=>['name'=>'LTS VSPOE-SW802 - 8-Port PoE Switch with 2 Uplink Ports','unit_amount'=>15999,'price_id'=>config_value('LTS_POE_SW802_PRICE_ID')],
 'AIC-POE-LTS-16'=>['name'=>'LTS VSPOE-SW1602 - 16-Port PoE Switch with 2 Combo Ports','unit_amount'=>32999,'price_id'=>config_value('LTS_POE_SW1602_PRICE_ID')],
 'AIC-POE-LTS-24'=>['name'=>'LTS VSPOE-SW2402 - Pro-VS 24-Port PoE Switch with 2 Combo Ports','unit_amount'=>42999,'price_id'=>config_value('LTS_POE_SW2402_PRICE_ID')],
 'AIC-CAM-5MP-DOME-001'=>['name'=>'VIVOTEK FD9380-HTV-V2 5MP Outdoor Dome AI Camera','unit_amount'=>34999,'price_id'=>config_value('CAMERA_5MP_DOME_PRICE_ID')],
 'AIC-CAM-BULLET-IB9380'=>['name'=>'VIVOTEK IB9380-HTV-V2 5MP Outdoor Bullet AI Camera','unit_amount'=>34999,'price_id'=>config_value('CAMERA_5MP_BULLET_PRICE_ID')],
 'AIC-CAM-FISHEYE-FE9380'=>['name'=>'VIVOTEK FE9380-HV 5MP Fisheye Panoramic Camera','unit_amount'=>64999,'price_id'=>config_value('CAMERA_5MP_FISHEYE_PRICE_ID')],
 'AIC-CAM-DUAL-MA9312'=>['name'=>'VIVOTEK MA9312-EHTV Dual-Directional 4K AI Camera','unit_amount'=>179999,'price_id'=>config_value('CAMERA_DUAL_LENS_PRICE_ID')],
 'AIC-POE-MOKER-8'=>['name'=>'MokerLink POE-F082G 8-Port PoE Switch with 2 Gigabit Uplinks','unit_amount'=>7999,'price_id'=>config_value('POE_8_PORT_PRICE_ID')],
 'AIC-POE-MOKER-16'=>['name'=>'MokerLink POE-G162G 16-Port Gigabit PoE+ Switch with 2 Gigabit Uplinks','unit_amount'=>17499,'price_id'=>config_value('POE_16_PORT_PRICE_ID')],
 'AIC-POE-MOKER-24'=>['name'=>'MokerLink POE-G244GS 24-Port Gigabit PoE+ Switch with 2 Ethernet and 2 SFP Uplinks','unit_amount'=>22999,'price_id'=>config_value('POE_24_PORT_PRICE_ID')],
 'AIC-POE-MOKER-48'=>['name'=>'MokerLink POE-G482GS 48-Port Gigabit PoE Switch with 2 SFP Uplinks','unit_amount'=>42999,'price_id'=>config_value('POE_48_PORT_PRICE_ID')],
];
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

// When this is a partner-assisted database order, verify that it exists.
if ($orderId !== '') {
    $dbConfigFile = __DIR__ . '/config.php';
    if (!is_file($dbConfigFile)) {
        fail('Database configuration is missing.', 500);
    }
    $db = require $dbConfigFile;
    try {
        $pdo = new PDO(
            'mysql:host=' . $db['db_host'] . ';dbname=' . $db['db_name'] . ';charset=utf8mb4',
            $db['db_user'],
            $db['db_pass'],
            [
                PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
                PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
                PDO::ATTR_EMULATE_PREPARES => false,
            ]
        );
        $stmt = $pdo->prepare(
            'SELECT o.id,o.status,o.customer_email,o.order_json,q.quote_code,p.partner_code
             FROM orders o
             JOIN quotes q ON q.id=o.quote_id
             JOIN partners p ON p.id=o.partner_id
             WHERE o.order_code = ?
             LIMIT 1'
        );
        $stmt->execute([$orderId]);
        $verifiedOrder = $stmt->fetch();
        if (!$verifiedOrder) {
            fail('The partner order could not be verified.', 404);
        }

        $lockedOrder = json_decode((string)$verifiedOrder['order_json'], true);
        if (!is_array($lockedOrder)) {
            fail('The partner order details are invalid.', 500);
        }

        $adapterKey = (string)($lockedOrder['adapter'] ?? '');
        $adapterMap = [
            'webhookTest' => 'webhook_test',
            'adapter8' => '8channel',
            'adapter16' => '16channel',
            'adapter32' => '32channel',
            'adapter64' => '64channel',
        ];
        $lockedAdapterType = $adapterMap[$adapterKey] ?? '';
        $lockedQuantity = max(1, min(20, (int)($lockedOrder['adapterQty'] ?? 1)));

        $lockedRawHardwareItems = [];
        if (is_array($lockedOrder['hardwareItems'] ?? null)) {
            $lockedRawHardwareItems = $lockedOrder['hardwareItems'];
        } elseif (is_array($lockedOrder['hardware_items'] ?? null)) {
            $lockedRawHardwareItems = $lockedOrder['hardware_items'];
        }

        $lockedHardwareItems = normalize_hardware_items($lockedRawHardwareItems, $hardwareCatalog);

        // Backward compatibility for older one-camera database orders.
        if (!$lockedHardwareItems) {
            $lockedCameraQuantity = max(
                0,
                min(
                    64,
                    (int)($lockedOrder['cameraHardwareQty'] ?? $lockedOrder['cameraQty'] ?? 0)
                )
            );

            if ($lockedCameraQuantity > 0) {
                $lockedHardwareItems = normalize_hardware_items([
                    [
                        'sku' => 'AIC-CAM-5MP-DOME-001',
                        'quantity' => $lockedCameraQuantity,
                    ],
                ], $hardwareCatalog);
            }
        }

        $hasPayableAdapter = $lockedAdapterType !== ''
            && isset($priceMap[$lockedAdapterType])
            && $priceMap[$lockedAdapterType] !== '';

        if (!$hasPayableAdapter && !$lockedHardwareItems) {
            fail('This order does not contain payable hardware.', 422);
        }

        // Use database values so browser changes cannot alter products,
        // quantities, salesperson attribution, quote, or customer email.
        $adapterType = $hasPayableAdapter ? $lockedAdapterType : 'software';
        $quantity = $lockedQuantity;
        $hardwareItems = $lockedHardwareItems;
        $cameraQuantity = array_reduce(
            $hardwareItems,
            static function (int $total, array $item): int {
                return str_starts_with((string)$item['sku'], 'AIC-CAM-')
                    ? $total + (int)$item['quantity']
                    : $total;
            },
            0
        );
        $quoteId = (string)$verifiedOrder['quote_code'];
        $partnerId = (string)$verifiedOrder['partner_code'];
        $customerEmail = (string)$verifiedOrder['customer_email'];
        $analyticsParts = [];
        foreach (($lockedOrder['analytics'] ?? []) as $selection) {
            if (!is_array($selection)) continue;
            $key = preg_replace('/[^A-Za-z0-9_-]/', '', (string)($selection['key'] ?? ''));
            $qty = (int)($selection['quantity'] ?? 0);
            if ($key !== '' && $qty > 0) $analyticsParts[] = $key . ':' . $qty;
        }
        $analyticsMetadata = substr(implode(',', $analyticsParts), 0, 450);
    } catch (Throwable $e) {
        error_log('Stripe checkout database verification failed: ' . $e->getMessage());
        fail('The order database could not be reached.', 500);
    }
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
    // Line items are added below so camera-only checkout is supported.
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
        // Safe temporary fallback until a Stripe Price ID is configured.
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

if ($orderId !== '' && isset($pdo, $verifiedOrder) && $pdo instanceof PDO && !empty($data['id'])) {
    try {
        $storedOrder = json_decode((string)$verifiedOrder['order_json'], true);
        if (!is_array($storedOrder)) $storedOrder = [];
        $stripe = isset($storedOrder['stripe']) && is_array($storedOrder['stripe']) ? $storedOrder['stripe'] : [];
        $stripe['checkout_session_id'] = (string)$data['id'];
        $stripe['checkout_created_at'] = gmdate('c');
        $stripe['payment_status'] = (string)($data['payment_status'] ?? 'unpaid');
        $storedOrder['stripe'] = $stripe;

        $stmt = $pdo->prepare(
            "UPDATE orders
             SET order_json = ?, status = CASE WHEN status IN ('paid','completed') THEN status ELSE 'checkout_started' END
             WHERE id = ?"
        );
        $stmt->execute([
            json_encode($storedOrder, JSON_UNESCAPED_SLASHES),
            $verifiedOrder['id'],
        ]);
    } catch (Throwable $e) {
        error_log('Stripe checkout session save failed for order ' . $orderId . ': ' . $e->getMessage());
        fail('The payment session was created, but the order could not be safely saved. Please contact support.', 500);
    }
}

if (ob_get_length()) {
    ob_clean();
}
echo json_encode([
    'url' => $data['url'],
], JSON_UNESCAPED_SLASHES);
