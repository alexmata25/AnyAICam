<?php
declare(strict_types=1);

function redirectTo(string $path): never {
    header('Location: ' . $path, true, 303);
    exit;
}

function cleanText($value, int $max = 1000): string {
    $value = trim((string)$value);
    $value = str_replace(["\r", "\0"], '', $value);
    return mb_substr($value, 0, $max);
}

function smtpRead($socket, array $codes): bool {
    $response = '';
    while (($line = fgets($socket, 515)) !== false) {
        $response .= $line;
        if (strlen($line) < 4 || $line[3] !== '-') break;
    }
    return in_array((int)substr($response, 0, 3), $codes, true);
}

function smtpCmd($socket, string $command, array $codes): bool {
    fwrite($socket, $command . "\r\n");
    return smtpRead($socket, $codes);
}

function smtpSend(array $cfg, string $to, string $subject, string $body, string $replyTo): bool {
    foreach (['smtp_host','smtp_port','smtp_secure','smtp_username','smtp_password','from_email','from_name'] as $key) {
        if (trim((string)($cfg[$key] ?? '')) === '') return false;
    }
    if (!filter_var($to, FILTER_VALIDATE_EMAIL)) return false;

    $secure = strtolower(trim((string)$cfg['smtp_secure']));
    $host = trim((string)$cfg['smtp_host']);
    $target = $secure === 'ssl' ? 'ssl://' . $host : $host;
    $socket = @fsockopen($target, (int)$cfg['smtp_port'], $errno, $errstr, 20);
    if (!$socket) return false;
    stream_set_timeout($socket, 20);

    if (!smtpRead($socket, [220]) || !smtpCmd($socket, 'EHLO anyaicam.com', [250])) {
        fclose($socket); return false;
    }

    if ($secure === 'tls') {
        if (!smtpCmd($socket, 'STARTTLS', [220]) ||
            !stream_socket_enable_crypto($socket, true, STREAM_CRYPTO_METHOD_TLS_CLIENT) ||
            !smtpCmd($socket, 'EHLO anyaicam.com', [250])) {
            fclose($socket); return false;
        }
    }

    if (!smtpCmd($socket, 'AUTH LOGIN', [334]) ||
        !smtpCmd($socket, base64_encode((string)$cfg['smtp_username']), [334]) ||
        !smtpCmd($socket, base64_encode((string)$cfg['smtp_password']), [235])) {
        fclose($socket); return false;
    }

    $fromEmail = trim(str_replace(["\r","\n"], '', (string)$cfg['from_email']));
    $fromName = trim(str_replace(["\r","\n"], '', (string)$cfg['from_name']));
    $to = trim(str_replace(["\r","\n"], '', $to));
    $subject = trim(str_replace(["\r","\n"], '', $subject));
    $replyTo = trim(str_replace(["\r","\n"], '', $replyTo));

    if (!smtpCmd($socket, 'MAIL FROM:<' . $fromEmail . '>', [250]) ||
        !smtpCmd($socket, 'RCPT TO:<' . $to . '>', [250,251]) ||
        !smtpCmd($socket, 'DATA', [354])) {
        fclose($socket); return false;
    }

    $message = implode("\r\n", [
        'To: <' . $to . '>',
        'Subject: ' . $subject,
        'From: ' . $fromName . ' <' . $fromEmail . '>',
        'Reply-To: ' . $replyTo,
        'MIME-Version: 1.0',
        'Content-Type: text/plain; charset=UTF-8',
        '',
        str_replace(["\r\n","\r"], "\n", $body)
    ]);
    $message = str_replace("\n.", "\n..", $message);

    $ok = smtpCmd($socket, $message . "\r\n.", [250]);
    smtpCmd($socket, 'QUIT', [221,250]);
    fclose($socket);
    return $ok;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    redirectTo('software-trial-request.html');
}

$name = cleanText($_POST['name'] ?? '', 120);
$email = strtolower(cleanText($_POST['email'] ?? '', 190));
$phone = cleanText($_POST['phone'] ?? '', 60);
$siteName = cleanText($_POST['site_name'] ?? '', 150);
$notes = cleanText($_POST['notes'] ?? '', 1500);
$ack = cleanText($_POST['subscription_ack'] ?? '', 10);

if ($name === '' || !filter_var($email, FILTER_VALIDATE_EMAIL) || $ack !== 'yes') {
    redirectTo('software-trial-request.html?error=missing');
}

$reference = 'SW-' . date('Ymd') . '-' . strtoupper(bin2hex(random_bytes(3)));
$timestamp = date('Y-m-d H:i:s');

$adminBody = implode("\n", [
    'NEW FREE SOFTWARE TRIAL REQUEST',
    '',
    'Reference: ' . $reference,
    'Submitted: ' . $timestamp,
    'Name: ' . $name,
    'Email: ' . $email,
    'Phone: ' . ($phone !== '' ? $phone : 'Not provided'),
    'Business / site: ' . ($siteName !== '' ? $siteName : 'Not provided'),
    'Notes: ' . ($notes !== '' ? $notes : 'None'),
    'Subscription acknowledgment: Yes'
]);

$customerBody = implode("\n", [
    'Hello ' . $name . ',',
    '',
    'ANY AI CAM received your free software trial request.',
    'Reference: ' . $reference,
    '',
    'We will email the software instructions and next steps.',
    '',
    'Reminder: continued cloud service requires a Videoloft subscription after the free trial.',
    '',
    'ANY AI CAM',
    '(832) 510-8240',
    'amata@anyaicam.com'
]);

@file_put_contents(
    __DIR__ . '/software-trial-requests.log',
    $timestamp . "\t" . $reference . "\t" . $email . "\t" . json_encode([
        'name' => $name,
        'email' => $email,
        'phone' => $phone,
        'site_name' => $siteName,
        'notes' => $notes
    ], JSON_UNESCAPED_SLASHES) . "\n",
    FILE_APPEND | LOCK_EX
);

$mailConfigFile = __DIR__ . '/mail_config.php';
if (is_file($mailConfigFile)) {
    $mailConfig = require $mailConfigFile;
    if (is_array($mailConfig)) {
        smtpSend(
            $mailConfig,
            'amata@anyaicam.com',
            'Free Software Trial Request - ' . $reference,
            $adminBody,
            $email
        );

        smtpSend(
            $mailConfig,
            $email,
            'We received your free software trial request',
            $customerBody,
            'amata@anyaicam.com'
        );
    }
}

redirectTo('software-trial-thank-you.html');
