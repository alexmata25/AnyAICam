<?php
declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

function respond(array $payload, int $status = 200): never {
    http_response_code($status);
    echo json_encode($payload, JSON_UNESCAPED_SLASHES);
    exit;
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

function money_from_cents(int $cents, string $currency): string {
    return strtoupper($currency ?: 'usd') . ' ' . number_format($cents / 100, 2);
}

$orderCode = trim((string)($_GET['order'] ?? $_GET['order_id'] ?? ''));
if ($orderCode === '' || !preg_match('/^AIC-O-[A-Z0-9-]+$/', $orderCode)) {
    respond(['ok' => false, 'error' => 'Invalid order.'], 400);
}

try {
    $pdo = db();
    $stmt = $pdo->prepare(
        'SELECT o.order_code,o.status,o.customer_email,o.customer_name,o.order_json,
                q.quote_code,p.partner_code
         FROM orders o
         LEFT JOIN quotes q ON q.id=o.quote_id
         LEFT JOIN partners p ON p.id=o.partner_id
         WHERE o.order_code = ?
         LIMIT 1'
    );
    $stmt->execute([$orderCode]);
    $order = $stmt->fetch();
    if (!$order) {
        respond(['ok' => false, 'error' => 'Order not found.'], 404);
    }

    $orderJson = json_decode((string)$order['order_json'], true);
    if (!is_array($orderJson)) $orderJson = [];
    $stripe = isset($orderJson['stripe']) && is_array($orderJson['stripe']) ? $orderJson['stripe'] : [];
    $customer = isset($orderJson['customer']) && is_array($orderJson['customer']) ? $orderJson['customer'] : [];
    $adapter = (string)($orderJson['adapter'] ?? $orderJson['adapter_type'] ?? '');
    $adapterQty = (int)($orderJson['adapterQty'] ?? $orderJson['adapter_quantity'] ?? 1);
    $amountCents = (int)($stripe['amount_total'] ?? $orderJson['payment']['amount_paid_today'] ?? 0);
    $currency = (string)($stripe['currency'] ?? $orderJson['payment']['currency'] ?? 'usd');
    $paid = in_array((string)$order['status'], ['paid', 'completed'], true);

    respond([
        'ok' => true,
        'paid' => $paid,
        'status' => (string)$order['status'],
        'order' => [
            'order_id' => (string)$order['order_code'],
            'quote_id' => (string)($order['quote_code'] ?? ''),
            'partner_id' => (string)($order['partner_code'] ?? ''),
            'customer_name' => (string)($customer['name'] ?? $order['customer_name'] ?? 'Customer'),
            'amount_paid_today' => $amountCents > 0 ? money_from_cents($amountCents, $currency) : 'Pending confirmation',
            'paid_at' => (string)($stripe['paid_at'] ?? ''),
            'adapter' => $adapter,
            'adapter_quantity' => $adapterQty,
        ],
    ]);
} catch (Throwable $e) {
    error_log('Payment status lookup failed: ' . $e->getMessage());
    respond(['ok' => false, 'error' => 'Payment status is unavailable.'], 500);
}
