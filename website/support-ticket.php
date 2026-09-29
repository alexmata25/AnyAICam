<?php
declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['ok' => false, 'error' => 'Method not allowed']);
    exit;
}

$raw = file_get_contents('php://input');
$data = json_decode($raw ?: '', true);

if (!is_array($data)) {
    http_response_code(400);
    echo json_encode(['ok' => false, 'error' => 'Invalid request']);
    exit;
}

function clean($value, int $max = 500): string {
    $value = trim((string)$value);
    $value = preg_replace('/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/u', '', $value) ?? '';
    return mb_substr($value, 0, $max);
}

$name = clean($data['name'] ?? '', 120);
$email = clean($data['email'] ?? '', 180);
$phone = clean($data['phone'] ?? '', 80);
$summary = clean($data['summary'] ?? '', 8000);

if ($email !== 'Not provided' && $email !== '' && !filter_var($email, FILTER_VALIDATE_EMAIL)) {
    http_response_code(422);
    echo json_encode(['ok' => false, 'error' => 'Please enter a valid email address']);
    exit;
}

if ($summary === '') {
    http_response_code(422);
    echo json_encode(['ok' => false, 'error' => 'Diagnostic summary is required']);
    exit;
}

$ticketId = 'AIC-S-' . gmdate('Ymd') . '-' . strtoupper(bin2hex(random_bytes(4)));
$createdAt = gmdate('c');

$record = [
    'ticket_id' => $ticketId,
    'created_at_utc' => $createdAt,
    'ip_hash' => hash('sha256', ($_SERVER['REMOTE_ADDR'] ?? '') . 'ANYAICAM-SUPPORT'),
    'name' => $name,
    'email' => $email,
    'phone' => $phone,
    'issue' => clean($data['issue'] ?? '', 100),
    'system' => clean($data['system'] ?? '', 100),
    'credential_status' => clean($data['credential_status'] ?? '', 100),
    'brand' => clean($data['brand'] ?? '', 120),
    'model' => clean($data['model'] ?? '', 120),
    'camera_count' => clean($data['camera_count'] ?? '', 40),
    'router' => clean($data['router'] ?? '', 120),
    'local_visible' => clean($data['local_visible'] ?? '', 40),
    'adapter_powered' => clean($data['adapter_powered'] ?? '', 40),
    'ethernet' => clean($data['ethernet'] ?? '', 40),
    'same_network' => clean($data['same_network'] ?? '', 40),
    'notes' => clean($data['notes'] ?? '', 2000),
    'summary' => $summary
];

$storageDir = __DIR__ . '/support-private';
if (!is_dir($storageDir) && !mkdir($storageDir, 0750, true) && !is_dir($storageDir)) {
    http_response_code(500);
    echo json_encode(['ok' => false, 'error' => 'Could not create storage']);
    exit;
}

$denyFile = $storageDir . '/.htaccess';
if (!file_exists($denyFile)) {
    file_put_contents($denyFile, "Require all denied\nDeny from all\n");
}

$logFile = $storageDir . '/diagnostics-' . gmdate('Y-m') . '.jsonl';
$written = file_put_contents(
    $logFile,
    json_encode($record, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) . PHP_EOL,
    FILE_APPEND | LOCK_EX
);

if ($written === false) {
    http_response_code(500);
    echo json_encode(['ok' => false, 'error' => 'Could not save ticket']);
    exit;
}

$subject = 'ANY AI CAM Support Ticket ' . $ticketId;
$body = "A new automated support diagnostic was completed.\n\n"
      . "Ticket: {$ticketId}\n"
      . "Created: {$createdAt}\n"
      . "Customer: {$name}\n"
      . "Email: {$email}\n"
      . "Phone: {$phone}\n\n"
      . $summary . "\n";

$headers = [
    'From: ANY AI CAM Support <support@anyaicam.com>',
    'Content-Type: text/plain; charset=UTF-8'
];

if ($email !== '' && $email !== 'Not provided' && filter_var($email, FILTER_VALIDATE_EMAIL)) {
    $headers[] = 'Reply-To: ' . $email;
}

$mailSent = @mail(
    'support@anyaicam.com',
    $subject,
    $body,
    implode("\r\n", $headers)
);

echo json_encode([
    'ok' => true,
    'ticket_id' => $ticketId,
    'email_sent' => $mailSent
]);
