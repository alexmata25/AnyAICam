<?php
declare(strict_types=1);

/**
 * Post-payment email helper for partner-generated Stripe orders.
 * Card data is never received, stored, or logged by this file.
 */

function aic_clean_header(string $value): string {
    return trim(str_replace(["\r", "\n"], '', $value));
}

function aic_smtp_expect($socket, array $codes): array {
    $response = '';
    while (($line = fgets($socket, 515)) !== false) {
        $response .= $line;
        if (strlen($line) < 4 || $line[3] !== '-') break;
    }
    $code = (int)substr($response, 0, 3);
    return [in_array($code, $codes, true), trim($response)];
}

function aic_smtp_command($socket, string $command, array $codes): array {
    fwrite($socket, $command . "\r\n");
    return aic_smtp_expect($socket, $codes);
}

function aic_send_smtp(array $config, string $to, string $subject, string $body, array $cc = []): array {
    foreach (['smtp_host','smtp_port','smtp_secure','smtp_username','smtp_password','from_email','from_name','support_email'] as $key) {
        if (!isset($config[$key]) || $config[$key] === '') {
            return [false, 'Mail configuration is incomplete.'];
        }
    }

    $to = aic_clean_header($to);
    if (!filter_var($to, FILTER_VALIDATE_EMAIL)) return [false, 'Recipient address is invalid.'];

    $cc = array_values(array_unique(array_filter(array_map('aic_clean_header', $cc), static function ($email) use ($to) {
        return $email !== $to && filter_var($email, FILTER_VALIDATE_EMAIL);
    })));

    $host = (string)$config['smtp_host'];
    $port = (int)$config['smtp_port'];
    $secure = strtolower((string)$config['smtp_secure']);
    $target = $secure === 'ssl' ? 'ssl://' . $host : $host;

    $socket = @fsockopen($target, $port, $errno, $errstr, 20);
    if (!$socket) return [false, 'SMTP connection failed.'];
    stream_set_timeout($socket, 20);

    [$ok] = aic_smtp_expect($socket, [220]);
    if (!$ok) { fclose($socket); return [false, 'SMTP greeting failed.']; }
    [$ok] = aic_smtp_command($socket, 'EHLO anyaicam.com', [250]);
    if (!$ok) { fclose($socket); return [false, 'SMTP EHLO failed.']; }

    if ($secure === 'tls') {
        [$ok] = aic_smtp_command($socket, 'STARTTLS', [220]);
        if (!$ok || !stream_socket_enable_crypto($socket, true, STREAM_CRYPTO_METHOD_TLS_CLIENT)) {
            fclose($socket);
            return [false, 'SMTP TLS failed.'];
        }
        [$ok] = aic_smtp_command($socket, 'EHLO anyaicam.com', [250]);
        if (!$ok) { fclose($socket); return [false, 'SMTP EHLO after TLS failed.']; }
    }

    foreach ([
        ['AUTH LOGIN', [334]],
        [base64_encode((string)$config['smtp_username']), [334]],
        [base64_encode((string)$config['smtp_password']), [235]],
    ] as [$command, $codes]) {
        [$ok] = aic_smtp_command($socket, $command, $codes);
        if (!$ok) { fclose($socket); return [false, 'SMTP authentication failed.']; }
    }

    $fromEmail = aic_clean_header((string)$config['from_email']);
    $fromName = aic_clean_header((string)$config['from_name']);
    $supportEmail = aic_clean_header((string)$config['support_email']);
    $subject = aic_clean_header($subject);

    [$ok] = aic_smtp_command($socket, 'MAIL FROM:<' . $fromEmail . '>', [250]);
    if (!$ok) { fclose($socket); return [false, 'SMTP sender setup failed.']; }
    foreach (array_merge([$to], $cc) as $recipient) {
        [$ok] = aic_smtp_command($socket, 'RCPT TO:<' . $recipient . '>', [250, 251]);
        if (!$ok) { fclose($socket); return [false, 'SMTP recipient setup failed.']; }
    }
    [$ok] = aic_smtp_command($socket, 'DATA', [354]);
    if (!$ok) { fclose($socket); return [false, 'SMTP message setup failed.']; }

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
    if ($cc) $headers[] = 'Cc: ' . implode(', ', $cc);

    $data = implode("\r\n", [
        'To: <' . $to . '>',
        'Subject: ' . $subject,
        implode("\r\n", $headers),
        '',
        str_replace(["\r\n", "\r"], "\n", $body),
    ]);
    $data = str_replace("\n.", "\n..", $data);

    [$ok] = aic_smtp_command($socket, $data . "\r\n.", [250]);
    aic_smtp_command($socket, 'QUIT', [221, 250]);
    fclose($socket);
    return [$ok, $ok ? 'Sent' : 'SMTP message failed.'];
}

function aic_email_log(string $orderCode, string $eventId, string $recipientType, string $status, string $detail): void {
    $safeDetail = preg_replace('/[\r\n]+/', ' ', $detail) ?: '';
    $line = sprintf(
        "%s - partner-payment-email - %s - order=%s - event=%s - recipient=%s - %s\n",
        date('Y-m-d H:i:s'),
        strtoupper($status),
        $orderCode,
        $eventId,
        $recipientType,
        $safeDetail
    );
    @file_put_contents(__DIR__ . '/email-log.txt', $line, FILE_APPEND | LOCK_EX);
}

function aic_money_from_cents(int $cents, string $currency = 'usd'): string {
    return strtoupper($currency) . ' $' . number_format($cents / 100, 2);
}

function aic_adapter_details(array $orderJson, array $session): array {
    $adapterKey = (string)($orderJson['adapter'] ?? '');
    $metadataType = (string)($session['metadata']['adapter_type'] ?? '');
    $map = [
        'webhookTest' => ['Webhook test item', 50],
        'adapter8' => ['Videoloft Cloud Adapter Mini (8-channel)', 34900],
        'adapter16' => ['Videoloft Cloud Adapter (16-channel)', 39900],
        'adapter32' => ['Videoloft Enterprise Cloud Adapter (32-channel)', 214900],
        'adapter64' => ['Videoloft Enterprise Cloud Adapter (64-channel)', 268900],
        'webhook_test' => ['Webhook test item', 50],
        '8channel' => ['Videoloft Cloud Adapter Mini (8-channel)', 34900],
        '16channel' => ['Videoloft Cloud Adapter (16-channel)', 39900],
        '32channel' => ['Videoloft Enterprise Cloud Adapter (32-channel)', 214900],
        '64channel' => ['Videoloft Enterprise Cloud Adapter (64-channel)', 268900],
    ];
    [$name, $unitCents] = $map[$adapterKey] ?? ($map[$metadataType] ?? ['Videoloft Cloud Adapter', 0]);
    $quantity = max(1, (int)($orderJson['adapterQty'] ?? 1));
    return [$name, $quantity, $unitCents];
}

function aic_cloud_details(array $orderJson): array {
    $cameraCount = max(0, (int)($orderJson['cameraCount'] ?? 0));
    $resolution = strtoupper((string)($orderJson['resolution'] ?? 'Not specified'));
    $recording = (($orderJson['mode'] ?? '') === 'continuous') ? '24/7 continuous recording' : 'Motion recording';
    $retention = (string)($orderJson['duration'] ?? 'Not specified') . '-day retention';
    $billing = (($orderJson['billing'] ?? '') === 'annual') ? 'Annual' : 'Monthly';
    $analytics = [];
    foreach (($orderJson['analytics'] ?? []) as $item) {
        if (!is_array($item)) continue;
        $name = (string)($item['name'] ?? $item['key'] ?? 'AI add-on');
        $quantity = max(0, (int)($item['quantity'] ?? 0));
        $unitPrice = (float)($item['unitPrice'] ?? 0);
        if ($quantity > 0) {
            $period = (($orderJson['billing'] ?? '') === 'annual') ? 'year' : 'month';
            $analytics[] = $name . ': ' . $quantity . ' camera' . ($quantity === 1 ? '' : 's')
                . ' x $' . number_format($unitPrice, 2) . ' / ' . $period
                . ' = $' . number_format($unitPrice * $quantity, 2);
        }
    }
    return [
        'camera_count' => $cameraCount,
        'resolution' => $resolution,
        'recording' => $recording,
        'retention' => $retention,
        'billing' => $billing,
        'analytics' => $analytics ? implode('; ', $analytics) : 'None recorded on the locked quote',
        'service' => (string)($orderJson['serviceName'] ?? 'ANY AI CAM guided cloud service'),
    ];
}

function aic_build_confirmation_messages(array $context): array {
    $session = $context['session'];
    $orderJson = $context['order_json'];
    [$adapterName, $quantity, $unitCents] = aic_adapter_details($orderJson, $session);
    $cloud = aic_cloud_details($orderJson);

    $currency = (string)($session['currency'] ?? 'usd');
    $amountTotal = (int)($session['amount_total'] ?? 0);
    $details = is_array($session['total_details'] ?? null) ? $session['total_details'] : [];
    $shippingCents = (int)($details['amount_shipping'] ?? 0);
    $taxCents = (int)($details['amount_tax'] ?? 0);
    $hardwareCents = max(0, $amountTotal - $shippingCents - $taxCents);
    if ($hardwareCents === 0 && $unitCents > 0) $hardwareCents = $unitCents * $quantity;

    $name = trim((string)$context['customer_name']) ?: 'Customer';
    $addressLines = array_values(array_filter([
        trim((string)$context['address_line']),
        trim(implode(', ', array_filter([(string)$context['city'], (string)$context['state']])) . ' ' . (string)$context['postal_code']),
    ]));
    $shippingAddress = $addressLines ? implode("\n", $addressLines) : 'Shipping address on the locked order';

    $customerBody = "Hello {$name},\n\n"
        . "Your ANY AI CAM adapter payment has been confirmed.\n\n"
        . "ORDER INFORMATION\n"
        . "Order number: {$context['order_code']}\n"
        . "Quote ID: {$context['quote_code']}\n\n"
        . "PAID TODAY\n"
        . "Adapter: {$adapterName}\n"
        . "Quantity: {$quantity}\n"
        . "Hardware subtotal: " . aic_money_from_cents($hardwareCents, $currency) . "\n"
        . "Shipping: " . aic_money_from_cents($shippingCents, $currency) . "\n"
        . "Sales tax: " . aic_money_from_cents($taxCents, $currency) . "\n"
        . "Total paid today: " . aic_money_from_cents($amountTotal, $currency) . "\n\n"
        . "SHIPPING ADDRESS\n{$shippingAddress}\n\n"
        . "Expected shipping time is up to 7 days. If an unexpected delay occurs, ANY AI CAM will contact you with an updated estimate and available options.\n\n"
        . "CLOUD SERVICE AFTER THE 30-DAY FREE TRIAL\n"
        . "Cameras: {$cloud['camera_count']}\n"
        . "Resolution: {$cloud['resolution']}\n"
        . "Recording: {$cloud['recording']}\n"
        . "Retention: {$cloud['retention']}\n"
        . "Cloud billing selection: {$cloud['billing']}\n"
        . "AI add-ons: {$cloud['analytics']}\n\n"
        . "Videoloft cloud billing begins after the 30-day free trial and is billed directly by Videoloft.\n\n"
        . "Questions? Contact support@anyaicam.com or (832) 510-8240.\n\n"
        . "Thank you,\nANY AI CAM\n";

    $adminBody = "A partner-generated customer payment was confirmed by Stripe.\n\n"
        . "CUSTOMER\n"
        . "Name: {$name}\n"
        . "Email: {$context['customer_email']}\n"
        . "Phone: {$context['customer_phone']}\n"
        . "Site: {$context['site_name']}\n\n"
        . "SHIPPING ADDRESS\n{$shippingAddress}\n\n"
        . "TRACKING\n"
        . "Partner ID: {$context['partner_code']}\n"
        . "Quote ID: {$context['quote_code']}\n"
        . "Order ID: {$context['order_code']}\n"
        . "Stripe Checkout Session ID: " . (string)($session['id'] ?? '') . "\n"
        . "Stripe Payment Intent ID: " . (string)($session['payment_intent'] ?? '') . "\n"
        . "Stripe Event ID: {$context['event_id']}\n\n"
        . "PAYMENT\n"
        . "Adapter: {$adapterName}\n"
        . "Quantity: {$quantity}\n"
        . "Hardware subtotal: " . aic_money_from_cents($hardwareCents, $currency) . "\n"
        . "Shipping: " . aic_money_from_cents($shippingCents, $currency) . "\n"
        . "Sales tax: " . aic_money_from_cents($taxCents, $currency) . "\n"
        . "Total paid: " . aic_money_from_cents($amountTotal, $currency) . "\n\n"
        . "CLOUD PLAN\n"
        . "Cameras: {$cloud['camera_count']}\n"
        . "Resolution: {$cloud['resolution']}\n"
        . "Recording: {$cloud['recording']}\n"
        . "Retention: {$cloud['retention']}\n"
        . "Billing selection: {$cloud['billing']}\n"
        . "Service: {$cloud['service']}\n"
        . "AI add-ons: {$cloud['analytics']}\n\n"
        . "No payment card details are stored or included in this email.\n";

    return [
        'customer_subject' => 'ANY AI CAM payment confirmed — ' . $context['order_code'],
        'customer_body' => $customerBody,
        'admin_subject' => 'Paid partner order — ' . $context['order_code'],
        'admin_body' => $adminBody,
    ];
}

function aic_claim_email(PDO $pdo, int $orderDbId, string $recipientType, string $eventId): string {
    $pdo->beginTransaction();
    try {
        $stmt = $pdo->prepare('SELECT order_json FROM orders WHERE id=? FOR UPDATE');
        $stmt->execute([$orderDbId]);
        $json = json_decode((string)$stmt->fetchColumn(), true);
        if (!is_array($json)) $json = [];
        $emails = is_array($json['confirmation_emails'] ?? null) ? $json['confirmation_emails'] : [];
        $current = is_array($emails[$recipientType] ?? null) ? $emails[$recipientType] : [];
        if (($current['status'] ?? '') === 'sent') {
            $pdo->commit();
            return 'already_sent';
        }
        $claimedAt = strtotime((string)($current['claimed_at'] ?? '')) ?: 0;
        if (($current['status'] ?? '') === 'sending' && $claimedAt > time() - 600) {
            $pdo->commit();
            return 'busy';
        }
        $emails[$recipientType] = [
            'status' => 'sending',
            'event_id' => $eventId,
            'claimed_at' => gmdate('c'),
            'attempts' => ((int)($current['attempts'] ?? 0)) + 1,
        ];
        $json['confirmation_emails'] = $emails;
        $pdo->prepare('UPDATE orders SET order_json=? WHERE id=?')->execute([
            json_encode($json, JSON_UNESCAPED_SLASHES),
            $orderDbId,
        ]);
        $pdo->commit();
        return 'claimed';
    } catch (Throwable $e) {
        if ($pdo->inTransaction()) $pdo->rollBack();
        throw $e;
    }
}

function aic_finish_email(PDO $pdo, int $orderDbId, string $recipientType, string $eventId, bool $sent, string $message): void {
    $pdo->beginTransaction();
    try {
        $stmt = $pdo->prepare('SELECT order_json FROM orders WHERE id=? FOR UPDATE');
        $stmt->execute([$orderDbId]);
        $json = json_decode((string)$stmt->fetchColumn(), true);
        if (!is_array($json)) $json = [];
        $emails = is_array($json['confirmation_emails'] ?? null) ? $json['confirmation_emails'] : [];
        $current = is_array($emails[$recipientType] ?? null) ? $emails[$recipientType] : [];
        $resultEntry = array_merge($current, [
            'status' => $sent ? 'sent' : 'failed',
            'event_id' => $eventId,
            'result' => $message,
        ]);
        $resultEntry[$sent ? 'sent_at' : 'failed_at'] = gmdate('c');
        $emails[$recipientType] = $resultEntry;
        $json['confirmation_emails'] = $emails;
        $pdo->prepare('UPDATE orders SET order_json=? WHERE id=?')->execute([
            json_encode($json, JSON_UNESCAPED_SLASHES),
            $orderDbId,
        ]);
        $pdo->commit();
    } catch (Throwable $e) {
        if ($pdo->inTransaction()) $pdo->rollBack();
        throw $e;
    }
}

function aic_send_partner_order_confirmations(PDO $pdo, array $context): array {
    $mailFile = __DIR__ . '/mail_config.php';
    if (!is_file($mailFile)) return [false, 'Mail configuration is missing.'];
    $mailConfig = require $mailFile;
    if (!is_array($mailConfig)) return [false, 'Mail configuration is invalid.'];

    $messages = aic_build_confirmation_messages($context);
    $jobs = [
        'customer' => [
            'to' => (string)$context['customer_email'],
            'cc' => [],
            'subject' => $messages['customer_subject'],
            'body' => $messages['customer_body'],
        ],
        'admin' => [
            'to' => 'amata@anyaicam.com',
            'cc' => ['support@anyaicam.com'],
            'subject' => $messages['admin_subject'],
            'body' => $messages['admin_body'],
        ],
    ];

    $allSent = true;
    $results = [];
    foreach ($jobs as $recipientType => $job) {
        $claim = aic_claim_email($pdo, (int)$context['order_db_id'], $recipientType, (string)$context['event_id']);
        if ($claim === 'already_sent') {
            $results[$recipientType] = 'already_sent';
            aic_email_log($context['order_code'], $context['event_id'], $recipientType, 'skipped', 'Already sent.');
            continue;
        }
        if ($claim === 'busy') {
            $results[$recipientType] = 'busy';
            $allSent = false;
            aic_email_log($context['order_code'], $context['event_id'], $recipientType, 'deferred', 'Another webhook is sending this email.');
            continue;
        }

        [$sent, $message] = aic_send_smtp($mailConfig, $job['to'], $job['subject'], $job['body'], $job['cc']);
        aic_finish_email($pdo, (int)$context['order_db_id'], $recipientType, (string)$context['event_id'], $sent, $message);
        aic_email_log($context['order_code'], $context['event_id'], $recipientType, $sent ? 'success' : 'failed', $message);
        $results[$recipientType] = $sent ? 'sent' : 'failed';
        if (!$sent) $allSent = false;
    }
    return [$allSent, $results];
}
