<?php
declare(strict_types=1);

$siteConfigFile = __DIR__ . '/config.php';
$siteConfig = is_file($siteConfigFile) ? (require $siteConfigFile) : [];
if (!is_array($siteConfig)) $siteConfig = [];

$supportEmail = filter_var((string)($siteConfig['support_email'] ?? $siteConfig['partner_admin_email'] ?? 'amata@anyaicam.com'), FILTER_VALIDATE_EMAIL)
    ? (string)($siteConfig['support_email'] ?? $siteConfig['partner_admin_email'] ?? 'amata@anyaicam.com')
    : 'amata@anyaicam.com';
$paymentAdminEmail = filter_var((string)($siteConfig['payment_admin_email'] ?? $siteConfig['partner_admin_email'] ?? $supportEmail), FILTER_VALIDATE_EMAIL)
    ? (string)($siteConfig['payment_admin_email'] ?? $siteConfig['partner_admin_email'] ?? $supportEmail)
    : $supportEmail;
$mailFromEmail = filter_var((string)($siteConfig['mail_from_email'] ?? 'orders@anyaicam.com'), FILTER_VALIDATE_EMAIL)
    ? (string)($siteConfig['mail_from_email'] ?? 'orders@anyaicam.com')
    : $supportEmail;
$supportPhone = '(832) 510-8240';

$stripeConfigFile = __DIR__ . '/stripe-config.php';
if (is_file($stripeConfigFile)) {
    require_once $stripeConfigFile;
}

$orderId = trim((string)($_GET['order_id'] ?? $_GET['order'] ?? ''));
$quoteId = trim((string)($_GET['quote_id'] ?? $_GET['quote'] ?? ''));

$safeOrderId = preg_match('/^AIC-O-[A-Z0-9-]+$/', $orderId) ? $orderId : '';
$safeQuoteId = preg_match('/^AIC-Q-[A-Z0-9-]+$/', $quoteId) ? $quoteId : '';

function h(string $value): string {
    return htmlspecialchars($value, ENT_QUOTES, 'UTF-8');
}

function money_value(mixed $value, bool $cents = false): string {
    if (is_string($value) && preg_match('/^(USD\s*)?\$?\d+(\.\d{1,2})?$/i', trim($value))) {
        $value = (float)preg_replace('/[^0-9.]/', '', $value);
    }
    if (!is_numeric($value)) return 'Not available';
    $amount = (float)$value;
    if ($cents) $amount = $amount / 100;
    return 'USD ' . number_format($amount, 2);
}

function text_value(mixed $value, string $fallback = 'Not available'): string {
    $text = trim((string)($value ?? ''));
    return $text !== '' ? $text : $fallback;
}

function adapter_label(string $adapter): string {
    $map = [
        'webhookTest' => 'Webhook Test',
        'webhook_test' => 'Webhook Test',
        'adapter8' => '8-channel adapter',
        '8channel' => '8-channel adapter',
        'adapter16' => '16-channel adapter',
        '16channel' => '16-channel adapter',
        'adapter32' => '32-channel adapter',
        '32channel' => '32-channel adapter',
        'adapter64' => '64-channel adapter',
        '64channel' => '64-channel adapter',
        'none' => 'No adapter hardware',
    ];
    return $map[$adapter] ?? ($adapter !== '' ? $adapter : 'ANY AI CAM order');
}

function analytics_text(array $orderJson): string {
    $items = $orderJson['analytics'] ?? [];
    if (!is_array($items) || !$items) return 'No AI add-ons';
    $parts = [];
    foreach ($items as $item) {
        if (!is_array($item)) continue;
        $qty = (int)($item['quantity'] ?? 0);
        if ($qty <= 0) continue;
        $name = text_value($item['name'] ?? $item['key'] ?? 'AI add-on');
        $parts[] = $name . ' - ' . $qty . ' camera' . ($qty === 1 ? '' : 's');
    }
    return $parts ? implode(', ', $parts) : 'No AI add-ons';
}

function itemized_rows_text(array $orderJson): string {
    $rows = $orderJson['rows'] ?? [];
    if (!is_array($rows) || !$rows) return 'No itemized order rows were saved for this order.';

    $lines = [];
    foreach ($rows as $row) {
        if (!is_array($row)) continue;
        $label = text_value($row['label'] ?? $row['type'] ?? 'Item');
        $qty = (int)($row['quantity'] ?? 1);
        $unit = array_key_exists('unitPrice', $row) ? money_value($row['unitPrice']) : 'Not available';
        $customer = array_key_exists('customer', $row) ? money_value($row['customer']) : 'Not available';
        $margin = array_key_exists('margin', $row) ? money_value($row['margin']) : 'Not available';
        $lines[] = '- ' . $label . "\n"
            . '  Qty: ' . $qty . "\n"
            . '  Unit price: ' . $unit . "\n"
            . '  Customer total: ' . $customer . "\n"
            . '  Margin: ' . $margin;
    }

    return $lines ? implode("\n", $lines) : 'No itemized order rows were saved for this order.';
}

function order_address_text(array $customer): string {
    $parts = [];
    foreach (['siteName', 'address', 'city', 'state', 'zip'] as $key) {
        $value = trim((string)($customer[$key] ?? ''));
        if ($value !== '') $parts[] = $value;
    }
    return $parts ? implode(', ', $parts) : 'Not available';
}
function saved_wizard_summary(array $orderJson): string {
    $summary = trim((string)($orderJson['orderSummaryText'] ?? ''));
    if ($summary !== '') return $summary;
    $emailSummary = is_array($orderJson['emailSummary'] ?? null) ? $orderJson['emailSummary'] : [];
    return trim((string)($emailSummary['body'] ?? ''));
}

function connect_db(): ?PDO {
    $configFile = __DIR__ . '/config.php';
    if (!is_file($configFile)) return null;
    $db = require $configFile;
    try {
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
    } catch (Throwable $e) {
        error_log('ANY AI CAM payment success database connection failed: ' . $e->getMessage());
        return null;
    }
}

function stripe_api_get(string $path, array $query = []): ?array {
    if (!defined('STRIPE_SECRET_KEY') || STRIPE_SECRET_KEY === '') return null;

    $url = 'https://api.stripe.com' . $path;
    if ($query) $url .= '?' . http_build_query($query);

    $ch = curl_init();
    curl_setopt_array($ch, [
        CURLOPT_URL => $url,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPGET => true,
        CURLOPT_CONNECTTIMEOUT => 10,
        CURLOPT_TIMEOUT => 30,
        CURLOPT_HTTPHEADER => [
            'Authorization: Bearer ' . STRIPE_SECRET_KEY,
        ],
    ]);

    $response = curl_exec($ch);
    $curlError = curl_error($ch);
    $httpCode = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);

    if ($response === false || $curlError !== '' || $httpCode < 200 || $httpCode >= 300) {
        error_log('ANY AI CAM Stripe API verification failed for ' . $path . '.');
        return null;
    }

    $data = json_decode($response, true);
    return is_array($data) ? $data : null;
}

function stripe_session_matches_order(array $session, string $orderId): bool {
    $metadata = is_array($session['metadata'] ?? null) ? $session['metadata'] : [];
    return (string)($session['client_reference_id'] ?? '') === $orderId
        || (string)($metadata['order_id'] ?? '') === $orderId;
}

function retrieve_stripe_checkout_session(string $sessionId, string $orderId = ''): ?array {
    if (!preg_match('/^cs_(test|live)_[A-Za-z0-9_]+$/', $sessionId)) return null;
    $session = stripe_api_get('/v1/checkout/sessions/' . rawurlencode($sessionId));
    if (!$session) return null;
    if ($orderId !== '' && !stripe_session_matches_order($session, $orderId)) {
        error_log('ANY AI CAM Stripe session did not match internal order ID.');
        return null;
    }
    return $session;
}

function find_paid_stripe_checkout_session_for_order(string $orderId): ?array {
    if ($orderId === '') return null;
    $sessions = stripe_api_get('/v1/checkout/sessions', ['limit' => 100]);
    if (!$sessions || !is_array($sessions['data'] ?? null)) return null;

    foreach ($sessions['data'] as $session) {
        if (!is_array($session)) continue;
        if (!stripe_session_matches_order($session, $orderId)) continue;
        if ((string)($session['payment_status'] ?? '') === 'paid') return $session;
    }
    return null;
}

function verify_and_mark_order_paid(string $safeOrderId): void {
    if ($safeOrderId === '') return;
    $pdo = connect_db();
    if (!$pdo) return;

    try {
        $pdo->beginTransaction();
        $stmt = $pdo->prepare(
            'SELECT o.id,o.order_code,o.status,o.customer_total,o.order_json,o.quote_id
             FROM orders o
             WHERE o.order_code = ?
             LIMIT 1
             FOR UPDATE'
        );
        $stmt->execute([$safeOrderId]);
        $order = $stmt->fetch();
        if (!$order) {
            $pdo->rollBack();
            return;
        }

        $orderJson = json_decode((string)$order['order_json'], true);
        if (!is_array($orderJson)) $orderJson = [];
        $stripe = isset($orderJson['stripe']) && is_array($orderJson['stripe']) ? $orderJson['stripe'] : [];
        $sessionId = (string)($stripe['checkout_session_id'] ?? '');

        if (in_array((string)$order['status'], ['paid', 'completed'], true)) {
            $pdo->commit();
            return;
        }

        $pdo->commit();

        $session = $sessionId !== ''
            ? retrieve_stripe_checkout_session($sessionId, $safeOrderId)
            : find_paid_stripe_checkout_session_for_order($safeOrderId);
        if (!$session || (string)($session['payment_status'] ?? '') !== 'paid') {
            error_log('ANY AI CAM could not confirm paid Stripe session for order ' . $safeOrderId);
            return;
        }

        $expectedCents = isset($order['customer_total']) ? (int)round(((float)$order['customer_total']) * 100) : 0;
        $paidCents = (int)($session['amount_total'] ?? 0);
        if ($expectedCents > 0 && $paidCents > 0 && $expectedCents !== $paidCents) {
            error_log('ANY AI CAM Stripe paid amount did not match internal order total for order ' . $safeOrderId);
            return;
        }
        $sessionId = (string)($session['id'] ?? $sessionId);

        $pdo->beginTransaction();
        $stmt = $pdo->prepare(
            'SELECT o.id,o.order_code,o.customer_total,o.order_json,o.quote_id
             FROM orders o
             WHERE o.order_code = ?
             LIMIT 1
             FOR UPDATE'
        );
        $stmt->execute([$safeOrderId]);
        $order = $stmt->fetch();
        if (!$order) {
            $pdo->rollBack();
            return;
        }

        $orderJson = json_decode((string)$order['order_json'], true);
        if (!is_array($orderJson)) $orderJson = [];
        $expectedCents = isset($order['customer_total']) ? (int)round(((float)$order['customer_total']) * 100) : $expectedCents;
        if ($expectedCents > 0 && $paidCents > 0 && $expectedCents !== $paidCents) {
            $pdo->rollBack();
            error_log('ANY AI CAM Stripe paid amount changed before order update for order ' . $safeOrderId);
            return;
        }
        $stripe = isset($orderJson['stripe']) && is_array($orderJson['stripe']) ? $orderJson['stripe'] : [];
        $stripe['checkout_session_id'] = $sessionId;
        $stripe['payment_intent_id'] = (string)($session['payment_intent'] ?? '');
        $stripe['payment_status'] = 'paid';
        $stripe['amount_total'] = (int)($session['amount_total'] ?? 0);
        $stripe['currency'] = (string)($session['currency'] ?? 'usd');
        $stripe['paid_at'] = $stripe['paid_at'] ?? gmdate('c');
        $orderJson['stripe'] = $stripe;
        $orderJson['payment'] = [
            'status' => 'paid',
            'amount_paid_today' => $stripe['amount_total'],
            'currency' => $stripe['currency'],
            'paid_at' => $stripe['paid_at'],
        ];
        $activation = is_array($orderJson['activation'] ?? null) ? $orderJson['activation'] : [];
        $history = is_array($activation['history'] ?? null) ? $activation['history'] : [];
        $history[] = ['status' => 'paid', 'note' => 'Stripe payment verified.', 'updated_at' => gmdate('c')];
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
    } catch (Throwable $e) {
        if (isset($pdo) && $pdo instanceof PDO && $pdo->inTransaction()) $pdo->rollBack();
        error_log('ANY AI CAM payment success verification failed: ' . $e->getMessage());
    }
}

function load_order_details(string $safeOrderId): array {
    if ($safeOrderId === '') return [];
    $pdo = connect_db();
    if (!$pdo) return [];
    try {
        $stmt = $pdo->prepare(
            'SELECT o.order_code,o.status,o.customer_email,o.customer_total,o.order_json,
                    q.quote_code,p.partner_code
             FROM orders o
             LEFT JOIN quotes q ON q.id=o.quote_id
             LEFT JOIN partners p ON p.id=o.partner_id
             WHERE o.order_code = ?
             LIMIT 1'
        );
        $stmt->execute([$safeOrderId]);
        $order = $stmt->fetch();
        if (!$order) return [];

        $orderJson = json_decode((string)$order['order_json'], true);
        if (!is_array($orderJson)) $orderJson = [];
        $customer = is_array($orderJson['customer'] ?? null) ? $orderJson['customer'] : [];
        $payment = is_array($orderJson['payment'] ?? null) ? $orderJson['payment'] : [];
        $stripe = is_array($orderJson['stripe'] ?? null) ? $orderJson['stripe'] : [];

        $adapter = (string)($orderJson['adapter'] ?? $orderJson['adapter_type'] ?? '');
        $adapterQty = max(1, (int)($orderJson['adapterQty'] ?? $orderJson['adapter_quantity'] ?? 1));
        $amountPaidToday = 'Not available';
        if (isset($payment['amount_paid_today'])) {
            $amountPaidToday = money_value($payment['amount_paid_today'], true);
        } elseif (isset($stripe['amount_total'])) {
            $amountPaidToday = money_value($stripe['amount_total'], true);
        } elseif (isset($orderJson['dueTodayTotal'])) {
            $amountPaidToday = money_value($orderJson['dueTodayTotal']);
        } elseif (isset($order['customer_total'])) {
            $amountPaidToday = money_value($order['customer_total']);
        } elseif (isset($orderJson['adapterTotal'])) {
            $amountPaidToday = money_value($orderJson['adapterTotal']);
        } elseif (isset($orderJson['total'])) {
            $amountPaidToday = money_value($orderJson['total']);
        }

        return [
            'order_id' => (string)$order['order_code'],
            'quote_id' => (string)($order['quote_code'] ?? ''),
            'partner_id' => (string)($order['partner_code'] ?? ''),
            'customer_name' => text_value($customer['name'] ?? $orderJson['customerName'] ?? 'Customer'),
            'customer_email' => text_value($customer['email'] ?? $order['customer_email'] ?? '', ''),
            'customer_phone' => text_value($customer['phone'] ?? '', ''),
            'site_address' => order_address_text($customer),
            'amount_paid_today' => $amountPaidToday,
            'adapter_details' => adapter_label($adapter) . ' - Qty ' . $adapterQty,
            'camera_count' => (string)(int)($orderJson['cameraCount'] ?? 0),
            'cloud_plan' => strtoupper(text_value($orderJson['resolution'] ?? '')) . ', '
                . text_value($orderJson['duration'] ?? '') . '-day '
                . (((string)($orderJson['mode'] ?? '')) === 'continuous' ? '24/7' : 'motion'),
            'monthly_after_trial' => isset($orderJson['futureCloudBillingMonthly'])
                ? money_value($orderJson['futureCloudBillingMonthly'])
                : (isset($orderJson['customerTotal']) ? money_value($orderJson['customerTotal']) : 'Not available'),
            'analytics' => analytics_text($orderJson),
            'itemized_rows' => itemized_rows_text($orderJson),
            'customer_itemized_rows' => preg_replace('/\n  Margin: [^\n]+/', '', itemized_rows_text($orderJson)),
            'wizard_summary' => saved_wizard_summary($orderJson),
            'customer_total' => isset($orderJson['customerTotal']) ? money_value($orderJson['customerTotal']) : (isset($order['customer_total']) ? money_value($order['customer_total']) : 'Not available'),
            'company_cost' => isset($orderJson['companyCost']) ? money_value($orderJson['companyCost']) : 'Not available',
            'gross_margin' => isset($orderJson['grossMargin']) ? money_value($orderJson['grossMargin']) : 'Not available',
            'commission' => isset($orderJson['repCommission']) ? money_value($orderJson['repCommission']) : 'Not available',
        ];
    } catch (Throwable $e) {
        error_log('ANY AI CAM payment success order lookup failed: ' . $e->getMessage());
        return [];
    }
}

function mail_marker_path(string $orderId): ?string {
    $baseDirs = [__DIR__ . '/.anyaicam-mail-sent', sys_get_temp_dir() . '/anyaicam-mail-sent'];
    foreach ($baseDirs as $dir) {
        if (!is_dir($dir)) @mkdir($dir, 0755, true);
        if (is_dir($dir) && is_writable($dir)) {
            return rtrim($dir, DIRECTORY_SEPARATOR) . DIRECTORY_SEPARATOR . hash('sha256', $orderId) . '.sent';
        }
    }
    return null;
}

function mail_headers(string $fromEmail, string $replyTo, array $extra = []): array {
    return array_merge([
        'From: ANY AI CAM Orders <' . $fromEmail . '>',
        'Reply-To: ' . $replyTo,
        'Content-Type: text/plain; charset=UTF-8',
        'X-Mailer: ANY AI CAM Website',
    ], $extra);
}

function send_plain_mail(string $to, string $subject, string $body, array $headers, string $envelopeFrom): bool {
    if (!filter_var($to, FILTER_VALIDATE_EMAIL)) return false;
    $params = filter_var($envelopeFrom, FILTER_VALIDATE_EMAIL) ? '-f' . $envelopeFrom : '';
    return $params !== ''
        ? @mail($to, $subject, $body, implode("\r\n", $headers), $params)
        : @mail($to, $subject, $body, implode("\r\n", $headers));
}

function send_order_email_once(array $details, string $supportEmail, string $supportPhone, string $adminEmail, string $fromEmail): string {
    $orderId = (string)($details['order_id'] ?? '');
    if ($orderId === '') return 'not_available';

    $marker = mail_marker_path($orderId . '-v3-itemized');
    if ($marker && is_file($marker)) return 'already_sent';

    $subject = 'ANY AI CAM itemized order received - ' . $orderId;
    $customerLines = [
        'ANY AI CAM Order Received',
        '',
        'Customer Information',
        'Customer: ' . ($details['customer_name'] ?? 'Customer'),
        'Customer email: ' . (($details['customer_email'] ?? '') ?: 'Not available'),
        'Customer phone: ' . (($details['customer_phone'] ?? '') ?: 'Not available'),
        'Installation/site: ' . (($details['site_address'] ?? '') ?: 'Not available'),
        '',
        'Order Information',
        'Order ID: ' . ($details['order_id'] ?? 'Not available'),
        'Quote ID: ' . (($details['quote_id'] ?? '') ?: 'Not available'),
        'Payment status: Paid',
        'Amount paid today: ' . ($details['amount_paid_today'] ?? 'Not available'),
        '',
        'Itemized Order',
        preg_replace('/\n  Margin: [^\n]+/', '', $details['itemized_rows'] ?? 'No itemized order rows were saved for this order.'),
    ];
    if (!empty($details['wizard_summary'])) {
        $customerLines[] = '';
        $customerLines[] = 'Original Wizard Order Details';
        $customerLines[] = (string)$details['wizard_summary'];
    }
    $customerLines = array_merge($customerLines, [
        '',
        'Order Summary',
        'Adapter/order details: ' . ($details['adapter_details'] ?? 'Not available'),
        'Cameras: ' . ($details['camera_count'] ?? 'Not available'),
        'Cloud plan: ' . ($details['cloud_plan'] ?? 'Not available'),
        'Monthly after trial: ' . ($details['monthly_after_trial'] ?? 'Not available'),
        'AI add-ons: ' . ($details['analytics'] ?? 'No AI add-ons'),
        '',
        'Shipping: adapter delivery normally occurs within 7 days after the order is confirmed.',
        'Cloud billing begins after the 30-day free trial.',
        '',
        'Support: ' . $supportEmail . ' | ' . $supportPhone,
    ]);
    $adminLines = array_merge($customerLines, [
        '',
        'Internal Partner / Financial Details',
        'Partner ID: ' . (($details['partner_id'] ?? '') ?: 'Not available'),
        'Customer total: ' . ($details['customer_total'] ?? 'Not available'),
        'Company cost: ' . ($details['company_cost'] ?? 'Not available'),
        'Gross margin: ' . ($details['gross_margin'] ?? 'Not available'),
        'Partner commission: ' . ($details['commission'] ?? 'Not available'),
        '',
        'Admin itemization including margin',
        $details['itemized_rows'] ?? 'No itemized order rows were saved for this order.',
    ]);
    $customerBody = implode("\n", $customerLines) . "\n";
    $adminBody = implode("\n", $adminLines) . "\n";

    $baseHeaders = mail_headers($fromEmail, $supportEmail);
    $adminSent = send_plain_mail($adminEmail, '[Admin] ' . $subject, $adminBody, $baseHeaders, $fromEmail);

    $customerSent = false;
    $customerEmail = trim((string)($details['customer_email'] ?? ''));
    if ($customerEmail !== '' && filter_var($customerEmail, FILTER_VALIDATE_EMAIL)) {
        $customerHeaders = $baseHeaders;
        if (filter_var($adminEmail, FILTER_VALIDATE_EMAIL) && strcasecmp($adminEmail, $customerEmail) !== 0) {
            $customerHeaders[] = 'Bcc: ' . $adminEmail;
        }
        $customerSent = send_plain_mail($customerEmail, $subject, $customerBody, $customerHeaders, $fromEmail);
    }

    error_log('ANY AI CAM itemized order mail status for order ' . $orderId . ': admin=' . ($adminSent ? 'accepted' : 'not_accepted') . ', customer=' . ($customerSent ? 'accepted' : 'not_accepted'));

    if ($adminSent) {
        if ($marker) @file_put_contents($marker, gmdate('c'));
        return $customerSent ? 'sent' : 'admin_sent';
    }

    if ($customerSent) {
        return 'customer_sent_admin_failed';
    }

    return 'failed';
}
$paymentVerified = false;
if ($safeOrderId !== '') {
    verify_and_mark_order_paid($safeOrderId);
    $paymentVerified = true;
}
$details = load_order_details($safeOrderId);
$emailStatus = $details ? send_order_email_once($details, $supportEmail, $supportPhone, $paymentAdminEmail, $mailFromEmail) : 'not_available';

$referenceLabel = $safeOrderId !== '' ? 'Order ID' : ($safeQuoteId !== '' ? 'Quote ID' : 'Reference');
$referenceValue = $safeOrderId !== '' ? $safeOrderId : ($safeQuoteId !== '' ? $safeQuoteId : 'Stripe receipt sent by email');
$displayCustomer = $details['customer_name'] ?? 'Customer';
$displayAmount = $details['amount_paid_today'] ?? 'See Stripe receipt';
$displayAdapter = $details['adapter_details'] ?? 'ANY AI CAM adapter order';
?>
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,follow">
<title>Payment Received | ANY AI CAM</title>
<link rel="stylesheet" href="styles.css">
<style>
body{background:#f3f6fb;color:#0b1332}.wrap{max-width:860px;margin:4rem auto;padding:1rem}.card{background:#fff;border:1px solid #dce6f5;border-radius:18px;padding:2rem;box-shadow:0 18px 45px rgba(23,38,70,.12);text-align:center}.badge{display:inline-flex;align-items:center;justify-content:center;margin-bottom:1rem;padding:.45rem .8rem;border-radius:999px;background:#e8f8f0;color:#16734e;font-weight:900}.card h1{margin:.35rem 0 1rem;font-size:2rem}.detail-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.8rem;margin:1.5rem auto;text-align:left}.reference{padding:1rem;border:1px solid #dce6f5;border-radius:12px;background:#f8fafc}.reference span{display:block;color:#63708a;font-size:.78rem;font-weight:800;text-transform:uppercase}.reference strong{display:block;margin-top:.25rem;overflow-wrap:anywhere}.notice{margin:1rem auto 0;padding:1rem;border-left:5px solid #5fc4d1;background:#eefaff;border-radius:8px;text-align:left}.email-note{margin:.75rem 0 0;color:#63708a;font-size:.92rem}.itemized{background:#f8fafc;border-left-color:#7fd4dc}.itemized h2{margin:.2rem 0 .8rem;font-size:1.1rem}.itemized pre{white-space:pre-wrap;margin:0;font:inherit;line-height:1.45}.button{display:inline-block;margin-top:1.25rem;padding:.85rem 1.2rem;border-radius:9px;background:#0b2b4f;color:#fff;text-decoration:none;font-weight:800}@media(max-width:700px){.detail-grid{grid-template-columns:1fr}.card{padding:1.25rem}.wrap{margin:1rem auto}}
</style>
</head>
<body>
<main class="wrap">
<section class="card">
<div class="badge">Payment received</div>
<h1>Payment Received</h1>
<p>Thank you. Your payment was processed and your ANY AI CAM setup request has been received.</p>

<div class="detail-grid">
  <div class="reference"><span>Customer</span><strong><?= h($displayCustomer) ?></strong></div>
  <div class="reference"><span><?= h($referenceLabel) ?></span><strong><?= h($referenceValue) ?></strong></div>
  <div class="reference"><span>Amount Paid Today</span><strong><?= h($displayAmount) ?></strong></div>
  <div class="reference"><span>Adapter / Order Details</span><strong><?= h($displayAdapter) ?></strong></div>
  <?php if ($details): ?>
  <div class="reference"><span>Quote ID</span><strong><?= h($details['quote_id'] ?: 'Not available') ?></strong></div>
  <div class="reference"><span>Partner ID</span><strong><?= h($details['partner_id'] ?: 'Not available') ?></strong></div>
  <div class="reference"><span>Cameras</span><strong><?= h($details['camera_count']) ?></strong></div>
  <div class="reference"><span>Cloud Plan</span><strong><?= h($details['cloud_plan']) ?></strong></div>
  <div class="reference"><span>Monthly After Trial</span><strong><?= h($details['monthly_after_trial']) ?></strong></div>
  <div class="reference"><span>AI Add-ons</span><strong><?= h($details['analytics']) ?></strong></div>
  <?php endif; ?>
</div>

<div class="notice">
  <p><strong>Shipping:</strong> adapter delivery normally occurs within 7 days after the order is confirmed.</p>
  <p><strong>Cloud billing:</strong> cloud service billing begins after the 30-day free trial.</p>
  <p><strong>Support:</strong> <a href="mailto:<?= h($supportEmail) ?>"><?= h($supportEmail) ?></a> | <a href="tel:+18325108240"><?= h($supportPhone) ?></a></p>
</div>
<?php if ($details && !empty($details['itemized_rows'])): ?>
<div class="notice itemized">
  <h2>Order items</h2>
  <pre><?= h($details['customer_itemized_rows'] ?? $details['itemized_rows']) ?></pre>
</div>
<?php endif; ?>
<?php if ($emailStatus === 'failed' || $emailStatus === 'customer_sent_admin_failed'): ?>
  <p class="email-note">The order was received, but the website mail service did not accept the order-detail email. Please contact support with the order ID.</p>
<?php elseif ($emailStatus === 'sent' || $emailStatus === 'admin_sent' || $emailStatus === 'already_sent'): ?>
  <p class="email-note">The website submitted the order-detail email.</p>
<?php endif; ?>
<a class="button" href="index.html">Return to ANY AI CAM</a>
</section>
</main>
</body>
</html>



