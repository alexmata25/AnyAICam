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
 * PRICES (2026-10-04): every amount_cents below is the published catalog
 * price (app/pricing_catalog.py, approved 2026-09-30) -- the same amount the
 * portal-recognized Stripe TEST Price in stripe-config.php charges. Before
 * this they were tiny sandbox amounts on Prices the portal ignored.
 * checkout-session.php verifies each Stripe Price against amount_cents
 * before creating a Checkout Session (stripe_price_problem() below) and
 * refuses on any mismatch. Structure follows that catalog: Local/Hybrid plans, the four analytics
 * packages (flat, never per camera), Talk Down (per site, includes AAC
 * Voice Call), and the one-time VMS software license -- charged for a DIY
 * install, included (not charged) when an AnyAiCam appliance is in the
 * cart. Secure Edge, Smart Motion and AACO are included in every plan.
 * Friends & Family is never a code: it is requested from the
 * customer's signed-in AnyAiCam account and approved by an administrator.
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
    'local_1_8'    => ['type' => 'local',  'capacity' => 8,  'label' => 'Local 8 cameras',   'amount_cents' => 1499, 'display_price' => 14.99, 'price_id_const' => 'LOCAL_1_8_PRICE_ID'],
    'local_9_16'   => ['type' => 'local',  'capacity' => 16, 'label' => 'Local 16 cameras',  'amount_cents' => 2499, 'display_price' => 24.99, 'price_id_const' => 'LOCAL_9_16_PRICE_ID'],
    'local_17_32'  => ['type' => 'local',  'capacity' => 32, 'label' => 'Local 32 cameras',  'amount_cents' => 3999, 'display_price' => 39.99, 'price_id_const' => 'LOCAL_17_32_PRICE_ID'],
    'local_33_64'  => ['type' => 'local',  'capacity' => 64, 'label' => 'Local 64 cameras',  'amount_cents' => 6999, 'display_price' => 69.99, 'price_id_const' => 'LOCAL_33_64_PRICE_ID'],
    'hybrid_1_8'   => ['type' => 'hybrid', 'capacity' => 8,  'label' => 'Hybrid 8 cameras',  'amount_cents' => 2499, 'display_price' => 24.99, 'price_id_const' => 'HYBRID_1_8_PRICE_ID'],
    'hybrid_9_16'  => ['type' => 'hybrid', 'capacity' => 16, 'label' => 'Hybrid 16 cameras', 'amount_cents' => 3999, 'display_price' => 39.99, 'price_id_const' => 'HYBRID_9_16_PRICE_ID'],
    'hybrid_17_32' => ['type' => 'hybrid', 'capacity' => 32, 'label' => 'Hybrid 32 cameras', 'amount_cents' => 6999, 'display_price' => 69.99, 'price_id_const' => 'HYBRID_17_32_PRICE_ID'],
    'hybrid_33_64' => ['type' => 'hybrid', 'capacity' => 64, 'label' => 'Hybrid 64 cameras', 'amount_cents' => 9999, 'display_price' => 99.99, 'price_id_const' => 'HYBRID_33_64_PRICE_ID'],
];

// Analytics packages (flat, never per camera) and Talk Down (per site,
// includes AAC Voice Call). Package contents live in app/pricing_catalog.py.
const ANALYTICS_CATALOG = [
    'ai_essentials'        => ['label' => 'AI Essentials',                        'amount_cents' => 799, 'display_price' => 7.99, 'price_id_const' => 'ANALYTICS_AI_ESSENTIALS_PRICE_ID'],
    'ai_professional'      => ['label' => 'AI Professional',                      'amount_cents' => 1499, 'display_price' => 14.99, 'price_id_const' => 'ANALYTICS_AI_PROFESSIONAL_PRICE_ID'],
    'vehicle_intelligence' => ['label' => 'Vehicle Intelligence',                 'amount_cents' => 1499, 'display_price' => 14.99, 'price_id_const' => 'ANALYTICS_VEHICLE_INTELLIGENCE_PRICE_ID'],
    'advanced_analytics'   => ['label' => 'Advanced Analytics',                   'amount_cents' => 2499, 'display_price' => 24.99, 'price_id_const' => 'ANALYTICS_ADVANCED_PRICE_ID'],
    'talk_down'            => ['label' => 'Talk Down (includes AAC Voice Call)',  'amount_cents' => 499, 'display_price' => 4.99, 'price_id_const' => 'ANALYTICS_TALK_DOWN_PRICE_ID'],
];

// One-time VMS software license, keyed by camera capacity (app/pricing_catalog.py).
const VMS_LICENSE_CATALOG = [
    8  => ['label' => 'VMS software license, 8 cameras',  'amount_cents' => 4999, 'display_price' => 49.99, 'price_id_const' => 'VMS_LICENSE_8_PRICE_ID'],
    16 => ['label' => 'VMS software license, 16 cameras', 'amount_cents' => 7999, 'display_price' => 79.99, 'price_id_const' => 'VMS_LICENSE_16_PRICE_ID'],
    32 => ['label' => 'VMS software license, 32 cameras', 'amount_cents' => 12999, 'display_price' => 129.99, 'price_id_const' => 'VMS_LICENSE_32_PRICE_ID'],
    64 => ['label' => 'VMS software license, 64 cameras', 'amount_cents' => 19999, 'display_price' => 199.99, 'price_id_const' => 'VMS_LICENSE_64_PRICE_ID'],
];

/** Fail-closed: an unknown or unconfigured license capacity resolves to null. */
function resolve_vms_license(int $capacity): ?array {
    if (!isset(VMS_LICENSE_CATALOG[$capacity])) {
        return null;
    }
    $entry = VMS_LICENSE_CATALOG[$capacity];
    $priceId = config_value_checked($entry['price_id_const']);
    if ($priceId === '') {
        return null;
    }
    return ['key' => (string)$capacity, 'capacity' => $capacity] + $entry + ['price_id' => $priceId];
}

const INCLUDED_FEATURES = ['Secure Edge', 'Smart Motion', 'AACO'];

// The only hardware this storefront sells (2026-10-04): the three Ryzen
// appliances and the Numato relay. Cameras, PoE switches and adapters stay in
// the hardware catalog for display but have no Price in this Stripe account,
// so a checkout leg naming them is refused.
const SELLABLE_HARDWARE_SKUS = ['AIC-APPLIANCE-RYZEN-STARTER', 'AIC-APPLIANCE-RYZEN-ENTERPRISE',
                                'AIC-APPLIANCE-RYZEN-AAC-FACIAL', 'AIC-RELAY-NUMATO-3CH'];

/**
 * Fail-closed check of a Stripe Price object (GET /v1/prices/<id>) against
 * the catalog: null when it charges exactly $expectedCents in USD, is active,
 * and is one-time for mode "payment" / monthly for mode "subscription";
 * otherwise a short reason. Never trusts a client-side amount.
 */
function stripe_price_problem(array $price, string $expectedPriceId, int $expectedCents, string $mode): ?string {
    if ((string)($price['id'] ?? '') !== $expectedPriceId) {
        return 'Stripe returned a different Price';
    }
    if (($price['active'] ?? false) !== true) {
        return 'the Stripe Price is not active';
    }
    if (strtolower((string)($price['currency'] ?? '')) !== 'usd') {
        return 'the Stripe Price is not in USD';
    }
    if (!is_int($price['unit_amount'] ?? null) || $price['unit_amount'] !== $expectedCents) {
        return 'the Stripe Price does not charge the published amount';
    }
    $recurring = $price['recurring'] ?? null;
    if ($mode === 'payment') {
        return $recurring === null ? null : 'the Stripe Price is recurring, not one-time';
    }
    if (!is_array($recurring) || ($recurring['interval'] ?? '') !== 'month' || (int)($recurring['interval_count'] ?? 1) !== 1) {
        return 'the Stripe Price is not monthly';
    }
    return null;
}

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

    // One-time VMS software license: included with an AnyAiCam appliance
    // (never charged twice); charged once for a DIY / customer-owned PC.
    $vmsLicense = null;
    $vmsLicenseIncluded = $cameraPlan !== null && $applianceSku !== '';
    if ($cameraPlan !== null && $applianceSku === '') {
        $vmsLicense = resolve_vms_license((int)$cameraPlan['capacity']);
        if ($vmsLicense === null) {
            throw new InvalidArgumentException('The VMS software license for this camera capacity is not available yet.');
        }
    }

    return [
        'customer_email' => $email,
        'appliance_sku' => $applianceSku,
        'relay' => $relay,
        'camera_plan' => $cameraPlan,
        'analytics' => $analytics,
        'vms_license' => $vmsLicense,
        'vms_license_included' => $vmsLicenseIncluded,
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

    if (($cart['vms_license'] ?? null) !== null) {
        $license = $cart['vms_license'];
        $legs[] = [
            'kind' => 'vms_license',
            'key' => $license['key'],
            'label' => $license['label'],
            'price_id' => $license['price_id'],
            'mode' => 'payment',
            'amount' => $license['display_price'],
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
