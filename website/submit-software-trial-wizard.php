<?php
declare(strict_types=1);

ini_set('display_errors', '0');
header('X-Content-Type-Options: nosniff');

function redirect_with_error(string $message): never {
    $target = 'software-trial-wizard.html?error=' . rawurlencode($message);
    header('Location: ' . $target, true, 303);
    exit;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    exit('Method not allowed');
}

if (trim((string)($_POST['website'] ?? '')) !== '') {
    header('Location: software-trial-wizard-thank-you.html', true, 303);
    exit;
}

function clean_field(string $key, int $max = 500): string {
    $value = trim((string)($_POST[$key] ?? ''));
    $value = str_replace(["\r", "\0"], '', $value);
    return mb_substr($value, 0, $max);
}

$name = clean_field('full_name', 120);
$company = clean_field('company', 160);
$email = filter_var(clean_field('email', 190), FILTER_VALIDATE_EMAIL);
$phone = clean_field('phone', 50);
$street = clean_field('street_address', 190);
$city = clean_field('city', 100);
$state = clean_field('state', 80);
$zip = clean_field('zip', 20);
$notes = clean_field('notes', 1500);

if ($name === '' || $email === false || $phone === '' || $street === '' || $city === '' || $state === '' || $zip === '') {
    redirect_with_error('Please complete all required fields with a valid email address.');
}

$mailConfig = is_file(__DIR__ . '/mail_config.php')
    ? (array) require __DIR__ . '/mail_config.php'
    : [];
$adminEmail = (string)($mailConfig['to_email'] ?? 'amata@anyaicam.com');
$subjectAdmin = 'New ANY AI CAM software trial request - ' . $name;
$bodyAdmin = "New software-only trial request\n\n"
    . "Name: {$name}\n"
    . "Company/site: " . ($company !== '' ? $company : 'Not provided') . "\n"
    . "Email: {$email}\n"
    . "Phone: {$phone}\n"
    . "Address: {$street}, {$city}, {$state} {$zip}\n"
    . "Notes: " . ($notes !== '' ? $notes : 'None') . "\n\n"
    . "Next step: Create the Videoloft trial account, then email the customer the official download link and a unique temporary password.";

$subjectCustomer = 'ANY AI CAM received your software trial request';
$bodyCustomer = "Hello {$name},\n\n"
    . "We received your request for a free 30-day ANY AI CAM software trial.\n\n"
    . "We will create your trial account and email your official software download link, unique temporary password, and setup instructions shortly.\n\n"
    . "The software will generate its own Cloud ID during installation. Please do not email camera passwords. If you need help locating your camera login later, reply to this email and we will explain your options.\n\n"
    . "ANY AI CAM\n"
    . "amata@anyaicam.com\n"
    . "(832) 510-8240";

$fromEmail = (string)($mailConfig['from_email'] ?? 'amata@anyaicam.com');
$fromName = (string)($mailConfig['from_name'] ?? 'ANY AI CAM');
$headers = [
    "From: {$fromName} <{$fromEmail}>",
    "Reply-To: {$adminEmail}",
    'MIME-Version: 1.0',
    'Content-Type: text/plain; charset=UTF-8',
];

$adminSent = false;
$customerSent = false;
$phpMailerFiles = [
    __DIR__ . '/PHPMailer/PHPMailer.php',
    __DIR__ . '/PHPMailer/Exception.php',
    __DIR__ . '/PHPMailer/SMTP.php',
];

if (
    !empty($mailConfig['smtp_enabled'])
    && !empty($mailConfig['smtp_host'])
    && count(array_filter($phpMailerFiles, 'is_file')) === 3
) {
    try {
        foreach ($phpMailerFiles as $file) {
            require_once $file;
        }
        $mailer = new PHPMailer\PHPMailer\PHPMailer(true);
        $mailer->isSMTP();
        $mailer->Host = (string)$mailConfig['smtp_host'];
        $mailer->SMTPAuth = true;
        $mailer->Username = (string)($mailConfig['smtp_username'] ?? '');
        $mailer->Password = (string)($mailConfig['smtp_password'] ?? '');
        $mailer->SMTPSecure = (string)($mailConfig['smtp_secure'] ?? 'tls');
        $mailer->Port = (int)($mailConfig['smtp_port'] ?? 587);
        $mailer->CharSet = 'UTF-8';
        $mailer->setFrom($fromEmail, $fromName);
        $mailer->isHTML(false);

        $mailer->addAddress($adminEmail);
        $mailer->addReplyTo((string)$email, $name);
        $mailer->Subject = $subjectAdmin;
        $mailer->Body = $bodyAdmin;
        $mailer->send();
        $adminSent = true;

        $mailer->clearAddresses();
        $mailer->clearReplyTos();
        $mailer->addAddress((string)$email, $name);
        $mailer->addReplyTo($adminEmail, $fromName);
        $mailer->Subject = $subjectCustomer;
        $mailer->Body = $bodyCustomer;
        $mailer->send();
        $customerSent = true;
    } catch (Throwable $e) {
        error_log('Software trial SMTP error: ' . $e->getMessage());
    }
}

if (!$adminSent) {
    $adminSent = @mail($adminEmail, $subjectAdmin, $bodyAdmin, implode("\r\n", $headers));
}
if (!$customerSent) {
    $customerSent = @mail((string)$email, $subjectCustomer, $bodyCustomer, implode("\r\n", $headers));
}

$logLine = sprintf(
    "[%s] software_trial name=%s email=%s admin=%s customer=%s\n",
    date('c'),
    str_replace(["\n", "\r"], ' ', $name),
    (string)$email,
    $adminSent ? 'sent' : 'failed',
    $customerSent ? 'sent' : 'failed'
);
@file_put_contents(__DIR__ . '/software-trial-email-log.txt', $logLine, FILE_APPEND | LOCK_EX);

header('Location: software-trial-wizard-thank-you.html', true, 303);
exit;
