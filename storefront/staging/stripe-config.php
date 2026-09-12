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

// Existing adapter Stripe Prices -- unchanged from the live file.
define('WEBHOOK_TEST_PRICE_ID', 'price_1Tu07GLeOpYDAzwemdlVNWUa');
define('ADAPTER_8CH_PRICE_ID', 'price_1Tov5kLeOpYDAzwe7fCiPrky');
define('ADAPTER_16CH_PRICE_ID', 'price_1Tov7DLeOpYDAzweEXqbq0rY');
define('ADAPTER_32CH_PRICE_ID', 'price_1TovAkLeOpYDAzwegtDbHRvM');
define('ADAPTER_64CH_PRICE_ID', 'price_1TovC9LeOpYDAzwepOg2TTSk');

// Camera Stripe Prices -- unchanged from the live file.
define('CAMERA_5MP_DOME_PRICE_ID', 'price_1TwwzkLeOpYDAzweaUOb0Hu8');
define('CAMERA_5MP_BULLET_PRICE_ID', 'price_1Twx2dLeOpYDAzweRLXdygCP');
define('CAMERA_5MP_FISHEYE_PRICE_ID', 'price_1Twx55LeOpYDAzweDoUvcVTN');
define('CAMERA_DUAL_LENS_PRICE_ID', 'price_1TwxA0LeOpYDAzwegMdXH5zr');

// PoE switch Stripe Prices -- unchanged from the live file.
define('POE_8_PORT_PRICE_ID', 'price_1TwxGjLeOpYDAzweuDZxytf3');
define('POE_16_PORT_PRICE_ID', 'price_1TwxKALeOpYDAzweEjb26IK0');
define('POE_24_PORT_PRICE_ID', 'price_1TwxMELeOpYDAzweMoI6xuIZ');
define('POE_48_PORT_PRICE_ID', 'price_1TwxNsLeOpYDAzwew9WMiGV4');

// ---------------------------------------------------------------------
// NEW: staging hardware -- Ryzen appliances + Numato relay module.
//
// Every constant below is read exactly like every pre-existing Price ID
// constant above (config_value() in stripe-checkout.php already treats
// an unset/empty constant as "not payable yet" and fails closed -- see
// camera_checkout.php's and stripe-checkout.php's existing handling).
// These four are STRIPE TEST/SANDBOX Price IDs, confirmed by the site
// owner -- NOT live/production values. Do not copy these into the live
// stripe-config.php; the live file must only ever hold live-mode
// (price_... created under a live Stripe account) IDs.
//
// Prices match the approved staging source of truth (website-pricing-
// review/build-your-system.html) as of this pass:
//   AnyAiCam Starter (MINISFORUM AI X1-255 -- Ryzen 7 255)      $1,249.99 one-time (124999 cents)
//   AnyAiCam Professional (MINISFORUM AI X1 -- Ryzen AI 9 HX 470) $1,749.99 one-time (174999 cents)
//   AnyAiCam Enterprise (MINISFORUM AI X1 Pro-470)  $2,249.99 one-time (224999 cents)
//   Numato Lab 3-Channel Ethernet Relay Module                 $149.99 one-time ( 14999 cents)
//
// Display-name rename (this pass): customer-facing names are now
// "AnyAiCam Starter/Professional/Enterprise" -- the underlying hardware
// model spec is unchanged, and the AAC Facial Recognition unit's correct
// model is still "MINISFORUM AI X1 Pro-470", never "AI X1-255" (that
// model belongs to the Starter unit, a different unit entirely). Only
// the 'name' values in $hardwareCatalog below changed; the array keys
// (SKUs) and price_id lookups are untouched.
//
// RYZEN_ENTERPRISE_PRICE_ID is deliberately named distinctly from any
// existing "enterprise" identifier in this codebase (there is none in
// this PHP site's own config; the AnyAiCam-VMS backend separately has a
// legacy "enterprise" SOFTWARE plan key that must never be confused with
// this HARDWARE appliance -- see that repo's customer_entitlements.py/
// hardware_orders.py for the equivalent guard on that side).
// Hardcoded exactly like every other Price ID constant above (Price IDs
// are not secrets -- only STRIPE_SECRET_KEY/STRIPE_WEBHOOK_SECRET use the
// env/local-secrets-file mechanism in this file).
define('RYZEN_STARTER_PRICE_ID', 'price_1UDnklGllhK80H2nBAHTk6ZP');
define('RYZEN_ENTERPRISE_PRICE_ID', 'price_1UDnmeGllhK80H2nshtCoJD5');
define('RYZEN_AAC_FACIAL_PRICE_ID', 'price_1UDnnyGllhK80H2nlbwTWX69');
define('RELAY_MODULE_3CH_PRICE_ID', 'price_1UDnpkGllhK80H2n2EA2lFmQ');

// ---------------------------------------------------------------------
// NEW: staging recurring products -- Local/Hybrid camera-slot
// subscriptions + the 10 analytics add-ons. No live equivalent exists
// yet (this checkout path has never been built anywhere -- website or
// backend). SANDBOX TEST Price IDs only, verified against the current
// Stripe TEST catalog (see the reconciliation pass that confirmed these
// exact IDs and amounts). Never copy these into a live config file --
// this file has no test/live switch of its own, exactly like every
// hardware constant above; environment is what's staged, not the code.
//
// LOCAL -- recurring monthly, $0.60/$0.61/$0.62/$0.63 TEST.
define('LOCAL_1_8_PRICE_ID', 'price_1UD2xKGllhK80H2nFJwtFJvw');
define('LOCAL_9_16_PRICE_ID', 'price_1UD2yLGllhK80H2n7Z2q8AM3');
define('LOCAL_17_32_PRICE_ID', 'price_1UD2z4GllhK80H2ncjHZVhms');
define('LOCAL_33_64_PRICE_ID', 'price_1UD30bGllhK80H2nfmvPZnSA');
// HYBRID -- recurring monthly, $0.70/$0.71/$0.72/$0.73 TEST.
define('HYBRID_1_8_PRICE_ID', 'price_1UD31AGllhK80H2nMKtYEmVw');
define('HYBRID_9_16_PRICE_ID', 'price_1UD31mGllhK80H2nJqorGzU6');
define('HYBRID_17_32_PRICE_ID', 'price_1UD32kGllhK80H2nGaOQB9XI');
define('HYBRID_33_64_PRICE_ID', 'price_1UD33SGllhK80H2nxrbBT2ch');
// ANALYTICS -- recurring monthly add-ons, $0.80-$0.89 TEST. Facial
// Recognition here is deliberately distinct from the one-time
// RYZEN_AAC_FACIAL_PRICE_ID hardware appliance above -- it stays an
// analytics add-on, never an appliance category (see app/analytics_
// entitlements.py's own module docstring for the same separation on
// the backend side).
define('ANALYTICS_SMART_MOTION_PRICE_ID', 'price_1UD34JGllhK80H2nxP2T6pq0');
define('ANALYTICS_PEOPLE_COUNTING_PRICE_ID', 'price_1UD353GllhK80H2nEaJnu9jZ');
define('ANALYTICS_LPR_PRICE_ID', 'price_1UD35kGllhK80H2nFIdXc5yW');
define('ANALYTICS_PPE_PRICE_ID', 'price_1UD36UGllhK80H2nfDIIb4kC');
define('ANALYTICS_TALK_DOWN_PRICE_ID', 'price_1UD377GllhK80H2nC1Z2VNh0');
define('ANALYTICS_AI_ESSENTIALS_PRICE_ID', 'price_1UD37tGllhK80H2naT95ryKA');
define('ANALYTICS_AI_PROFESSIONAL_PRICE_ID', 'price_1UD38aGllhK80H2nMSdir4BE');
define('ANALYTICS_VEHICLE_INTELLIGENCE_PRICE_ID', 'price_1UD39XGllhK80H2nnyJJoXKQ');
define('ANALYTICS_CLOUD_OVERFLOW_PRICE_ID', 'price_1UD3UMGllhK80H2nqMJhbPe6');
define('ANALYTICS_FACIAL_RECOGNITION_PRICE_ID', 'price_1UD3UuGllhK80H2nAdGKN4ff');

// Env-overridable, same convention as STRIPE_SECRET_KEY/STRIPE_WEBHOOK_SECRET
// above: the deployed storefront container sets ANYAICAM_STOREFRONT_BASE_URL
// to https://store-staging.anyaicam.com so checkout-session.php's success_url/
// cancel_url point at the real staging storefront host instead of the live
// domain. Falls back to the live value so this file's behavior is unchanged
// for anyone running it without that env var set.
define('BASE_URL', getenv('ANYAICAM_STOREFRONT_BASE_URL') ?: 'https://anyaicam.com');
