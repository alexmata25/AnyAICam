<?php
/**
 * ANY AI CAM PoE email helper.
 *
 * Add this file beside save-checkout-lead.php, then require it near the top:
 *     require_once __DIR__ . '/poe-email-helper.php';
 *
 * Parse the wizard hardware list with:
 *     $hardwareItems = anyaicam_parse_hardware_items($wizardData);
 *
 * Add the returned text inside BOTH the admin and customer
 * "HARDWARE PURCHASED/PAID TODAY" sections:
 *     $adminMessage .= anyaicam_hardware_email_text($hardwareItems);
 *     $customerMessage .= anyaicam_hardware_email_text($hardwareItems);
 *
 * Use anyaicam_hardware_total($hardwareItems) when calculating hardwarePaidToday.
 */

declare(strict_types=1);

function anyaicam_hardware_catalog(): array {
    return [
        'AIC-CAM-5MP-DOME-001' => [
            'kind' => 'camera',
            'name' => 'VIVOTEK FD9380-HTV-V2 5MP Outdoor Dome AI Camera',
            'unitPrice' => 349.99,
        ],
        'AIC-CAM-BULLET-IB9380' => [
            'kind' => 'camera',
            'name' => 'VIVOTEK IB9380-HTV-V2 5MP Outdoor Bullet AI Camera',
            'unitPrice' => 349.99,
        ],
        'AIC-CAM-FISHEYE-FE9380' => [
            'kind' => 'camera',
            'name' => 'VIVOTEK FE9380-HV 5MP Fisheye Panoramic Camera',
            'unitPrice' => 649.99,
        ],
        'AIC-CAM-DUAL-MA9312' => [
            'kind' => 'camera',
            'name' => 'VIVOTEK MA9312-EHTV Dual-Directional 4K AI Camera',
            'unitPrice' => 1799.99,
        ],
        'AIC-POE-MOKER-8' => [
            'kind' => 'poe',
            'name' => 'MokerLink POE-F082G 8-Port PoE Switch with 2 Gigabit Uplinks',
            'unitPrice' => 79.99,
        ],
        'AIC-POE-MOKER-16' => [
            'kind' => 'poe',
            'name' => 'MokerLink POE-G162G 16-Port Gigabit PoE+ Switch with 2 Gigabit Uplinks',
            'unitPrice' => 174.99,
        ],
        'AIC-POE-MOKER-24' => [
            'kind' => 'poe',
            'name' => 'MokerLink POE-G244GS 24-Port Gigabit PoE+ Switch with Ethernet and SFP Uplinks',
            'unitPrice' => 229.99,
        ],
        'AIC-POE-MOKER-48' => [
            'kind' => 'poe',
            'name' => 'MokerLink POE-G482GS 48-Port Gigabit PoE Switch with 2 SFP Uplinks',
            'unitPrice' => 429.99,
        ],
    ];
}

function anyaicam_parse_hardware_items(array $wizardData): array {
    $catalog = anyaicam_hardware_catalog();
    $rawItems = is_array($wizardData['hardwareItems'] ?? null)
        ? $wizardData['hardwareItems']
        : [];

    // Backward compatibility with the former one-camera-only payload.
    if (!$rawItems && (int)($wizardData['cameraHardwareQty'] ?? 0) > 0) {
        $rawItems[] = [
            'sku' => 'AIC-CAM-5MP-DOME-001',
            'quantity' => (int)$wizardData['cameraHardwareQty'],
        ];
    }

    $items = [];
    $seen = [];

    foreach ($rawItems as $rawItem) {
        if (!is_array($rawItem)) continue;

        $sku = trim((string)($rawItem['sku'] ?? ''));
        $quantity = max(0, min(64, (int)($rawItem['quantity'] ?? 0)));

        if ($quantity < 1 || isset($seen[$sku]) || !isset($catalog[$sku])) continue;

        $seen[$sku] = true;
        $product = $catalog[$sku];
        $items[] = [
            'sku' => $sku,
            'kind' => $product['kind'],
            'name' => $product['name'],
            'quantity' => $quantity,
            'unitPrice' => (float)$product['unitPrice'],
            'subtotal' => round($quantity * (float)$product['unitPrice'], 2),
        ];
    }

    return $items;
}

function anyaicam_hardware_total(array $items): float {
    $total = 0.0;
    foreach ($items as $item) {
        if (!is_array($item)) continue;
        $total += (float)($item['subtotal'] ?? 0);
    }
    return round($total, 2);
}

function anyaicam_hardware_email_text(array $items): string {
    if (!$items) {
        return "Camera / PoE hardware: None\n\n";
    }

    $text = '';
    foreach ($items as $item) {
        if (!is_array($item)) continue;

        $kind = ($item['kind'] ?? '') === 'poe' ? 'PoE switch' : 'Camera';
        $text .= $kind . ': ' . ($item['name'] ?? 'Hardware item') . "\n";
        $text .= 'SKU: ' . ($item['sku'] ?? '') . "\n";
        $text .= 'Quantity: ' . (int)($item['quantity'] ?? 0) . "\n";
        $text .= 'Unit price: $' . number_format((float)($item['unitPrice'] ?? 0), 2) . "\n";
        $text .= 'Subtotal: $' . number_format((float)($item['subtotal'] ?? 0), 2) . "\n\n";
    }

    return $text;
}

// LTS PoE products are validated through the locked order catalog in api.php and stripe-checkout.php.
