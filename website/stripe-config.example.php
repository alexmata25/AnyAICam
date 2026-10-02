<?php
/**
 * EXAMPLE ONLY. Do not upload this file with placeholder values unchanged.
 * Rotate the Stripe secret key that was exposed in chat/file uploads.
 */
define('STRIPE_SECRET_KEY', 'sk_live_REPLACE_WITH_NEW_ROTATED_KEY');
define('STRIPE_PUBLISHABLE_KEY', 'pk_live_REPLACE_WITH_YOUR_KEY');

define('ADAPTER_8CH_PRICE_ID', 'price_REPLACE');
define('ADAPTER_16CH_PRICE_ID', 'price_REPLACE');
define('ADAPTER_32CH_PRICE_ID', 'price_REPLACE');
define('ADAPTER_64CH_PRICE_ID', 'price_REPLACE');

define('STRIPE_WEBHOOK_SECRET', 'whsec_REPLACE_AFTER_CREATING_WEBHOOK');
define('BASE_URL', 'https://anyaicam.com');

// Stripe one-time Price ID for the VIVOTEK FD9380-HTV-V2 camera.
define('CAMERA_5MP_DOME_PRICE_ID', 'price_1TwwzkLeOpYDAzweaUOb0Hu8');
define('CAMERA_5MP_BULLET_PRICE_ID', 'price_1Twx2dLeOpYDAzweRLXdygCP');
define('CAMERA_5MP_FISHEYE_PRICE_ID', 'price_1Twx55LeOpYDAzweDoUvcVTN');
define('CAMERA_DUAL_LENS_PRICE_ID', 'price_1TwxA0LeOpYDAzwegMdXH5zr');
define('POE_8_PORT_PRICE_ID', 'price_1TwxGjLeOpYDAzweuDZxytf3');
define('POE_16_PORT_PRICE_ID', 'price_1TwxKALeOpYDAzweEjb26IK0');
define('POE_24_PORT_PRICE_ID', 'price_1TwxMELeOpYDAzweMoI6xuIZ');
define('POE_48_PORT_PRICE_ID', 'price_1TwxNsLeOpYDAzwew9WMiGV4');

// LTS camera and PoE switch Stripe Price IDs
define('LTS_LXIP1142_PRICE_ID', ''); // Add Stripe one-time Price ID: price_...
define('LTS_CMIP3382_PRICE_ID', ''); // Add Stripe one-time Price ID: price_...
define('LTS_CMIP3C42_PRICE_ID', ''); // Add Stripe one-time Price ID: price_...
define('LTS_CMIP3C82_PRICE_ID', ''); // Add Stripe one-time Price ID: price_...
define('LTS_CMHT1722_PRICE_ID', ''); // Add Stripe one-time Price ID: price_...
define('LTS_CMHT1752_PRICE_ID', ''); // Add Stripe one-time Price ID: price_...
define('LTS_CMHT1782_PRICE_ID', ''); // Add Stripe one-time Price ID: price_...
define('LTS_POE_SW802_PRICE_ID', ''); // Add Stripe one-time Price ID: price_...
define('LTS_POE_SW1602_PRICE_ID', ''); // Add Stripe one-time Price ID: price_...
define('LTS_POE_SW2402_PRICE_ID', ''); // Add Stripe one-time Price ID: price_...
