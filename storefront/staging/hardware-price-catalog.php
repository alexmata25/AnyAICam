<?php
declare(strict_types=1);

/**
 * STAGING/REVIEW COPY -- NOT the live Bluehost file.
 *
 * Extracted from stripe-checkout.php's own inline $hardwareCatalog
 * (verbatim, zero behavior change) so a second file -- checkout-
 * session.php, the new Local/Hybrid/analytics checkout endpoint -- can
 * resolve the same three appliance SKUs + the relay accessory without
 * requiring stripe-checkout.php itself (which is a full HTTP request
 * handler: reading php://input, checking $_SERVER['REQUEST_METHOD'],
 * exiting with its own JSON response -- not something a second script
 * can safely require_once mid-request). stripe-checkout.php now
 * requires this file instead of defining the array inline; nothing
 * about its own behavior changes.
 *
 * Returns the catalog array; callers assign it themselves
 * ($hardwareCatalog = require __DIR__ . '/hardware-price-catalog.php';)
 * rather than this file reaching into the caller's scope.
 */

function hardware_price_catalog_config_value(string $name, string $fallback = ''): string {
    return defined($name) ? trim((string)constant($name)) : $fallback;
}

return [
 'AIC-CAM-5MP-DOME-001'=>['name'=>'VIVOTEK FD9380-HTV-V2 5MP Outdoor Dome AI Camera','unit_amount'=>34999,'price_id'=>hardware_price_catalog_config_value('CAMERA_5MP_DOME_PRICE_ID')],
 'AIC-CAM-BULLET-IB9380'=>['name'=>'VIVOTEK IB9380-HTV-V2 5MP Outdoor Bullet AI Camera','unit_amount'=>34999,'price_id'=>hardware_price_catalog_config_value('CAMERA_5MP_BULLET_PRICE_ID')],
 'AIC-CAM-FISHEYE-FE9380'=>['name'=>'VIVOTEK FE9380-HV 5MP Fisheye Panoramic Camera','unit_amount'=>64999,'price_id'=>hardware_price_catalog_config_value('CAMERA_5MP_FISHEYE_PRICE_ID')],
 'AIC-CAM-DUAL-MA9312'=>['name'=>'VIVOTEK MA9312-EHTV Dual-Directional 4K AI Camera','unit_amount'=>179999,'price_id'=>hardware_price_catalog_config_value('CAMERA_DUAL_LENS_PRICE_ID')],
 'AIC-POE-MOKER-8'=>['name'=>'MokerLink POE-F082G 8-Port PoE Switch with 2 Gigabit Uplinks','unit_amount'=>7999,'price_id'=>hardware_price_catalog_config_value('POE_8_PORT_PRICE_ID')],
 'AIC-POE-MOKER-16'=>['name'=>'MokerLink POE-G162G 16-Port Gigabit PoE+ Switch with 2 Gigabit Uplinks','unit_amount'=>17499,'price_id'=>hardware_price_catalog_config_value('POE_16_PORT_PRICE_ID')],
 'AIC-POE-MOKER-24'=>['name'=>'MokerLink POE-G244GS 24-Port Gigabit PoE+ Switch with 2 Ethernet and 2 SFP Uplinks','unit_amount'=>22999,'price_id'=>hardware_price_catalog_config_value('POE_24_PORT_PRICE_ID')],
 'AIC-POE-MOKER-48'=>['name'=>'MokerLink POE-G482GS 48-Port Gigabit PoE Switch with 2 SFP Uplinks','unit_amount'=>42999,'price_id'=>hardware_price_catalog_config_value('POE_48_PORT_PRICE_ID')],
 // NEW: staging hardware -- one-time Ryzen appliances + Numato relay.
 // unit_amount is in cents, matching every other row's convention. This
 // fallback is only ever used if price_id resolves empty (see the
 // line-item loop below) -- with real Stripe TEST Price IDs configured
 // in staging/stripe-config.php, Stripe's own Price object is what
 // actually determines the charged amount; this unit_amount is kept in
 // sync purely for display/consistency and for the empty-price_id
 // fallback path itself.
 //
 // SANDBOX TEST AMOUNTS, not real retail: 50/51/52/53 cents, matching
 // the Stripe TEST Price IDs' own unit_amount. Display-name rename (an
 // earlier pass): 'name' is otherwise unchanged; the SKU (array key) and
 // price_id are unchanged. Real retail is 124999/174999/224999/14999
 // cents -- restore those values (and repoint price_id at live Price
 // IDs) before any promotion of this file to production.
 'AIC-APPLIANCE-RYZEN-STARTER'=>['name'=>'AnyAiCam Starter','unit_amount'=>50,'price_id'=>hardware_price_catalog_config_value('RYZEN_STARTER_PRICE_ID')],
 'AIC-APPLIANCE-RYZEN-ENTERPRISE'=>['name'=>'AnyAiCam Professional','unit_amount'=>51,'price_id'=>hardware_price_catalog_config_value('RYZEN_ENTERPRISE_PRICE_ID')],
 'AIC-APPLIANCE-RYZEN-AAC-FACIAL'=>['name'=>'AnyAiCam Enterprise','unit_amount'=>52,'price_id'=>hardware_price_catalog_config_value('RYZEN_AAC_FACIAL_PRICE_ID')],
 'AIC-RELAY-NUMATO-3CH'=>['name'=>'Numato 3-Channel Relay Module','unit_amount'=>53,'price_id'=>hardware_price_catalog_config_value('RELAY_MODULE_3CH_PRICE_ID')],
];
