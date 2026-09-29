<?php
declare(strict_types=1);

/**
 * Stripe webhook for ANY AI CAM adapter checkout.
 *
 * Source of truth for payment completion:
 * - verifies Stripe signature
 * - processes checkout.session.completed once
 * - marks the internal order paid
 * - stores Stripe session/payment IDs server-side
 * - sends customer/admin confirmation emails once
 */

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

require_once __DIR__ . '/stripe-config.php';

function json_response(array $payload, int $status = 200): never {
    http_response_code($status);
    echo json_encode($payload, JSON_UNESCAPED_SLASHES);
    exit;
}

function configured(string $name): bool {
    return defined($name) && (string)constant($name) !== '' && strpos((string)constant($name), 'REPLACE') === false;
}

if (!configured('STRIPE_WEBHOOK_SECRET')) {
    json_response(['error' => 'Webhook secret is not configured.'], 500);
}

function verify_stripe_signature(string $payload, string $header, string $secret, int $tolerance = 300): bool {
    $timestamp = null;
    $signatures = [];

    foreach (explode(',', $header) as $part) {
        $pieces = explode('=', trim($part), 2);
        if (count($pieces) !== 2) continue;
        if ($pieces[0] === 't') $timestamp = (int)$pieces[1];
        if ($pieces[0] === 'v1') $signatures[] = $pieces[1];
    }

    if (!$timestamp || !$signatures) return false;
    if (abs(time() - $timestamp) > $tolerance) return false;

    $expected = hash_hmac('sha256', $timestamp . '.' . $payload, $secret);
    foreach ($signatures as $signature) {
        if (hash_equals($expected, $signature)) return true;
    }
    return false;
}

function db(): PDO {
    $configFile = __DIR__ . '/config.php';
    if (!is_file($configFile)) {
        throw new RuntimeException('Database configuration is missing.');
    }
    $db = require $configFile;
    return new PDO(
        'mysql:host=' . $db['db_host'] . ';dbname=' . $db['db_name'] . ';charset=utf8mb4',
        $db['db_user'],
        $db['db_pass'],
        [
            PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            PDO::ATTR_EMULATE_PREPARES => false,
        ]
    );
}

function clean_header(string $value): string {
    return trim(str_replace(["\r", "\n"], '', $value));
}
function smtp_expect($socket, array $codes): array {
    $response = '';
    while (($line = fgets($socket, 515)) !== false) {
        $response .= $line;
        if (strlen($line) < 4 || $line[3] !== '-') break;
    }
    $code = (int)substr($response, 0, 3);
    return [in_array($code, $codes, true), trim($response)];
}
function smtp_command($socket, string $command, array $codes): array {
    fwrite($socket, $command . "\r\n");
    return smtp_expect($socket, $codes);
}
function send_raw_smtp(array $config, string $to, string $subject, string $body): array {
    foreach (['smtp_host','smtp_port','smtp_secure','smtp_username','smtp_password','from_email','from_name','support_email'] as $key) {
        if (!isset($config[$key]) || $config[$key] === '') {
            return [false, 'Mail configuration is incomplete.'];
        }
    }

    $host = (string)$config['smtp_host'];
    $port = (int)$config['smtp_port'];
    $secure = strtolower((string)$config['smtp_secure']);
    $target = $secure === 'ssl' ? 'ssl://' . $host : $host;

    $socket = @fsockopen($target, $port, $errno, $errstr, 20);
    if (!$socket) return [false, 'SMTP connection failed.'];
    stream_set_timeout($socket, 20);

    [$ok,$msg] = smtp_expect($socket,[220]);
    if (!$ok) { fclose($socket); return [false,'SMTP greeting failed.']; }

    [$ok,$msg] = smtp_command($socket,'EHLO anyaicam.com',[250]);
    if (!$ok) { fclose($socket); return [false,'SMTP EHLO failed.']; }

    if ($secure === 'tls') {
        [$ok,$msg] = smtp_command($socket,'STARTTLS',[220]);
        if (!$ok || !stream_socket_enable_crypto($socket,true,STREAM_CRYPTO_METHOD_TLS_CLIENT)) {
            fclose($socket);
            return [false,'SMTP TLS failed.'];
        }
        [$ok,$msg] = smtp_command($socket,'EHLO anyaicam.com',[250]);
        if (!$ok) { fclose($socket); return [false,'SMTP EHLO after TLS failed.']; }
    }

    foreach ([
        ['AUTH LOGIN',[334]],
        [base64_encode((string)$config['smtp_username']),[334]],
        [base64_encode((string)$config['smtp_password']),[235]]
    ] as [$command,$codes]) {
        [$ok,$msg] = smtp_command($socket,$command,$codes);
        if (!$ok) { fclose($socket); return [false,'SMTP authentication failed.']; }
    }

    $fromEmail = clean_header((string)$config['from_email']);
    $fromName = clean_header((string)$config['from_name']);
    $supportEmail = clean_header((string)$config['support_email']);
    $to = clean_header($to);
    $subject = clean_header($subject);

    foreach ([
        ['MAIL FROM:<' . $fromEmail . '>',[250]],
        ['RCPT TO:<' . $to . '>',[250,251]],
        ['DATA',[354]]
    ] as [$command,$codes]) {
        [$ok,$msg] = smtp_command($socket,$command,$codes);
        if (!$ok) { fclose($socket); return [false,'SMTP message setup failed.']; }
    }

    $headers = [
        'From: ' . $fromName . ' <' . $fromEmail . '>',
        'Reply-To: ANY AI CAM Support <' . $supportEmail . '>',
        'Date: ' . date(DATE_RFC2822),
        'Message-ID: <' . bin2hex(random_bytes(12)) . '@anyaicam.com>',
        'MIME-Version: 1.0',
        'Content-Type: text/plain; charset=UTF-8',
        'Content-Transfer-Encoding: 8bit',
        'X-Mailer: ANYAICAM Website',
    ];

    $data = implode("\r\n", [
        'To: <' . $to . '>',
        'Subject: ' . $subject,
        implode("\r\n",$headers),
        '',
        str_replace(["\r\n","\r"],"\n",$body),
    ]);
    $data = str_replace("\n.","\n..",$data);

    [$ok,$msg] = smtp_command($socket,$data . "\r\n.",[250]);
    smtp_command($socket,'QUIT',[221,250]);
    fclose($socket);
    return [$ok,$ok?'Sent':'SMTP message failed.'];
}

function money_from_cents(int $cents, string $currency): string {
    return strtoupper($currency ?: 'usd') . ' ' . number_format($cents / 100, 2);
}

function send_payment_emails(array $order, array $orderJson, array $stripe): array {
    $customerEmail = trim((string)($order['customer_email'] ?? ''));
    $customerName = trim((string)($orderJson['customer']['name'] ?? $order['customer_name'] ?? 'Customer'));
    $orderCode = (string)($order['order_code'] ?? '');
    $amount = money_from_cents((int)($stripe['amount_total'] ?? 0), (string)($stripe['currency'] ?? 'usd'));
    $config = [];
    $configFile = __DIR__ . '/config.php';
    if (is_file($configFile)) {
        $loadedConfig = require $configFile;
        if (is_array($loadedConfig)) $config = $loadedConfig;
    }

    $mailConfig = [];
    $mailConfigFile = __DIR__ . '/mail_config.php';
    if (is_file($mailConfigFile)) {
        $loadedMailConfig = require $mailConfigFile;
        if (is_array($loadedMailConfig)) $mailConfig = $loadedMailConfig;
    }
    $supportEmail = filter_var((string)($config['support_email'] ?? $config['partner_admin_email'] ?? (defined('SUPPORT_EMAIL') ? SUPPORT_EMAIL : 'amata@anyaicam.com')), FILTER_VALIDATE_EMAIL)
        ? (string)($config['support_email'] ?? $config['partner_admin_email'] ?? (defined('SUPPORT_EMAIL') ? SUPPORT_EMAIL : 'amata@anyaicam.com'))
        : 'amata@anyaicam.com';
    $supportPhone = defined('SUPPORT_PHONE') ? (string)SUPPORT_PHONE : '(832) 510-8240';
    $adminEmail = filter_var((string)($config['payment_admin_email'] ?? $config['partner_admin_email'] ?? (defined('PAYMENT_ADMIN_EMAIL') ? PAYMENT_ADMIN_EMAIL : $supportEmail)), FILTER_VALIDATE_EMAIL)
        ? (string)($config['payment_admin_email'] ?? $config['partner_admin_email'] ?? (defined('PAYMENT_ADMIN_EMAIL') ? PAYMENT_ADMIN_EMAIL : $supportEmail))
        : $supportEmail;
    $fromEmail = filter_var((string)($config['mail_from_email'] ?? 'orders@anyaicam.com'), FILTER_VALIDATE_EMAIL)
        ? (string)($config['mail_from_email'] ?? 'orders@anyaicam.com')
        : $supportEmail;

    $customer = is_array($orderJson['customer'] ?? null) ? $orderJson['customer'] : [];
    $siteParts = [];
    foreach (['siteName', 'address', 'city', 'state', 'zip'] as $key) {
        $value = trim((string)($customer[$key] ?? ''));
        if ($value !== '') $siteParts[] = $value;
    }
    $siteAddress = $siteParts ? implode(', ', $siteParts) : 'Not available';

    $adapter = (string)($orderJson['adapter'] ?? '');
    $adapterQty = (int)($orderJson['adapterQty'] ?? 1);
    $cloudPlan = strtoupper((string)($orderJson['resolution'] ?? '')) . ', ' . (string)($orderJson['duration'] ?? '') . '-day ' . (((string)($orderJson['mode'] ?? '')) === 'continuous' ? '24/7' : 'motion');
    $analytics = 'No AI add-ons';
    if (!empty($orderJson['analytics']) && is_array($orderJson['analytics'])) {
        $parts = [];
        foreach ($orderJson['analytics'] as $item) {
            if (is_array($item) && !empty($item['quantity'])) $parts[] = (int)$item['quantity'] . ' x ' . (string)($item['name'] ?? $item['key'] ?? 'AI add-on');
        }
        if ($parts) $analytics = implode(', ', $parts);
    }

    $itemLines = [];
    $rows = is_array($orderJson['rows'] ?? null) ? $orderJson['rows'] : [];
    foreach ($rows as $row) {
        if (!is_array($row)) continue;
        $label = trim((string)($row['label'] ?? $row['type'] ?? 'Item')) ?: 'Item';
        $qty = (int)($row['quantity'] ?? 1);
        $unit = is_numeric($row['unitPrice'] ?? null) ? money_from_cents((int)round(((float)$row['unitPrice']) * 100), 'usd') : 'Not available';
        $customerTotal = is_numeric($row['customer'] ?? null) ? money_from_cents((int)round(((float)$row['customer']) * 100), 'usd') : 'Not available';
        $margin = is_numeric($row['margin'] ?? null) ? money_from_cents((int)round(((float)$row['margin']) * 100), 'usd') : 'Not available';
        $itemLines[] = '- ' . $label . "\n"
            . '  Qty: ' . $qty . "\n"
            . '  Unit price: ' . $unit . "\n"
            . '  Customer total: ' . $customerTotal . "\n"
            . '  Margin: ' . $margin;
    }
    $itemizedOrder = $itemLines ? implode("\n", $itemLines) : 'No itemized order rows were saved for this order.';
    $customerItemizedOrder = preg_replace('/\n  Margin: [^\n]+/', '', $itemizedOrder) ?: $itemizedOrder;
    $wizardSummary = trim((string)($orderJson['orderSummaryText'] ?? ''));
    if ($wizardSummary === '' && is_array($orderJson['emailSummary'] ?? null)) {
        $wizardSummary = trim((string)($orderJson['emailSummary']['body'] ?? ''));
    }

    $customerTotal = is_numeric($orderJson['customerTotal'] ?? null) ? money_from_cents((int)round(((float)$orderJson['customerTotal']) * 100), 'usd') : (is_numeric($order['customer_total'] ?? null) ? money_from_cents((int)round(((float)$order['customer_total']) * 100), 'usd') : 'Not available');
    $companyCost = is_numeric($orderJson['companyCost'] ?? null) ? money_from_cents((int)round(((float)$orderJson['companyCost']) * 100), 'usd') : 'Not available';
    $grossMargin = is_numeric($orderJson['grossMargin'] ?? null) ? money_from_cents((int)round(((float)$orderJson['grossMargin']) * 100), 'usd') : 'Not available';
    $commission = is_numeric($orderJson['repCommission'] ?? null) ? money_from_cents((int)round(((float)$orderJson['repCommission']) * 100), 'usd') : (is_numeric($order['rep_commission'] ?? null) ? money_from_cents((int)round(((float)$order['rep_commission']) * 100), 'usd') : 'Not available');

    $subject = 'ANY AI CAM itemized payment received - ' . $orderCode;
    $customerLines = [
        'ANY AI CAM Payment Received',
        '',
        'Customer Information',
        'Customer: ' . $customerName,
        'Customer email: ' . ($customerEmail ?: 'Not available'),
        'Customer phone: ' . (trim((string)($customer['phone'] ?? '')) ?: 'Not available'),
        'Installation/site: ' . $siteAddress,
        '',
        'Order Information',
        'Order ID: ' . $orderCode,
        'Quote ID: ' . (string)($order['quote_code'] ?? 'Not available'),
        'Payment status: Paid',
        'Amount paid today: ' . $amount,
        '',
        'Itemized Order',
        $customerItemizedOrder,
    ];
    if ($wizardSummary !== '') {
        $customerLines[] = '';
        $customerLines[] = 'Original Wizard Order Details';
        $customerLines[] = $wizardSummary;
    }
    $customerLines = array_merge($customerLines, [
        '',
        'Order Summary',
        'Adapter/order details: ' . ($adapter !== '' ? $adapter : 'ANY AI CAM order') . ' - Qty ' . $adapterQty,
        'Cameras: ' . (string)($orderJson['cameraCount'] ?? '0'),
        'Cloud plan: ' . $cloudPlan,
        'AI add-ons: ' . $analytics,
        '',
        'Delivery normally occurs within 7 days for shipped adapter hardware.',
        'Cloud billing begins after the 30-day free trial.',
        '',
        'Support: ' . $supportEmail . ' | ' . $supportPhone,
    ]);
    $adminLines = array_merge($customerLines, [
        '',
        'Internal Partner / Financial Details',
        'Partner ID: ' . (string)($order['partner_code'] ?? 'Not available'),
        'Customer total: ' . $customerTotal,
        'Company cost: ' . $companyCost,
        'Gross margin: ' . $grossMargin,
        'Partner commission: ' . $commission,
        '',
        'Admin itemization including margin',
        $itemizedOrder,
    ]);
    $customerBody = implode("\n", $customerLines) . "\n";
    $adminBody = implode("\n", $adminLines) . "\n";
    $results = [
        'customer_attempted' => false,
        'customer_sent' => false,
        'admin_attempted' => false,
        'admin_sent' => false,
    ];

    if (!$mailConfig) {
        error_log('ANY AI CAM payment email failed: mail_config.php is missing or invalid for order ' . $orderCode);
        @file_put_contents(
            __DIR__ . '/email-log.txt',
            date('Y-m-d H:i:s') . ' - stripe-webhook - FAILED - ' . $orderCode . ' - mail_config.php missing or invalid' . "\n",
            FILE_APPEND | LOCK_EX
        );
        return $results;
    }

    // Use the same authenticated SMTP sender that is proven in welcome-email.php.
    // Recipient addresses still come from the order/database configuration.
    if ($customerEmail !== '' && filter_var($customerEmail, FILTER_VALIDATE_EMAIL)) {
        $results['customer_attempted'] = true;
        [$results['customer_sent'], $customerMessage] = send_raw_smtp(
            $mailConfig,
            $customerEmail,
            $subject,
            $customerBody
        );
        @file_put_contents(
            __DIR__ . '/email-log.txt',
            date('Y-m-d H:i:s') . ' - stripe-webhook-customer - '
                . ($results['customer_sent'] ? 'SUCCESS' : 'FAILED')
                . ' - ' . $orderCode . ' - ' . $customerMessage . "\n",
            FILE_APPEND | LOCK_EX
        );
        if (!$results['customer_sent']) {
            error_log('ANY AI CAM authenticated SMTP customer payment email failed for order ' . $orderCode . ': ' . $customerMessage);
        }
    }

    if ($adminEmail !== '' && filter_var($adminEmail, FILTER_VALIDATE_EMAIL)) {
        $results['admin_attempted'] = true;
        [$results['admin_sent'], $adminMessage] = send_raw_smtp(
            $mailConfig,
            $adminEmail,
            '[Admin] ' . $subject,
            $adminBody
        );
        @file_put_contents(
            __DIR__ . '/email-log.txt',
            date('Y-m-d H:i:s') . ' - stripe-webhook-admin - '
                . ($results['admin_sent'] ? 'SUCCESS' : 'FAILED')
                . ' - ' . $orderCode . ' - ' . $adminMessage . "\n",
            FILE_APPEND | LOCK_EX
        );
        if (!$results['admin_sent']) {
            error_log('ANY AI CAM authenticated SMTP admin payment email failed for order ' . $orderCode . ': ' . $adminMessage);
        }
    }

    return $results;
}
$payload = file_get_contents('php://input') ?: '';
$signatureHeader = $_SERVER['HTTP_STRIPE_SIGNATURE'] ?? '';

if (!verify_stripe_signature($payload, $signatureHeader, (string)STRIPE_WEBHOOK_SECRET)) {
    json_response(['error' => 'Invalid Stripe signature.'], 400);
}

$event = json_decode($payload, true);
if (!is_array($event) || empty($event['type'])) {
    json_response(['error' => 'Invalid event payload.'], 400);
}

$handledTypes = ['checkout.session.completed', 'checkout.session.async_payment_succeeded'];
if (!in_array($event['type'], $handledTypes, true)) {
    json_response(['received' => true, 'ignored' => true]);
}

$session = $event['data']['object'] ?? [];
if (!is_array($session)) {
    json_response(['error' => 'Invalid session payload.'], 400);
}

$paymentStatus = (string)($session['payment_status'] ?? '');
if ($paymentStatus !== 'paid') {
    json_response(['received' => true, 'pending' => true]);
}

$metadata = is_array($session['metadata'] ?? null) ? $session['metadata'] : [];
$orderId = trim((string)($metadata['order_id'] ?? $session['client_reference_id'] ?? ''));
if ($orderId === '') {
    json_response(['received' => true, 'public_order' => true]);
}

try {
    $pdo = db();
    $pdo->beginTransaction();

    $stmt = $pdo->prepare(
        'SELECT o.id,o.order_code,o.status,o.customer_email,o.customer_name,o.order_json,o.quote_id,
                q.quote_code,p.partner_code,o.customer_total,o.rep_commission
         FROM orders o
         LEFT JOIN quotes q ON q.id=o.quote_id
         LEFT JOIN partners p ON p.id=o.partner_id
         WHERE o.order_code = ?
         LIMIT 1
         FOR UPDATE'
    );
    $stmt->execute([$orderId]);
    $order = $stmt->fetch();

    if (!$order) {
        $pdo->rollBack();
        json_response(['error' => 'Order not found.'], 404);
    }

    $orderJson = json_decode((string)$order['order_json'], true);
    if (!is_array($orderJson)) $orderJson = [];

    $stripe = isset($orderJson['stripe']) && is_array($orderJson['stripe']) ? $orderJson['stripe'] : [];
    $sessionId = (string)($session['id'] ?? '');
    $alreadyPaid = in_array((string)$order['status'], ['paid', 'completed'], true)
        && (string)($stripe['checkout_session_id'] ?? '') === $sessionId
        && !empty($stripe['itemized_confirmation_email_sent_at']);

    if ($alreadyPaid) {
        $pdo->commit();
        json_response(['received' => true, 'duplicate' => true, 'order_id' => $orderId, 'status' => 'paid']);
    }

    $paidAt = gmdate('c');
    $stripe['checkout_session_id'] = $sessionId;
    $stripe['payment_intent_id'] = (string)($session['payment_intent'] ?? '');
    $stripe['payment_status'] = $paymentStatus;
    $stripe['amount_total'] = (int)($session['amount_total'] ?? 0);
    $stripe['currency'] = (string)($session['currency'] ?? 'usd');
    $stripe['paid_at'] = $stripe['paid_at'] ?? $paidAt;
    $orderJson['stripe'] = $stripe;
    $orderJson['payment'] = [
        'status' => 'paid',
        'amount_paid_today' => $stripe['amount_total'],
        'currency' => $stripe['currency'],
        'paid_at' => $stripe['paid_at'],
    ];
    $activation = is_array($orderJson['activation'] ?? null) ? $orderJson['activation'] : [];
    $history = is_array($activation['history'] ?? null) ? $activation['history'] : [];
    $history[] = ['status' => 'paid', 'note' => 'Stripe webhook payment verified.', 'updated_at' => gmdate('c')];
    $activation['current_status'] = 'paid';
    $activation['updated_at'] = gmdate('c');
    $activation['history'] = $history;
    $orderJson['activation'] = $activation;

    $stmt = $pdo->prepare("UPDATE orders SET status='paid', order_json=? WHERE id=?");
    $stmt->execute([json_encode($orderJson, JSON_UNESCAPED_SLASHES), $order['id']]);

    if (!empty($order['quote_id'])) {
        $stmt = $pdo->prepare("UPDATE quotes SET status='paid' WHERE id=?");
        $stmt->execute([$order['quote_id']]);
    }

    $pdo->commit();

    if (empty($stripe['itemized_confirmation_email_sent_at'])) {
        $emailResults = send_payment_emails($order, $orderJson, $stripe);
        $stripe['customer_confirmation_email_sent_at'] = $stripe['customer_confirmation_email_sent_at']
            ?? ($emailResults['customer_sent'] ? gmdate('c') : null);
        $stripe['admin_confirmation_email_sent_at'] = $stripe['admin_confirmation_email_sent_at']
            ?? ($emailResults['admin_sent'] ? gmdate('c') : null);
        if (
            (!$emailResults['customer_attempted'] || $emailResults['customer_sent'])
            && (!$emailResults['admin_attempted'] || $emailResults['admin_sent'])
        ) {
            $stripe['itemized_confirmation_email_sent_at'] = gmdate('c');
            $stripe['payment_confirmation_email_sent_at'] = $stripe['payment_confirmation_email_sent_at'] ?? $stripe['itemized_confirmation_email_sent_at'];
        }
        $orderJson['stripe'] = array_filter($stripe, static fn($value) => $value !== null);
        $stmt = $pdo->prepare('UPDATE orders SET order_json=? WHERE id=?');
        $stmt->execute([json_encode($orderJson, JSON_UNESCAPED_SLASHES), $order['id']]);
    }

    json_response(['received' => true, 'order_id' => $orderId, 'status' => 'paid']);
} catch (Throwable $e) {
    if (isset($pdo) && $pdo instanceof PDO && $pdo->inTransaction()) $pdo->rollBack();
    error_log('Stripe webhook database error: ' . $e->getMessage());
    json_response(['error' => 'Database update failed.'], 500);
}



