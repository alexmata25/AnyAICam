# anyaicam.com upload package

Release blocks: **release blocks pending**. This is a preparation build: do not upload it. Run `website_release.py publish` with the final installer, then rebuild this package.

## Upload (50 files)
Copy `upload/` into `public_html`, keeping the folder layout. Nothing is deleted from the host.

- `404.html`
- `adapters.html`
- `analytics.html`
- `basket.html`
- `build-your-system.html`
- `camera-compatibility-check.html`
- `cameras.html`
- `cloud-storage-pricing.html`
- `contact.html`
- `customer-checkout.html`
- `customer-setup.html`
- `edge-appliance.html`
- `face-access.html`
- `hardware.html`
- `how-it-works.html`
- `how-vms-works.html`
- `index-no-public-qr.html`
- `index.html`
- `nvr.html`
- `partner-assisted-setup.html`
- `partner-sales-calculator.html`
- `plans.html`
- `pricing.html`
- `privacy-policy.html`
- `sales-calculator.html`
- `sales-partner-login.html`
- `secure-edge.html`
- `shipping-returns.html`
- `sitemap.xml`
- `software-free-trial-request.html`
- `software-trial-request.html`
- `software.html`
- `starter-camera-trial.html`
- `support-diagnostic.html`
- `support-escalation.html`
- `support.html`
- `terms.html`
- `thank-you.html`
- `troubleshooting.html`
- `videoloft-partner-referral-wizard.html`
- `videoloft-partner-sales-calculator.html`
- `videoloft-partner.html`
- `vms-features.html`
- `vms-footer.js`
- `vms-linux.html` (new)
- `vms-partner-apply.html`
- `vms-partner-sales.html`
- `vms-product.css`
- `vms-support.html`
- `vms.html`

## Not uploaded
- 120 files already match the live site.
- 24 files (PHP endpoints, .htaccess and unchanged assets) are already on the host exactly as exported.
- Never upload repository notes (README.md, SOURCE_MANIFEST.json, SERVER_CLEANUP.json), example configs, tests, tools or docs. Secrets (config.php, mail_config.php, stripe-config.php, api.php) stay on the host.

## Special handling
- `CCTV connect image.webp`: the live URL serves different bytes from the host file (CDN); not uploaded
- If `ARCHIVED-0.1.3-DOWNLOADS.md` exists in `public_html`, delete it (internal note with 0.1.3 links).

## Delete from public_html (internal test pages and snippets, see website/SERVER_CLEANUP.json)
Move them outside `public_html` if you want to keep them. PHP files cannot be checked over HTTP; delete them if present.

- `anyaicam-test-checkout.html` (still served)
- `homepage-button-snippet.html` (still served)
- `anyaicam-test-checkout.php` (PHP: check in File Manager)
- `anyaicam-test-webhook.php` (PHP: check in File Manager)

## After uploading
1. Purge the Cloudflare cache.
2. Run `python tools/website_publish_package.py build --out <new folder>`: every file should report `same as live`.

## Rollback
Upload `rollback-original/` (the host source of every replaced file) and delete the files marked (new).
Checksums of `upload/`: `SHA256SUMS.txt`.
