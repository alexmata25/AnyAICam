<?php
declare(strict_types=1);

$videoloftUrl = 'https://videoloft.com/product/virtual-cloud-adapter/';
$logFile = __DIR__ . '/software-trial-requests.log';


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

function finish(string $url): never {
    header('Location: ' . $url, true, 303);
    exit;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    finish('software-free-trial-request.html');
}

$payloadRaw = (string)($_POST['payload'] ?? '');
$data = json_decode($payloadRaw, true);
if (!is_array($data)) {
    finish('software-free-trial-request.html?error=invalid');
}

$clean = static function ($value, int $max = 500): string {
    $value = trim((string)$value);
    $value = str_replace(["\r", "\0"], '', $value);
    return mb_substr($value, 0, $max);
};

$request = [
    'name' => $clean($data['name'] ?? '', 120),
    'email' => strtolower($clean($data['email'] ?? '', 190)),
    'phone' => $clean($data['phone'] ?? '', 60),
    'siteName' => $clean($data['siteName'] ?? '', 150),
    'brand' => $clean($data['brand'] ?? '', 120),
    'model' => $clean($data['model'] ?? '', 120),
    'cameraCount' => max(1, min(128, (int)($data['cameraCount'] ?? 1))),
    'windowsComputer' => $clean($data['windowsComputer'] ?? '', 40),
    'hasLogin' => $clean($data['hasLogin'] ?? '', 20),
    'notes' => $clean($data['notes'] ?? '', 1500),
];

if (
    $request['name'] === '' ||
    !filter_var($request['email'], FILTER_VALIDATE_EMAIL) ||
    $request['phone'] === '' ||
    $request['brand'] === ''
) {
    finish('software-free-trial-request.html?error=missing');
}

$timestamp = date('Y-m-d H:i:s');
$reference = 'SW-' . date('Ymd') . '-' . strtoupper(bin2hex(random_bytes(3)));

$body = implode("\n", [
    'NEW FREE SOFTWARE TRIAL REQUEST',
    '',
    'Reference: ' . $reference,
    'Submitted: ' . $timestamp,
    'Name: ' . $request['name'],
    'Email: ' . $request['email'],
    'Phone: ' . $request['phone'],
    'Business / site: ' . ($request['siteName'] !== '' ? $request['siteName'] : 'Not provided'),
    'Camera / recorder brand: ' . $request['brand'],
    'Model: ' . ($request['model'] !== '' ? $request['model'] : 'Not provided'),
    'Camera count: ' . $request['cameraCount'],
    'Windows computer available: ' . $request['windowsComputer'],
    'Administrator login known: ' . $request['hasLogin'],
    'Notes: ' . ($request['notes'] !== '' ? $request['notes'] : 'None'),
]);

@file_put_contents(
    $logFile,
    $timestamp . "\t" . $reference . "\t" . $request['email'] . "\t" . json_encode($request, JSON_UNESCAPED_SLASHES) . "\n",
    FILE_APPEND | LOCK_EX
);

$mailConfigFile = __DIR__ . '/mail_config.php';
if (is_file($mailConfigFile)) {
    $mailConfig = require $mailConfigFile;
    if (is_array($mailConfig)) {
        $to = 'amata@anyaicam.com';
        smtpSend(
            $mailConfig,
            $to,
            'Free Software Trial Request - ' . $reference,
            $body,
            $request['email']
        );

        $customerBody = implode("\n", [
            'Hello ' . $request['name'] . ',',
            '',
            'We received your ANY AI CAM free software trial request.',
            'Reference: ' . $reference,
            '',
            'You are being directed to Videoloft’s official Virtual Cloud Adapter page.',
            'Remember: the Windows computer must remain powered on, and you will need the administrator login for at least one compatible camera or recorder.',
            '',
            'ANY AI CAM',
            '(832) 510-8240',
            'amata@anyaicam.com'
        ]);
        smtpSend(
            $mailConfig,
            $request['email'],
            'We received your free software trial request',
            $customerBody,
            'amata@anyaicam.com'
        );
    }
}

finish($videoloftUrl);
