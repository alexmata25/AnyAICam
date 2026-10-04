<?php
declare(strict_types=1);

/**
 * STAGING/REVIEW COPY — NOT the live Bluehost file.
 *
 * This is stripe-config.php (live at OneDrive/Documents/anyaicam/stripe-
 * config.php on the production docroot) plus four new one-time hardware
 * Price ID constants for the Ryzen appliance line and the Numato relay
 * module. It exists so those four products can be reviewed/wired against
 * a Stripe TEST/SANDBOX account before anything touches the live site or
 * the live Stripe account. Promote by diffing this file against the live
 * one and copying only the additions below the "NEW: staging hardware"
 * marker -- everything above it is unchanged from production.
 *
 * Same secret-loading contract as the live file: STRIPE_SECRET_KEY and
 * STRIPE_WEBHOOK_SECRET come from getenv() or stripe-secrets.local.php,
 * never hardcoded here. When actually running this staging config against
 * Stripe, the secret source must hold a TEST-mode secret key (sk_test_...)
 * -- this file has no separate test/live switch of its own, exactly like
 * the live file has none; environment is what's staged, not the code.
 */

function _anyaicam_stripe_secret(string $name): string {
    $fromEnv = getenv($name);
    if ($fromEnv !== false && trim($fromEnv) !== '') {
        return trim($fromEnv);
    }
    static $localSecrets = null;
    if ($localSecrets === null) {
        $localFile = __DIR__ . '/stripe-secrets.local.php';
        $loaded = is_file($localFile) ? (require $localFile) : [];
        $localSecrets = is_array($loaded) ? $loaded : [];
    }
    return isset($localSecrets[$name]) ? trim((string)$localSecrets[$name]) : '';
}

define('STRIPE_SECRET_KEY', _anyaicam_stripe_secret('STRIPE_SECRET_KEY'));
define('STRIPE_WEBHOOK_SECRET', _anyaicam_stripe_secret('STRIPE_WEBHOOK_SECRET'));

// Publishable key is optional for the current server-created Checkout Session flow.
define('STRIPE_PUBLISHABLE_KEY', '');

// ---------------------------------------------------------------------
// Price IDs (2026-10-04): every product this staging storefront sells uses
// EXACTLY the Stripe TEST Price the staging portal recognizes -- the same
// Stripe account as STRIPE_SECRET_KEY (acct_1U87e9GllhK80H2n, TEST mode),
// each charging the published catalog price. Source of truth:
// deploy/staging-stripe-test-prices.json (the portal's ANYAICAM_STRIPE_PRICE_*
// values); app/tests/test_storefront_portal_price_ids.py fails if any ID here
// differs from it. Before this, the storefront used separate cent-priced
// sandbox Prices the portal ignored, so a storefront purchase never became a
// portal order, plan or license. Never copy these TEST IDs into a live file.

// Adapters, cameras, PoE switches and the old webhook test price: their Price
// IDs belonged to a DIFFERENT Stripe account ("No such price" in this one) and
// no Price exists for them in this account, so they are not sold here -- an
// empty ID fails closed in every checkout path. Not replaced with invented prices.
define('WEBHOOK_TEST_PRICE_ID', '');
define('ADAPTER_8CH_PRICE_ID', '');
define('ADAPTER_16CH_PRICE_ID', '');
define('ADAPTER_32CH_PRICE_ID', '');
define('ADAPTER_64CH_PRICE_ID', '');
define('CAMERA_5MP_DOME_PRICE_ID', '');
define('CAMERA_5MP_BULLET_PRICE_ID', '');
define('CAMERA_5MP_FISHEYE_PRICE_ID', '');
define('CAMERA_DUAL_LENS_PRICE_ID', '');
define('POE_8_PORT_PRICE_ID', '');
define('POE_16_PORT_PRICE_ID', '');
define('POE_24_PORT_PRICE_ID', '');
define('POE_48_PORT_PRICE_ID', '');

// Ryzen appliances and the Numato relay -- one-time (portal: app/hardware_orders.py).
// RYZEN_ENTERPRISE_PRICE_ID is the "AnyAiCam Professional" SKU and
// RYZEN_AAC_FACIAL_PRICE_ID the "AnyAiCam Enterprise" SKU (display names only;
// SKUs unchanged), never the legacy "enterprise" software plan key.
define('RYZEN_STARTER_PRICE_ID', 'price_1ULTEfGllhK80H2nlzTBBk7G');     // $1,249.99
define('RYZEN_ENTERPRISE_PRICE_ID', 'price_1ULTEgGllhK80H2n2GBTpJ3v');  // $1,749.99 (AnyAiCam Professional)
define('RYZEN_AAC_FACIAL_PRICE_ID', 'price_1ULTEgGllhK80H2nbVKxBgNy');  // $2,249.99 (AnyAiCam Enterprise)
define('RELAY_MODULE_3CH_PRICE_ID', 'price_1ULTEhGllhK80H2ncXLynne4');  // $149.99

// Local / Hybrid camera-slot subscriptions -- monthly.
define('LOCAL_1_8_PRICE_ID', 'price_1ULTDbGllhK80H2nG5js8TyE');    // $14.99
define('LOCAL_9_16_PRICE_ID', 'price_1ULTDbGllhK80H2nsHlQqGqE');   // $24.99
define('LOCAL_17_32_PRICE_ID', 'price_1ULTDcGllhK80H2n7uwbVonf');  // $39.99
define('LOCAL_33_64_PRICE_ID', 'price_1ULTDcGllhK80H2nTd2PMMSn');  // $69.99
define('HYBRID_1_8_PRICE_ID', 'price_1ULTDdGllhK80H2ncLHUF7XQ');   // $24.99
define('HYBRID_9_16_PRICE_ID', 'price_1ULTDdGllhK80H2nUxGoG1mj');  // $39.99
define('HYBRID_17_32_PRICE_ID', 'price_1ULTDeGllhK80H2n16rH7dIK'); // $69.99
define('HYBRID_33_64_PRICE_ID', 'price_1ULTDeGllhK80H2nD8jd8rpd'); // $99.99

// Analytics packages and Talk Down -- monthly (portal: app/pricing_catalog.py ADDONS).
define('ANALYTICS_AI_ESSENTIALS_PRICE_ID', 'price_1ULTDfGllhK80H2noos78Omo');        // $7.99
define('ANALYTICS_AI_PROFESSIONAL_PRICE_ID', 'price_1ULTDfGllhK80H2nnMlSGoz7');      // $14.99
define('ANALYTICS_VEHICLE_INTELLIGENCE_PRICE_ID', 'price_1ULTDgGllhK80H2nkLTjPGlD'); // $14.99
define('ANALYTICS_ADVANCED_PRICE_ID', 'price_1ULTDgGllhK80H2nGYQNvKw5');             // $24.99
define('ANALYTICS_TALK_DOWN_PRICE_ID', 'price_1ULTDhGllhK80H2nJBZtYL9i');            // $4.99
// Not sold by this storefront (Smart Motion is included with every plan; Cloud
// Overflow has no published price; the single-price Facial Recognition, LPR,
// People Counting and PPE SKUs are retired): empty, fail closed.
define('ANALYTICS_SMART_MOTION_PRICE_ID', '');
define('ANALYTICS_PEOPLE_COUNTING_PRICE_ID', '');
define('ANALYTICS_LPR_PRICE_ID', '');
define('ANALYTICS_PPE_PRICE_ID', '');
define('ANALYTICS_CLOUD_OVERFLOW_PRICE_ID', '');
define('ANALYTICS_FACIAL_RECOGNITION_PRICE_ID', '');

// One-time VMS software license by camera capacity (for a customer-owned PC;
// included with an appliance).
define('VMS_LICENSE_8_PRICE_ID', 'price_1ULTDjGllhK80H2n4cd5fVX1');   // $49.99
define('VMS_LICENSE_16_PRICE_ID', 'price_1ULTDjGllhK80H2n7QtlNYHf');  // $79.99
define('VMS_LICENSE_32_PRICE_ID', 'price_1ULTDkGllhK80H2nsuVnC8cw');  // $129.99
define('VMS_LICENSE_64_PRICE_ID', 'price_1ULTDkGllhK80H2nlPrgGVOL');  // $199.99

// Env-overridable, same convention as STRIPE_SECRET_KEY/STRIPE_WEBHOOK_SECRET
// above: the deployed storefront container sets ANYAICAM_STOREFRONT_BASE_URL
// to https://store-staging.anyaicam.com so checkout-session.php's success_url/
// cancel_url point at the real staging storefront host instead of the live
// domain. Falls back to the live value so this file's behavior is unchanged
// for anyone running it without that env var set.
define('BASE_URL', getenv('ANYAICAM_STOREFRONT_BASE_URL') ?: 'https://anyaicam.com');
