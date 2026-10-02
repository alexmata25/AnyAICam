document.addEventListener("DOMContentLoaded", function () {
  const stylesheet = document.createElement("link");
  stylesheet.rel = "stylesheet";
  stylesheet.href = "staging-footer.css";
  document.head.appendChild(stylesheet);
  const footer = document.querySelector(".stage-footer");
  if (!footer) return;
  footer.innerHTML = `
    <div class="footer-grid wrap">
      <section class="footer-brand-block" aria-label="ANY AI CAM company information">
        <img src="full-logo-transparent.png" alt="ANY AI CAM AI Cloud Protection">
        <p>Local-first and hybrid video management, edge appliances, cloud protection, and modular AI analytics.</p>
        <p class="footer-stage-note"><strong>Staging only:</strong> Stripe Test Mode, $1/year test pricing, and mock provisioning.</p>
      </section>
      <section><h2>Products</h2><a href="plans.html">Plan Explainer</a><a href="pricing.html">Test Pricing</a><a href="analytics.html">Analytics Catalog</a><a href="hardware.html">Hardware Selector</a><a href="build-your-system.html">Build Your System</a><a href="cart.html">Test Cart</a></section>
      <section><h2>Resources</h2><a href="signin.html">Account Preview</a><a href="support.html">Support</a><a href="contact.html">Contact</a><a href="sales-partner-login.html">Sales Partner Login</a><a href="/camera-compatibility-check.html">Camera Compatibility</a><a href="/cloud-storage-pricing.html">Videoloft Cloud Pricing</a></section>
      <section><h2>Contact</h2><a href="tel:+13465544699">(346) 554-4699</a><a href="mailto:amata@anyaicam.com">amata@anyaicam.com</a><address>6218 Arcadia Sound Lane<br>Porter, Texas 77365</address></section>
    </div>
    <div class="footer-bottom wrap"><span>© 2026 ANY AI CAM. All rights reserved.</span><nav aria-label="Policy links"><a href="/privacy-policy.html">Privacy Policy</a><a href="/terms.html">Terms of Service</a><a href="/shipping-returns.html">Shipping &amp; Returns</a></nav></div>`;
});
