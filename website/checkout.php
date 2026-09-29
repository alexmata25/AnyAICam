<?php
header('Content-Type: application/json');

// Stripe Secret Key
define('STRIPE_SECRET_KEY', 'sk_test_your_actual_secret_key_here');

// Only allow POST
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['error' => 'Method not allowed']);
    exit;
}

// Allowed adapter Stripe Price IDs
$allowed_price_ids = [
    'price_8channel_here',
    'price_16channel_here',
    'price_32channel_here',
    'price_64channel_here'
];

// Get POST data
$price_id = trim($_POST['price_id'] ?? '');
$product_type = trim($_POST['product_type'] ?? '');
$email = trim($_POST['email'] ?? '');
$customer_name = trim($_POST['customer_name'] ?? '');
$company_name = trim($_POST['company_name'] ?? '');
$phone = trim($_POST['phone'] ?? '');
$checkout_data_raw = $_POST['checkout_data'] ?? '';

// Only allow adapter checkout
if ($product_type !== 'adapter') {
    http_response_code(400);
    echo json_encode([
        'error' => 'Only adapter payments are accepted through Stripe. Cloud billing is handled separately by Videoloft.'
    ]);
    exit;
}

// Validate required fields
if (!$price_id) {
    http_response_code(400);
    echo json_encode(['error' => 'Missing price ID']);
    exit;
}

// Validate allowed adapter price ID
if (!in_array($price_id, $allowed_price_ids, true)) {
    http_response_code(400);
    echo json_encode(['error' => 'Invalid adapter price ID']);
    exit;
}

// Decode optional wizard data
$checkout_data = json_decode($checkout_data_raw, true);
if (!is_array($checkout_data)) {
    $checkout_data = [];
}

// Base URLs
$base_url = 'https://anyaicam.com';
$success_url = $base_url . '/success.html?session_id={CHECKOUT_SESSION_ID}';
$cancel_url = $base_url . '/cloud-checkout.html';

// Build metadata
$metadata = [
    'product_type' => 'adapter',
    'wizard_flow' => 'true',
    'adapter_type' => $checkout_data['adapterType'] ?? '',
    'include_cloud' => !empty($checkout_data['includeCloud']) ? 'yes' : 'no',
    'billing_cycle' => $checkout_data['billingCycle'] ?? '',
    'customer_name' => $customer_name,
    'company_name' => $company_name,
    'phone' => $phone
];

if (!empty($checkout_data['resolution'])) {
    $metadata['cloud_resolution'] = $checkout_data['resolution'];
}
if (!empty($checkout_data['quantity'])) {
    $metadata['camera_quantity'] = (string)$checkout_data['quantity'];
}
if (!empty($checkout_data['recordingType'])) {
    $metadata['recording_type'] = $checkout_data['recordingType'];
}
if (!empty($checkout_data['duration'])) {
    $metadata['duration_days'] = (string)$checkout_data['duration'];
}

// Create Stripe Checkout Session
$ch = curl_init();

$post_fields = [
    'mode' => 'payment',
    'success_url' => $success_url,
    'cancel_url' => $cancel_url,
    'line_items[0][price]' => $price_id,
    'line_items[0][quantity]' => 1,
];

if ($email !== '') {
    $post_fields['customer_email'] = $email;
}

// Add metadata fields in Stripe-compatible format
foreach ($metadata as $key => $value) {
    $post_fields["metadata[$key]"] = $value;
}

curl_setopt_array($ch, [
    CURLOPT_URL => 'https://api.stripe.com/v1/checkout/sessions',
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_POST => true,
    CURLOPT_HTTPHEADER => [
        'Authorization: Bearer ' . STRIPE_SECRET_KEY,
        'Content-Type: application/x-www-form-urlencoded',
    ],
    CURLOPT_POSTFIELDS => http_build_query($post_fields),
]);

$response = curl_exec($ch);

if ($response === false) {
    http_response_code(500);
    echo json_encode([
        'error' => 'cURL error while creating checkout session',
        'details' => curl_error($ch)
    ]);
    curl_close($ch);
    exit;
}

$http_code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
curl_close($ch);

$data = json_decode($response, true);

if ($http_code !== 200 || empty($data['url'])) {
    http_response_code(500);
    echo json_encode([
        'error' => 'Failed to create checkout session',
        'details' => $data ?: $response
    ]);
    exit;
}

echo json_encode([
    'url' => $data['url']
]);
?>
