<?php
declare(strict_types=1);

/**
 * A minimal fake of the Stripe API for the offline storefront tests
 * (tests/run-offline-tests.sh), served with `php -S 127.0.0.1:8098`.
 * Never deployed: checkout-session.php only talks to it when
 * ANYAICAM_STOREFRONT_TEST_STRIPE_BASE is a 127.0.0.1 URL.
 *
 * - GET /v1/prices/<id>: the Price the staging PORTAL recognizes, built from
 *   deploy/staging-stripe-test-prices.json (FAKE_STRIPE_PRICES), unless the
 *   test overrides it in <state>/overrides.json (a Price object, or
 *   {"_status": 404} for a Price Stripe does not know). Unknown IDs: 404.
 * - POST /v1/customers and /v1/checkout/sessions: like Stripe, a repeated
 *   Idempotency-Key returns the first answer instead of creating another.
 * Every request is appended to <state>/log.jsonl.
 */

$state = rtrim((string)getenv('FAKE_STRIPE_STATE'), '/');
$method = $_SERVER['REQUEST_METHOD'];
$path = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
$key = $_SERVER['HTTP_IDEMPOTENCY_KEY'] ?? '';
parse_str(file_get_contents('php://input') ?: '', $fields);

$load = fn(string $name, $default) => is_file("$state/$name") ? json_decode(file_get_contents("$state/$name"), true) : $default;
$save = fn(string $name, $value) => file_put_contents("$state/$name", json_encode($value));
file_put_contents("$state/log.jsonl", json_encode(['method' => $method, 'path' => $path, 'key' => $key, 'fields' => $fields]) . "\n", FILE_APPEND);
header('Content-Type: application/json');

function reply(array $body, int $status = 200): never {
    http_response_code($status);
    echo json_encode($body);
    exit;
}

if ($method === 'GET' && preg_match('#^/v1/prices/([A-Za-z0-9_]+)$#', $path, $m)) {
    $overrides = $load('overrides.json', []);
    if (isset($overrides[$m[1]])) {
        $price = $overrides[$m[1]];
        if (($price['_status'] ?? 200) !== 200) {
            reply(['error' => ['message' => "No such price: '{$m[1]}'"]], (int)$price['_status']);
        }
        reply($price);
    }
    $portal = json_decode(file_get_contents((string)getenv('FAKE_STRIPE_PRICES')), true)['prices'];
    foreach ($portal as $entry) {
        if ($entry['price_id'] === $m[1]) {
            reply(['id' => $m[1], 'object' => 'price', 'active' => true, 'currency' => 'usd', 'livemode' => false,
                   'unit_amount' => $entry['amount_cents'], 'lookup_key' => $entry['lookup_key'],
                   'recurring' => $entry['interval'] ? ['interval' => $entry['interval'], 'interval_count' => 1] : null]);
        }
    }
    reply(['error' => ['message' => "No such price: '{$m[1]}'"]], 404);
}

if ($method === 'POST' && ($path === '/v1/customers' || $path === '/v1/checkout/sessions')) {
    $store = $path === '/v1/customers' ? 'customers.json' : 'sessions.json';
    $objects = $load($store, []);
    if ($key !== '' && isset($objects['by_key'][$key])) {
        reply($objects['items'][$objects['by_key'][$key]]);
    }
    $n = count($objects['items'] ?? []) + 1;
    $id = ($path === '/v1/customers' ? 'cus_fake' : 'cs_test_fake') . str_pad((string)$n, 6, '0', STR_PAD_LEFT);
    $object = ['id' => $id, 'fields' => $fields];
    if ($path === '/v1/checkout/sessions') {
        $object['url'] = "https://checkout.stripe.test/$id";
    }
    $objects['items'][$id] = $object;
    if ($key !== '') {
        $objects['by_key'][$key] = $id;
    }
    $save($store, $objects);
    reply($object);
}

reply(['error' => ['message' => "fake Stripe: unexpected $method $path"]], 400);
