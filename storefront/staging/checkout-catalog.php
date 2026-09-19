<?php
declare(strict_types=1);

/**
 * STAGING/REVIEW COPY -- NOT the live Bluehost file. New file: no live
 * equivalent exists yet. Local/Hybrid camera-slot subscriptions and all
 * 10 analytics add-ons have never had a checkout path anywhere in this
 * codebase (website or backend) -- this is that missing piece, built for
 * Stripe TEST/staging use only.
 *
 * Pure, dependency-free catalog + cart-shape validation + order-summary
 * math. No Stripe API calls, no I/O, no session/cookie access -- every
 * function here takes plain arrays in and returns plain arrays out, so
 * it can be exercised directly by tests/test-checkout-catalog.php (a
 * plain-PHP assertion script; no test framework is installed in this
 * repo) without a running web server.
 *
 * checkout-session.php (the only file that talks to Stripe) requires
 * this file for price-ID resolution and leg-building; it never
 * duplicates this logic.
 *
 * One-Stripe-Price-ID-per-Checkout-Session, by design
 * -----------------------------------------------------
 * The backend's subscription-lifecycle code (customer_entitlements.py's
 * and analytics_entitlements.py's _current_subscription_price_id())
 * reads only items.data[0].price.id on a Stripe subscription -- a
 * multi-item subscription would silently have every item after the
 * first ignored by entitlement/analytics sync. The one-time hardware
 * checkout has the same constraint one layer up: hardware_orders.py's
 * webhook sync reads a single anyaicam_stripe_price_id/anyaicam_
 * hardware_sku pair from Checkout Session metadata, not a per-line-item
 * value, so two hardware SKUs in one Checkout Session would only ever
 * record one order (confirmed by /api/payments/hardware-checkout's own
 * HardwareCheckoutCreateModel, which already only ever accepts a single
 * sku per call). So every distinct product the customer selects --
 * appliance, relay, camera plan, each analytic -- becomes its own
 * "leg": one Stripe Price ID, one Checkout Session, one Stripe object.
 * This is architecture A from the task that commissioned this file
 * (preserve the current separate-subscription model) rather than
 * architecture B (upgrading three backend modules to read multi-item
 * subscriptions end to end) -- B is materially larger and riskier for a
 * staging build with no product requirement driving it yet.
 *
 * SANDBOX TEST PRICES: every display_price below is the tiny Stripe
 * TEST-mode amount that stripe-config.php's Price IDs actually charge,
 * not real retail. Never promote these numbers (or this catalog) to
 * production as-is.
 */

require_once __DIR__ . '/stripe-config.php';

function config_value_checked(string $name): string {
    return defined($name) ? trim((string)constant($name)) : '';
}

// appliance_sku is looked up in stripe-checkout.php's existing
// $hardwareCatalog (never duplicated here) -- checkout-session.php
// requires stripe-checkout.php's catalog builder for that reason. This
// module only defines the two categories that don't already have a
// catalog anywhere: camera plans and analytics.

const CAMERA_PLAN_CATALOG = [
    'local_1_8'    => ['type' => 'local',  'label' => 'Local 1–8 cameras',      'display_price' => 0.60, 'price_id_const' => 'LOCAL_1_8_PRICE_ID'],
    'local_9_16'   => ['type' => 'local',  'label' => 'Local 9–16 cameras',     'display_price' => 0.61, 'price_id_const' => 'LOCAL_9_16_PRICE_ID'],
    'local_17_32'  => ['type' => 'local',  'label' => 'Local 17–32 cameras',    'display_price' => 0.62, 'price_id_const' => 'LOCAL_17_32_PRICE_ID'],
    'local_33_64'  => ['type' => 'local',  'label' => 'Local 33–64 cameras',    'display_price' => 0.63, 'price_id_const' => 'LOCAL_33_64_PRICE_ID'],
    'hybrid_1_8'   => ['type' => 'hybrid', 'label' => 'Hybrid 1–8 cameras',     'display_price' => 0.70, 'price_id_const' => 'HYBRID_1_8_PRICE_ID'],
    'hybrid_9_16'  => ['type' => 'hybrid', 'label' => 'Hybrid 9–16 cameras',    'display_price' => 0.71, 'price_id_const' => 'HYBRID_9_16_PRICE_ID'],
    'hybrid_17_32' => ['type' => 'hybrid', 'label' => 'Hybrid 17–32 cameras',   'display_price' => 0.72, 'price_id_const' => 'HYBRID_17_32_PRICE_ID'],
    'hybrid_33_64' => ['type' => 'hybrid', 'label' => 'Hybrid 33–64 cameras',   'display_price' => 0.73, 'price_id_const' => 'HYBRID_33_64_PRICE_ID'],
];

const ANALYTICS_CATALOG = [
    'smart_motion'         => ['label' => 'Smart Motion',                 'display_price' => 0.80, 'price_id_const' => 'ANALYTICS_SMART_MOTION_PRICE_ID'],
    'people_counting'      => ['label' => 'People Counting',              'display_price' => 0.81, 'price_id_const' => 'ANALYTICS_PEOPLE_COUNTING_PRICE_ID'],
    'lpr'                  => ['label' => 'License Plate Recognition',    'display_price' => 0.82, 'price_id_const' => 'ANALYTICS_LPR_PRICE_ID'],
    'ppe'                  => ['label' => 'PPE Detection',                'display_price' => 0.83, 'price_id_const' => 'ANALYTICS_PPE_PRICE_ID'],
    'talk_down'            => ['label' => 'Talk Down',                    'display_price' => 0.84, 'price_id_const' => 'ANALYTICS_TALK_DOWN_PRICE_ID'],
    'ai_essentials'        => ['label' => 'AnyAiCam AI Essentials',       'display_price' => 0.85, 'price_id_const' => 'ANALYTICS_AI_ESSENTIALS_PRICE_ID'],
    'ai_professional'      => ['label' => 'AnyAiCam AI Professional',     'display_price' => 0.86, 'price_id_const' => 'ANALYTICS_AI_PROFESSIONAL_PRICE_ID'],
    'vehicle_intelligence' => ['label' => 'Vehicle Intelligence',         'display_price' => 0.87, 'price_id_const' => 'ANALYTICS_VEHICLE_INTELLIGENCE_PRICE_ID'],
    'cloud_overflow'       => ['label' => 'Cloud Overflow',               'display_price' => 0.88, 'price_id_const' => 'ANALYTICS_CLOUD_OVERFLOW_PRICE_ID'],
    'facial_recognition'   => ['label' => 'Facial Recognition',           'display_price' => 0.89, 'price_id_const' => 'ANALYTICS_FACIAL_RECOGNITION_PRICE_ID'],
];

/** Fail-closed: an unknown camera-plan key resolves to null, never a guess. */
function resolve_camera_plan(string $key): ?array {
    if (!isset(CAMERA_PLAN_CATALOG[$key])) {
        return null;
    }
    $entry = CAMERA_PLAN_CATALOG[$key];
    $priceId = config_value_checked($entry['price_id_const']);
    if ($priceId === '') {
        return null;
    }
    return ['key' => $key] + $entry + ['price_id' => $priceId];
}

/** Fail-closed: an unknown analytic key resolves to null, never a guess. */
function resolve_analytic(string $key): ?array {
    if (!isset(ANALYTICS_CATALOG[$key])) {
        return null;
    }
    $entry = ANALYTICS_CATALOG[$key];
    $priceId = config_value_checked($entry['price_id_const']);
    if ($priceId === '') {
        return null;
    }
    return ['key' => $key] + $entry + ['price_id' => $priceId];
}

/**
 * Validates the raw cart payload the storefront JS submits, given the
 * hardware catalog stripe-checkout.php already exports ($hardwareCatalog,
 * SKU => ['name','unit_amount','price_id']). Returns a normalized cart
 * array on success. Throws InvalidArgumentException with a customer-
 * safe message on any invalid selection -- never silently drops or
 * guesses at an ambiguous cart.
 */
function normalize_cart(array $raw, array $hardwareCatalog): array {
    $applianceSku = trim((string)($raw['appliance_sku'] ?? ''));
    if ($applianceSku !== '' && !isset($hardwareCatalog[$applianceSku])) {
        throw new InvalidArgumentException('Unknown appliance selection.');
    }
    // Only the three appliance SKUs may be selected as "the appliance" --
    // the relay is a separate accessory line, never conflated with it,
    // even though both live in the same underlying hardware catalog.
    if ($applianceSku !== '' && !str_starts_with($applianceSku, 'AIC-APPLIANCE-')) {
        throw new InvalidArgumentException('Selected SKU is not an appliance.');
    }

    $relay = (bool)($raw['relay'] ?? false);

    $cameraPlanKey = trim((string)($raw['camera_plan'] ?? ''));
    $cameraPlan = null;
    if ($cameraPlanKey !== '') {
        $cameraPlan = resolve_camera_plan($cameraPlanKey);
        if ($cameraPlan === null) {
            throw new InvalidArgumentException('Unknown or unconfigured camera-slot plan.');
        }
    }

    $analyticsKeys = is_array($raw['analytics'] ?? null) ? $raw['analytics'] : [];
    $analytics = [];
    $seen = [];
    foreach ($analyticsKeys as $rawKey) {
        $key = trim((string)$rawKey);
        if ($key === '' || isset($seen[$key])) {
            continue;
        }
        $resolved = resolve_analytic($key);
        if ($resolved === null) {
            throw new InvalidArgumentException("Unknown or unconfigured analytics selection: {$key}");
        }
        $seen[$key] = true;
        $analytics[] = $resolved;
    }

    $email = trim((string)($raw['customer_email'] ?? ''));
    if ($email !== '' && !filter_var($email, FILTER_VALIDATE_EMAIL)) {
        throw new InvalidArgumentException('Invalid customer email.');
    }

    if ($applianceSku === '' && !$relay && $cameraPlan === null && count($analytics) === 0) {
        throw new InvalidArgumentException('Select at least one item before checking out.');
    }

    return [
        'customer_email' => $email,
        'appliance_sku' => $applianceSku,
        'relay' => $relay,
        'camera_plan' => $cameraPlan,
        'analytics' => $analytics,
    ];
}

/**
 * Builds the ordered list of checkout "legs" for a normalized cart.
 * Each leg is exactly one Stripe Price ID / one Checkout Session --
 * see this file's own header for why. Order is deterministic (appliance,
 * relay, camera plan, then analytics in catalog order) so a customer
 * re-running the same cart always sees the same journey.
 */
function build_checkout_legs(array $cart, array $hardwareCatalog): array {
    $legs = [];

    if ($cart['appliance_sku'] !== '') {
        $item = $hardwareCatalog[$cart['appliance_sku']];
        $legs[] = [
            'kind' => 'hardware',
            'key' => $cart['appliance_sku'],
            'label' => $item['name'],
            'price_id' => $item['price_id'],
            'mode' => 'payment',
            'amount' => $item['unit_amount'] / 100,
        ];
    }

    if ($cart['relay']) {
        $item = $hardwareCatalog['AIC-RELAY-NUMATO-3CH'];
        $legs[] = [
            'kind' => 'hardware',
            'key' => 'AIC-RELAY-NUMATO-3CH',
            'label' => $item['name'],
            'price_id' => $item['price_id'],
            'mode' => 'payment',
            'amount' => $item['unit_amount'] / 100,
        ];
    }

    if ($cart['camera_plan'] !== null) {
        $plan = $cart['camera_plan'];
        $legs[] = [
            'kind' => 'camera_plan',
            'key' => $plan['key'],
            'label' => $plan['label'],
            'price_id' => $plan['price_id'],
            'mode' => 'subscription',
            'amount' => $plan['display_price'],
        ];
    }

    foreach ($cart['analytics'] as $analytic) {
        $legs[] = [
            'kind' => 'analytics',
            'key' => $analytic['key'],
            'label' => $analytic['label'],
            'price_id' => $analytic['price_id'],
            'mode' => 'subscription',
            'amount' => $analytic['display_price'],
        ];
    }

    return $legs;
}

/**
 * Order summary, one-time and monthly kept strictly separate -- never
 * combined into one misleading total (task requirement).
 */
function order_summary(array $legs): array {
    $oneTime = ['items' => [], 'subtotal' => 0.0];
    $monthly = ['items' => [], 'subtotal' => 0.0];
    foreach ($legs as $leg) {
        $bucket = $leg['mode'] === 'payment' ? 'one_time' : 'monthly';
        if ($bucket === 'one_time') {
            $oneTime['items'][] = ['label' => $leg['label'], 'amount' => $leg['amount']];
            $oneTime['subtotal'] = round($oneTime['subtotal'] + $leg['amount'], 2);
        } else {
            $monthly['items'][] = ['label' => $leg['label'], 'amount' => $leg['amount']];
            $monthly['subtotal'] = round($monthly['subtotal'] + $leg['amount'], 2);
        }
    }
    return ['one_time' => $oneTime, 'monthly' => $monthly];
}
