<?php
declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
date_default_timezone_set('America/Chicago');

function respond(array $data, int $status = 200): never {
    http_response_code($status);
    echo json_encode($data, JSON_UNESCAPED_SLASHES);
    exit;
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

$dbFile = __DIR__ . '/config.php';
$mailFile = __DIR__ . '/mail_config.php';

if (!is_file($dbFile) || !is_file($mailFile)) {
    respond(['ok'=>false,'error'=>'Server mail or database configuration is missing.'],500);
}

$db = require $dbFile;
$mailConfig = require $mailFile;
if (!is_array($db) || !is_array($mailConfig)) {
    respond(['ok'=>false,'error'=>'Server configuration is invalid.'],500);
}

session_name($db['session_name'] ?? 'ANYAICAM_PARTNER');
session_set_cookie_params([
    'lifetime'=>0,
    'path'=>'/',
    'secure'=>(!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off'),
    'httponly'=>true,
    'samesite'=>'Lax',
]);
session_start();

$partner = $_SESSION['partner'] ?? null;
if (!is_array($partner) || empty($partner['db_id'])) {
    respond(['ok'=>false,'error'=>'Please log in through the partner portal.'],401);
}
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    respond(['ok'=>false,'error'=>'POST required.'],405);
}

$data = json_decode(file_get_contents('php://input') ?: '{}',true);
if (!is_array($data)) $data=[];

$orderId = trim((string)($data['orderId'] ?? ''));
$tempPassword = (string)($data['temporaryPassword'] ?? '');

if ($orderId === '' || strlen($tempPassword) < 8 || strlen($tempPassword) > 128) {
    respond(['ok'=>false,'error'=>'Enter the order and a valid temporary password.'],422);
}

try {
    $pdo = new PDO(
        'mysql:host=' . $db['db_host'] . ';dbname=' . $db['db_name'] . ';charset=utf8mb4',
        $db['db_user'],
        $db['db_pass'],
        [
            PDO::ATTR_ERRMODE=>PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE=>PDO::FETCH_ASSOC,
            PDO::ATTR_EMULATE_PREPARES=>false,
        ]
    );

    $stmt=$pdo->prepare('SELECT id,customer_name,customer_email,order_json FROM orders WHERE order_code=? AND partner_id=? LIMIT 1');
    $stmt->execute([$orderId,(int)$partner['db_id']]);
    $order=$stmt->fetch();

    if (!$order || !filter_var($order['customer_email'],FILTER_VALIDATE_EMAIL)) {
        respond(['ok'=>false,'error'=>'The customer order or email address could not be found.'],404);
    }

    $customerName = trim((string)$order['customer_name']) ?: 'Customer';
    $subject = 'Your ANY AI CAM Videoloft login is ready';

    $body = "Hello " . $customerName . ",\n\n"
        . "Your ANY AI CAM cloud video account has been created.\n\n"
        . "VIDEOFLOFT APP LOGIN\n"
        . "Email: " . $order['customer_email'] . "\n"
                . "Temporary password: " . $tempPassword . "\n\n"
        . "NEXT STEPS\n"
        . "1. Download the Videoloft CCTV Connect app.\n"
        . "2. Sign in using the email and temporary password above.\n"
        . "3. Change your password after your first successful login.\n"
        . "4. Wait for your Videoloft cloud adapter to arrive.\n"
        . "5. The Cloud ID is printed on the adapter.\n"
        . "6. Connect the adapter to power and your network.\n"
        . "7. Open the app, enter the Cloud ID printed on the adapter, and scan for your compatible cameras.\n\n"
        . "Do not email camera passwords or payment card information.\n\n"
        . "Need help? Reply to this email or contact support@anyaicam.com.\n\n"
        . "Thank you,\n"
        . "ANY AI CAM Support\n"
        . "support@anyaicam.com\n";

    [$sent,$message] = send_raw_smtp($mailConfig,(string)$order['customer_email'],$subject,$body);
    @file_put_contents(
        __DIR__ . '/email-log.txt',
        date('Y-m-d H:i:s') . ' - welcome-email - ' . ($sent?'SUCCESS':'FAILED') . ' - ' . $orderId . ' - ' . $message . "\n",
        FILE_APPEND | LOCK_EX
    );

    if (!$sent) {
        respond(['ok'=>false,'error'=>'The email could not be sent. Check email-log.txt.'],500);
    }

    $json=json_decode((string)$order['order_json'],true);
    if (!is_array($json)) $json=[];
    $activation=is_array($json['activation']??null)?$json['activation']:[];
    $history=is_array($activation['history']??null)?$activation['history']:[];

    $history[]=[
        'status'=>'welcome_email_sent',
        'note'=>'Videoloft welcome email sent to customer.',
        'updated_at'=>gmdate('c'),
        'updated_by'=>['partner_id'=>$partner['id']??'','partner_name'=>$partner['name']??'']
    ];
    $activation['current_status']='welcome_email_sent';
    $activation['last_note']='Videoloft welcome email sent to customer.';
    $activation['updated_at']=gmdate('c');
    $activation['history']=$history;
    $json['activation']=$activation;

    // Do not store the temporary password.
    $stmt=$pdo->prepare('UPDATE orders SET order_json=? WHERE id=?');
    $stmt->execute([json_encode($json,JSON_UNESCAPED_SLASHES),$order['id']]);

    respond(['ok'=>true,'orderId'=>$orderId,'status'=>'welcome_email_sent']);
} catch (Throwable $e) {
    $errorId=strtoupper(substr(bin2hex(random_bytes(6)),0,10));
    error_log('ANYAICAM welcome email ' . $errorId . ': ' . $e->getMessage());
    respond(['ok'=>false,'error'=>'The welcome email could not be completed. Error reference: ' . $errorId],500);
}
