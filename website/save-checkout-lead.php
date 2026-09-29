<?php
declare(strict_types=1);

date_default_timezone_set('America/Chicago');
header('Content-Type: application/json');

$config = [];

// Try to load mail config
$configPath = __DIR__ . '/mail_config.php';
if (is_file($configPath)) {
    $config = require $configPath;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    echo json_encode(['success' => false, 'error' => 'Invalid request method']);
    exit;
}

function clean_value($value) {
    return trim((string)$value);
}


function anyaicam_hardware_catalog(): array {
    return [
        'AIC-CAM-LTS-LXIP1142' => ['kind' => 'camera', 'name' => 'LTS LXIP1142W-28MA - Pro-X 4MP Starlight Turret IP Camera', 'unitPrice' => 114.99],
        'AIC-CAM-LTS-CMIP3382' => ['kind' => 'camera', 'name' => 'LTS CMIP3382WI-28SDL - Platinum Plus 8MP Active-Deterrence Turret', 'unitPrice' => 249.99],
        'AIC-CAM-LTS-CMIP3C42' => ['kind' => 'camera', 'name' => 'LTS CMIP3C42WI-28SDL - 4MP Color 24/7 Active-Deterrence Turret', 'unitPrice' => 299.99],
        'AIC-CAM-LTS-CMIP3C82' => ['kind' => 'camera', 'name' => 'LTS CMIP3C82WI-28SDL - 8MP Color 24/7 Active-Deterrence Turret', 'unitPrice' => 409.99],
        'AIC-CAM-LTS-CMHT1722' => ['kind' => 'camera', 'name' => 'LTS CMHT1722-28LS - 2MP HD-TVI Active-Deterrence Turret', 'unitPrice' => 114.99],
        'AIC-CAM-LTS-CMHT1752' => ['kind' => 'camera', 'name' => 'LTS CMHT1752-28LS - 5MP HD-TVI Dual-Light Turret', 'unitPrice' => 129.99],
        'AIC-CAM-LTS-CMHT1782' => ['kind' => 'camera', 'name' => 'LTS CMHT1782-28LF - Platinum Plus 8MP HD-TVI Turret', 'unitPrice' => 149.99],
        'AIC-POE-LTS-8' => ['kind' => 'poe', 'name' => 'LTS VSPOE-SW802 - 8-Port PoE Switch with 2 Uplink Ports', 'unitPrice' => 159.99],
        'AIC-POE-LTS-16' => ['kind' => 'poe', 'name' => 'LTS VSPOE-SW1602 - 16-Port PoE Switch with 2 Combo Ports', 'unitPrice' => 329.99],
        'AIC-POE-LTS-24' => ['kind' => 'poe', 'name' => 'LTS VSPOE-SW2402 - Pro-VS 24-Port PoE Switch with 2 Combo Ports', 'unitPrice' => 429.99],
        'AIC-CAM-5MP-DOME-001' => ['kind' => 'camera', 'name' => 'VIVOTEK FD9380-HTV-V2 5MP Outdoor Dome AI Camera', 'unitPrice' => 349.99],
        'AIC-CAM-BULLET-IB9380' => ['kind' => 'camera', 'name' => 'VIVOTEK IB9380-HTV-V2 5MP Outdoor Bullet AI Camera', 'unitPrice' => 349.99],
        'AIC-CAM-FISHEYE-FE9380' => ['kind' => 'camera', 'name' => 'VIVOTEK FE9380-HV 5MP Fisheye Panoramic Camera', 'unitPrice' => 649.99],
        'AIC-CAM-DUAL-MA9312' => ['kind' => 'camera', 'name' => 'VIVOTEK MA9312-EHTV Dual-Directional 4K AI Camera', 'unitPrice' => 1799.99],
        'AIC-POE-MOKER-8' => ['kind' => 'poe', 'name' => 'MokerLink POE-F082G 8-Port PoE Switch with 2 Gigabit Uplinks', 'unitPrice' => 79.99],
        'AIC-POE-MOKER-16' => ['kind' => 'poe', 'name' => 'MokerLink POE-G162G 16-Port Gigabit PoE+ Switch with 2 Gigabit Uplinks', 'unitPrice' => 174.99],
        'AIC-POE-MOKER-24' => ['kind' => 'poe', 'name' => 'MokerLink POE-G244GS 24-Port Gigabit PoE+ Switch with Ethernet and SFP Uplinks', 'unitPrice' => 229.99],
        'AIC-POE-MOKER-48' => ['kind' => 'poe', 'name' => 'MokerLink POE-G482GS 48-Port Gigabit PoE Switch with 2 SFP Uplinks', 'unitPrice' => 429.99],
    ];
}

function anyaicam_parse_hardware_items(array $wizardData): array {
    $catalog = anyaicam_hardware_catalog();
    $rawItems = is_array($wizardData['hardwareItems'] ?? null) ? $wizardData['hardwareItems'] : [];

    // Backward compatibility only. Never trust a browser-supplied hardware price.
    if (!$rawItems && (int)($wizardData['cameraHardwareQty'] ?? $wizardData['cameraQty'] ?? 0) > 0) {
        $rawItems[] = [
            'sku' => clean_value($wizardData['cameraHardwareSku'] ?? 'AIC-CAM-5MP-DOME-001'),
            'quantity' => (int)($wizardData['cameraHardwareQty'] ?? $wizardData['cameraQty'] ?? 0),
        ];
    }

    $items = [];
    $quantities = [];
    foreach ($rawItems as $rawItem) {
        if (!is_array($rawItem)) continue;
        $sku = clean_value($rawItem['sku'] ?? '');
        $quantity = max(0, min(64, (int)($rawItem['quantity'] ?? 0)));
        if ($quantity < 1 || !isset($catalog[$sku])) continue;
        $quantities[$sku] = min(64, ($quantities[$sku] ?? 0) + $quantity);
    }

    foreach ($catalog as $sku => $product) {
        $quantity = (int)($quantities[$sku] ?? 0);
        if ($quantity < 1) continue;
        $unitPrice = (float)$product['unitPrice'];
        $items[] = [
            'sku' => $sku,
            'kind' => $product['kind'],
            'name' => $product['name'],
            'quantity' => $quantity,
            'unitPrice' => $unitPrice,
            'subtotal' => round($quantity * $unitPrice, 2),
        ];
    }
    return $items;
}

function anyaicam_hardware_total(array $items): float {
    $total = 0.0;
    foreach ($items as $item) $total += (float)($item['subtotal'] ?? 0);
    return round($total, 2);
}

function anyaicam_hardware_email_text(array $items, bool $admin = false): string {
    if (!$items) return "Camera / PoE hardware: None\n\n";
    $text = '';
    foreach ($items as $item) {
        $isPoe = ($item['kind'] ?? '') === 'poe';
        $label = $isPoe ? 'PoE switch' : 'Camera';
        $text .= $label . ': ' . ($item['name'] ?? 'Hardware item') . "\n";
        $text .= $label . ' SKU: ' . ($item['sku'] ?? '') . "\n";
        $text .= $label . ' quantity: ' . (int)($item['quantity'] ?? 0) . "\n";
        $text .= $label . ' unit price: $' . number_format((float)($item['unitPrice'] ?? 0), 2) . "\n";
        $text .= $label . ' subtotal: $' . number_format((float)($item['subtotal'] ?? 0), 2) . "\n";
        if (!$isPoe) $text .= "Camera warranty: 1 year\n";
        $text .= "\n";
    }
    return $text;
}

$customerName = clean_value($_POST['customer_name'] ?? '');
$companyName = clean_value($_POST['company_name'] ?? '');
$address = clean_value($_POST['address'] ?? '');
$city = clean_value($_POST['city'] ?? '');
$state = clean_value($_POST['state'] ?? '');
$zip = clean_value($_POST['zip'] ?? '');
$email = clean_value($_POST['email'] ?? '');
$phone = clean_value($_POST['phone'] ?? '');
$paymentMethod = clean_value($_POST['payment_method'] ?? '');
$checkoutStartedAt = clean_value($_POST['checkout_started_at'] ?? '');
$wizardDataRaw = $_POST['wizard_data'] ?? '';

if ($customerName === '' || $email === '') {
    echo json_encode(['success' => false, 'error' => 'Missing required customer details']);
    exit;
}

// Build address string
$addressStr = '';
if ($address) {
    $addressStr = $address;
    if ($city) $addressStr .= ", $city";
    if ($state) $addressStr .= ", $state";
    if ($zip) $addressStr .= " $zip";
}

$wizardData = json_decode($wizardDataRaw, true);

$adapterType = '';
$dueToday = '';
$cloudBilling = '';
$billingCycle = '';
$totalCameras = 0;
$resolutions = [];
$duration = '';
$recordingType = '';
$analyticsList = '';
$cameraDetails = '';
$hardwareItems = [];
$hardwareItemsSubtotal = 0.00;
$adapterQty = 0;
$adapterUnitPrice = 0.00;
$adapterSubtotal = 0.00;
$hardwarePaidToday = 0.00;

if (is_array($wizardData)) {
    $adapterType = $wizardData['adapterType'] ?? '';
    $dueToday = $wizardData['dueTodayTotal'] ?? ($wizardData['total'] ?? '');
    $billingCycle = $wizardData['billingCycle'] ?? '';
    $duration = $wizardData['duration'] ?? '';
    $recordingType = $wizardData['recordingType'] ?? '';
    $resolutions = $wizardData['resolutions'] ?? [];
    $totalCameras = (int)($wizardData['totalCameras'] ?? $wizardData['quantity'] ?? 0);
    $hardwareItems = anyaicam_parse_hardware_items($wizardData);
    $hardwareItemsSubtotal = anyaicam_hardware_total($hardwareItems);

    $adapterQty = !empty($adapterType) ? max(0, (int)($wizardData['adapterQty'] ?? 1)) : 0;
    $adapterPrices = [
        'webhook_test' => 0.50,
        'webhookTest' => 0.50,
        '8channel' => 349.00,
        '16channel' => 399.00,
        '32channel' => 2149.00,
        '64channel' => 2689.00,
        'software' => 0.00,
        'ownedadapter' => 0.00
    ];
    $adapterUnitPrice = (float)($adapterPrices[$adapterType] ?? 0.00);
    $adapterSubtotal = $adapterQty * $adapterUnitPrice;
    $hardwarePaidToday = $adapterSubtotal + $hardwareItemsSubtotal;
    $dueToday = $hardwarePaidToday;

    // Build cloud-connected camera details string
    if (!empty($resolutions) && is_array($resolutions)) {
        $parts = [];
        foreach ($resolutions as $res => $qty) {
            if ($qty > 0) {
                $parts[] = "$qty x " . strtoupper($res);
            }
        }
        $cameraDetails = implode(', ', $parts);
    } elseif (!empty($wizardData['resolution'])) {
        $cameraDetails = $totalCameras . ' x ' . strtoupper($wizardData['resolution']);
    }

    if ($billingCycle === 'annual') {
        $cloudBilling = $wizardData['futureCloudBillingAnnual'] ?? '';
    } else {
        $cloudBilling = $wizardData['futureCloudBillingMonthly'] ?? '';
    }

    if (!empty($wizardData['analytics']) && is_array($wizardData['analytics'])) {
        $analyticsNames = [];
        foreach ($wizardData['analytics'] as $item) {
            if (is_array($item) && !empty($item['name'])) {
                $analyticsNames[] = $item['name'];
            }
        }
        $analyticsList = implode(', ', $analyticsNames);
    }
}

$adapterLabels = [
    'webhook_test' => 'Webhook Test',
    'webhookTest' => 'Webhook Test',
    '8channel' => 'Cloud Adapter Mini — 8 channels',
    '16channel' => 'Cloud Adapter — 16 channels',
    '32channel' => 'Cloud Adapter Enterprise — 32 channels',
    '64channel' => 'Cloud Adapter Enterprise — 64 channels',
    'software' => 'Free Downloadable Virtual Cloud Adapter',
    'ownedadapter' => 'Existing Videoloft Adapter'
];
$adapterLabel = $adapterLabels[$adapterType] ?? 'No adapter hardware';
$recordingLabel = $recordingType === 'continuous' ? '24/7 Continuous' : ($recordingType === 'motion' ? 'Motion Only' : 'N/A');
$durationLabel = $duration ? "$duration days" : 'N/A';
$billingLabel = $billingCycle === 'annual' ? 'Annual (10% Off)' : 'Monthly';

// Save to log file
$logFile = __DIR__ . '/checkout-leads.txt';
$logEntry = date('Y-m-d H:i:s') . " | $customerName | $email | $adapterLabel | $cameraDetails | $totalCameras cam | ${recordingLabel} | ${durationLabel} | ${billingLabel}\n";
file_put_contents($logFile, $logEntry, FILE_APPEND | LOCK_EX);

// === BUILD ADMIN EMAIL ===
$adminTo = $config['to_email'] ?? 'amata@anyaicam.com';
$adminSubject = 'New ANY AI CAM Order - ' . $customerName;
$adminMessage = "NEW ANY AI CAM ORDER RECEIVED\n";
$adminMessage .= str_repeat('=', 48) . "\n\n";
$adminMessage .= "CUSTOMER INFORMATION\n";
$adminMessage .= str_repeat('-', 48) . "\n";
$adminMessage .= "Name: $customerName\n";
$adminMessage .= "Company: " . ($companyName ?: 'N/A') . "\n";
$adminMessage .= "Address: " . ($addressStr ?: 'N/A') . "\n";
$adminMessage .= "Email: $email\n";
$adminMessage .= "Phone: " . ($phone ?: 'N/A') . "\n";
$adminMessage .= "Payment Method: " . ($paymentMethod ?: 'N/A') . "\n\n";

$adminMessage .= "HARDWARE PAID TODAY\n";
$adminMessage .= str_repeat('-', 48) . "\n";
if ($adapterQty > 0 && $adapterUnitPrice > 0) {
    $adminMessage .= "Adapter: $adapterLabel\n";
    $adminMessage .= "Adapter quantity: $adapterQty\n";
    $adminMessage .= "Adapter unit price: $" . number_format($adapterUnitPrice, 2) . "\n";
    $adminMessage .= "Adapter subtotal: $" . number_format($adapterSubtotal, 2) . "\n\n";
} else {
    $adminMessage .= "Adapter hardware: None\n\n";
}
$adminMessage .= anyaicam_hardware_email_text($hardwareItems, true);
$adminMessage .= "TOTAL PAID TODAY: $" . number_format($hardwarePaidToday, 2) . "\n\n";

$adminMessage .= "CLOUD SERVICE AFTER 30-DAY FREE TRIAL\n";
$adminMessage .= str_repeat('-', 48) . "\n";
$adminMessage .= "Connected cameras: $totalCameras\n";
$adminMessage .= "Camera plan details: " . ($cameraDetails ?: 'None') . "\n";
$adminMessage .= "Recording: $recordingLabel\n";
$adminMessage .= "Duration: $durationLabel\n";
$adminMessage .= "Billing: $billingLabel\n";
$adminMessage .= "Cloud amount: $" . number_format((float)$cloudBilling, 2) . "\n";
$adminMessage .= "Analytics: " . ($analyticsList ?: 'None') . "\n\n";
$adminMessage .= "Checkout Started: $checkoutStartedAt\n";
$adminMessage .= "Submitted: " . date('Y-m-d H:i:s') . "\n";

// === BUILD CUSTOMER EMAIL ===
$customerSubject = 'Your ANY AI CAM Order Summary';
$customerMessage = "Dear $customerName,\n\n";
$customerMessage .= "Thank you for your ANY AI CAM order.\n\n";

$customerMessage .= "HARDWARE PURCHASED TODAY\n";
$customerMessage .= str_repeat('-', 48) . "\n";
if ($adapterQty > 0 && $adapterUnitPrice > 0) {
    $customerMessage .= "$adapterLabel\n";
    $customerMessage .= "Quantity: $adapterQty\n";
    $customerMessage .= "Unit price: $" . number_format($adapterUnitPrice, 2) . "\n";
    $customerMessage .= "Adapter subtotal: $" . number_format($adapterSubtotal, 2) . "\n\n";
}
$customerMessage .= anyaicam_hardware_email_text($hardwareItems, false);
if ($hardwarePaidToday > 0) {
    $customerMessage .= "TOTAL PAID TODAY: $" . number_format($hardwarePaidToday, 2) . "\n\n";
} else {
    $customerMessage .= "No hardware payment is due today.\n\n";
}

if ($totalCameras > 0) {
    $customerMessage .= "CLOUD SERVICE AFTER 30-DAY FREE TRIAL\n";
    $customerMessage .= str_repeat('-', 48) . "\n";
    $customerMessage .= "Connected cameras: $totalCameras\n";
    if ($cameraDetails) $customerMessage .= "Camera plan details: $cameraDetails\n";
    if ($recordingLabel !== 'N/A') $customerMessage .= "Recording: $recordingLabel\n";
    if ($durationLabel !== 'N/A') $customerMessage .= "Retention: $durationLabel\n";
    if ($billingLabel !== 'N/A') $customerMessage .= "Billing: $billingLabel\n";
    $customerMessage .= "Cloud amount after trial: $" . number_format((float)$cloudBilling, 2) . "\n";
    if ($analyticsList) $customerMessage .= "Analytics: $analyticsList\n";
    $customerMessage .= "\n";
}

$customerMessage .= "WHAT HAPPENS NEXT\n";
$customerMessage .= str_repeat('-', 48) . "\n";
$step = 1;
if ($hardwarePaidToday > 0) {
    $customerMessage .= $step++ . ". Complete the secure Stripe payment for the hardware listed above.\n";
}
if (!empty($hardwareItems) || $adapterQty > 0) {
    $customerMessage .= $step++ . ". ANY AI CAM will process and prepare the purchased hardware for shipment.\n";
}
if ($totalCameras > 0) {
    $customerMessage .= $step++ . ". We will complete your cloud activation and camera onboarding.\n";
    $customerMessage .= $step++ . ". Videoloft cloud billing begins after the 30-day free trial.\n";
}
$customerMessage .= "\nIf you have any questions, reply to this email or contact us:\n";
$customerMessage .= "Phone: (832) 510-8240\n";
$customerMessage .= "Email: amata@anyaicam.com\n\n";
$customerMessage .= "Best regards,\n";
$customerMessage .= "ANY AI CAM Team\n";

// === SEND EMAILS ===
$adminSent = false;
$customerSent = false;

// Try PHPMailer first
if (!empty($config['smtp_enabled']) && !empty($config['smtp_host'])) {
    require_once __DIR__ . '/PHPMailer/PHPMailer.php';
    require_once __DIR__ . '/PHPMailer/Exception.php';
    require_once __DIR__ . '/PHPMailer/SMTP.php';

    try {
        $mail = new PHPMailer\PHPMailer\PHPMailer(true);
        $mail->isSMTP();
        $mail->Host = $config['smtp_host'];
        $mail->SMTPAuth = true;
        $mail->Username = $config['smtp_username'];
        $mail->Password = $config['smtp_password'];
        $mail->SMTPSecure = $config['smtp_secure'];
        $mail->Port = $config['smtp_port'];
        $mail->CharSet = 'UTF-8';
        $mail->setFrom($config['from_email'], $config['from_name'] ?: 'ANY AI CAM');

        // Admin email
        $mail->addAddress($adminTo);
        $mail->addReplyTo($email, $customerName);
        $mail->isHTML(false);
        $mail->Subject = $adminSubject;
        $mail->Body = $adminMessage;
        $mail->send();
        $adminSent = true;

        // Customer email
        $mail->clearAddresses();
        $mail->addAddress($email);
        $mail->addReplyTo($adminTo, $config['from_name'] ?: 'ANY AI CAM');
        $mail->Subject = $customerSubject;
        $mail->Body = $customerMessage;
        $mail->send();
        $customerSent = true;
    } catch (Throwable $e) {
        // Fallback to PHP mail()
        $adminSent = mail($adminTo, $adminSubject, $adminMessage, "From: no-reply@anyaicam.com\r\nReply-To: $email");
        $customerSent = mail($email, $customerSubject, $customerMessage, "From: no-reply@anyaicam.com\r\nReply-To: $adminTo");
    }
} else {
    // PHP mail fallback
    $adminSent = mail($adminTo, $adminSubject, $adminMessage, "From: no-reply@anyaicam.com\r\nReply-To: $email");
    $customerSent = mail($email, $customerSubject, $customerMessage, "From: no-reply@anyaicam.com\r\nReply-To: $adminTo");
}

// Log
$emailLog = __DIR__ . '/email-log.txt';
$logResult = "Admin:" . ($adminSent ? 'OK' : 'FAIL') . " Customer:" . ($customerSent ? 'OK' : 'FAIL');
file_put_contents($emailLog, date('Y-m-d H:i:s') . " - save-checkout-lead - $logResult\n", FILE_APPEND | LOCK_EX);

echo json_encode([
    'success' => $adminSent || $customerSent,
    'admin_email_sent' => $adminSent,
    'customer_email_sent' => $customerSent,
    'customer_email' => $email
]);
