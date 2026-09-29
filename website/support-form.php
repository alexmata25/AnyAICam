<?php
declare(strict_types=1);

date_default_timezone_set('America/Chicago');

function support_field(string $name): string
{
    return trim((string)($_POST[$name] ?? ''));
}

function support_clean_line(string $value): string
{
    return trim(str_replace(["\r", "\n"], ' ', $value));
}

function support_header_value(string $value): string
{
    return preg_replace('/[^a-zA-Z0-9 @._+\-,]/', '', support_clean_line($value)) ?? '';
}

function support_respond(bool $ok, string $message, int $statusCode = 200): void
{
    http_response_code($statusCode);
    header('Content-Type: text/html; charset=utf-8');

    $title = $ok ? 'Support Request Received' : 'Support Request Not Sent';
    $safeMessage = htmlspecialchars($message, ENT_QUOTES, 'UTF-8');

    echo <<<HTML
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{$title} | ANYAICAM</title>
  <style>
    body { font-family: Arial, sans-serif; background:#f4f7fb; color:#080d2b; margin:0; display:grid; min-height:100vh; place-items:center; }
    main { max-width:720px; background:#fff; border:1px solid #dbe5f4; border-radius:16px; padding:36px; box-shadow:0 20px 60px rgba(8,13,43,.12); }
    a { color:#1677c8; font-weight:700; }
  </style>
</head>
<body>
  <main>
    <h1>{$title}</h1>
    <p>{$safeMessage}</p>
    <p><a href="support.html#supportForm">Back to support form</a></p>
  </main>
</body>
</html>
HTML;
    exit;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    header('Location: support.html');
    exit;
}

$fullName = support_clean_line(support_field('fullName'));
$email = filter_var(support_field('email'), FILTER_VALIDATE_EMAIL) ? support_field('email') : '';
$cameraCount = support_clean_line(support_field('cameraCount'));

if ($fullName === '' || $email === '' || $cameraCount === '') {
    support_respond(false, 'Please complete your name, email, and number of cameras before sending the request.', 400);
}

$requestId = 'ST-' . date('Ymd-His') . '-' . strtoupper(substr(md5((string)microtime(true)), 0, 6));
$fields = [
    'Request ID' => $requestId,
    'Full Name' => $fullName,
    'Business Name' => support_clean_line(support_field('businessName')),
    'Email' => $email,
    'Phone Number' => support_clean_line(support_field('phone')),
    'Number of Cameras' => $cameraCount,
    'Camera Brand' => support_clean_line(support_field('cameraBrand')),
    'Customer Computer' => support_clean_line(support_field('computerOS')),
    'Preferred Session Time' => support_clean_line(support_field('preferredTime')),
    'Has Activation Request' => support_clean_line(support_field('hasActivationRequest')),
    'Notes or Questions' => trim(support_field('notes')),
];

$bodyLines = [
    'New ANYAICAM Live Tech Support Request',
    '',
];

foreach ($fields as $label => $value) {
    $bodyLines[] = $label . ': ' . ($value !== '' ? $value : 'Not provided');
}

$bodyLines[] = '';
$bodyLines[] = 'Security reminder: Do not request camera passwords, payment card details, or private access codes by email.';
$body = implode("\n", $bodyLines);

$savedSubmission = "=== " . date('Y-m-d H:i:s') . " ===\n" . $body . "\n" . str_repeat('-', 40) . "\n";
@file_put_contents(__DIR__ . '/submissions-support.txt', $savedSubmission, FILE_APPEND | LOCK_EX);

$to = 'amata@anyaicam.com';
$subject = 'ANYAICAM Live Tech Support Request ' . $requestId;
$fromEmail = 'amata@anyaicam.com';
$fromName = 'ANYAICAM Website';
$replyName = support_header_value($fullName);

$headers = [
    'From: ' . $fromName . ' <' . $fromEmail . '>',
    'Reply-To: ' . $replyName . ' <' . $email . '>',
    'MIME-Version: 1.0',
    'Content-Type: text/plain; charset=UTF-8',
    'X-Mailer: PHP/' . phpversion(),
];

$sent = @mail($to, $subject, $body, implode("\r\n", $headers), '-f ' . $fromEmail);
$logLine = date('Y-m-d H:i:s') . ' - support - ' . ($sent ? 'SUCCESS' : 'SAVED_ONLY') . ' - ' . $requestId . "\n";
@file_put_contents(__DIR__ . '/email-log.txt', $logLine, FILE_APPEND | LOCK_EX);

if ($sent) {
    support_respond(true, 'Thanks. We received your live tech support request and will follow up shortly.');
}

support_respond(true, 'Thanks. Your support request was saved. If you do not hear back shortly, please email amata@anyaicam.com directly.');
