<?php
declare(strict_types=1);

if (is_file(__DIR__ . '/stripe-config.php')) {
    require_once __DIR__ . '/stripe-config.php';
}

$orderCode = trim((string)($_GET['order'] ?? $_GET['order_id'] ?? ''));
$safeOrder = preg_match('/^AIC-O-[A-Z0-9-]+$/', $orderCode) ? $orderCode : '';
$supportEmail = defined('SUPPORT_EMAIL') ? (string)SUPPORT_EMAIL : 'amata@anyaicam.com';
$supportPhone = defined('SUPPORT_PHONE') ? (string)SUPPORT_PHONE : '(832) 510-8240';
?>
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,follow">
<title>Payment Received | ANY AI CAM</title>
<link rel="stylesheet" href="styles.css">
<style>
body{background:#f3f6fb;color:#0b1332}.wrap{max-width:780px;margin:4rem auto;padding:1rem}.card{background:#fff;border:1px solid #dce6f5;border-radius:18px;padding:2rem;box-shadow:0 18px 45px rgba(23,38,70,.12)}.status{display:inline-flex;align-items:center;gap:.5rem;padding:.45rem .75rem;border-radius:999px;background:#e8f8f0;color:#16734e;font-weight:900}.pending{background:#fff7e0;color:#8a6412}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.8rem;margin:1.25rem 0}.item{padding:1rem;border:1px solid #dce6f5;border-radius:12px;background:#f8fafc}.item span{display:block;color:#63708a;font-size:.78rem;font-weight:800;text-transform:uppercase}.item strong{display:block;margin-top:.25rem;overflow-wrap:anywhere}.notice{margin-top:1rem;padding:1rem;border-left:5px solid #5fc4d1;background:#eefaff;border-radius:8px}.button{display:inline-block;margin-top:1.25rem;padding:.85rem 1.2rem;border-radius:9px;background:#0b2b4f;color:#fff;text-decoration:none;font-weight:800}@media(max-width:640px){.grid{grid-template-columns:1fr}.wrap{margin:1.5rem auto}.card{padding:1.25rem}}
</style>
</head>
<body>
<main class="wrap">
<section class="card">
<p class="status pending" id="paymentStatus">Confirming payment...</p>
<h1 id="headline">Payment Received</h1>
<p id="intro">Thank you. We are confirming your payment and preparing your ANY AI CAM setup.</p>
<div class="grid">
  <div class="item"><span>Customer</span><strong id="customerName">Confirming...</strong></div>
  <div class="item"><span>Order Number</span><strong id="orderNumber"><?= htmlspecialchars($safeOrder ?: 'Pending', ENT_QUOTES, 'UTF-8') ?></strong></div>
  <div class="item"><span>Amount Paid Today</span><strong id="amountPaid">Confirming...</strong></div>
  <div class="item"><span>Adapter / Order Details</span><strong id="adapterDetails">Confirming...</strong></div>
  <div class="item"><span>Quote ID</span><strong id="quoteId">Confirming...</strong></div>
  <div class="item"><span>Partner ID</span><strong id="partnerId">Confirming...</strong></div>
</div>
<div class="notice">
  <p><strong>Shipping:</strong> adapter delivery normally occurs within 7 days after the order is confirmed.</p>
  <p><strong>Cloud billing:</strong> cloud service billing begins after the 30-day free trial.</p>
  <p><strong>Support:</strong> <a href="mailto:<?= htmlspecialchars($supportEmail, ENT_QUOTES, 'UTF-8') ?>"><?= htmlspecialchars($supportEmail, ENT_QUOTES, 'UTF-8') ?></a> | <a href="tel:+18325108240"><?= htmlspecialchars($supportPhone, ENT_QUOTES, 'UTF-8') ?></a></p>
</div>
<a class="button" href="index.html">Return to ANY AI CAM</a>
</section>
</main>
<script>
(function(){
  const order = <?= json_encode($safeOrder, JSON_UNESCAPED_SLASHES) ?>;
  const statusEl = document.getElementById("paymentStatus");
  const setText = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = value || "Not available"; };
  let attempts = 0;
  async function poll(){
    if (!order) {
      statusEl.textContent = "Payment received";
      statusEl.classList.remove("pending");
      setText("customerName", "Customer");
      setText("orderNumber", "Confirmation email sent by Stripe");
      setText("amountPaid", "See Stripe receipt");
      setText("adapterDetails", "ANY AI CAM adapter order");
      setText("quoteId", "Not applicable");
      setText("partnerId", "Not applicable");
      document.getElementById("intro").textContent = "Your payment was processed. Stripe will email your receipt, and ANY AI CAM will prepare the next setup step.";
      return;
    }
    attempts += 1;
    try {
      const response = await fetch("payment-status.php?order=" + encodeURIComponent(order), {credentials:"same-origin", cache:"no-store"});
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || "Status unavailable");
      const data = result.order || {};
      setText("customerName", data.customer_name);
      setText("orderNumber", data.order_id);
      setText("amountPaid", data.amount_paid_today);
      setText("adapterDetails", [data.adapter, data.adapter_quantity ? "Qty " + data.adapter_quantity : ""].filter(Boolean).join(" - "));
      setText("quoteId", data.quote_id);
      setText("partnerId", data.partner_id);
      if (result.paid) {
        statusEl.textContent = "Payment confirmed";
        statusEl.classList.remove("pending");
        document.getElementById("intro").textContent = "Your payment has been confirmed. We are preparing your setup and shipping details.";
        return;
      }
    } catch (error) {
      statusEl.textContent = attempts >= 10 ? "Payment received - confirmation still processing" : "Confirming payment...";
    }
    if (attempts < 10) window.setTimeout(poll, 3000);
  }
  poll();
})();
</script>
</body>
</html>
