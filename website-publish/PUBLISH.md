# anyaicam.com publish package — FOR REVIEW, NOT UPLOADED

Built from `website/` (branch website/source-and-products-20260929) against the real Bluehost source import (commit 040664b).
Nothing has been uploaded. Publishing stays a manual owner step (cPanel File Manager or SFTP) after review.

## 1. Upload to public_html (keep the folder structure; `app-screens/` is a new folder)

| File | Change | sha256 |
|---|---|---|
| aac-features.html | new | `fb7a8d5bddfb9e41…` |
| aaco.html | new | `934af44b8e576fa6…` |
| app-screens/aaco-person-events.webp | new | `773094d6b4048d5d…` |
| app-screens/live-front-door-phone.webp | new | `eeb003474aabffa7…` |
| app-screens/secure-edge-arm-disarm.webp | new | `1faf4b83ff7c4609…` |
| app-screens/visitor-call-email-phone.webp | new | `a423a4d645a9be7e…` |
| app-screens/visitor-call-settings.webp | new | `839bf0f5ac237739…` |
| app-screens/vms-dashboard-security.webp | new | `9b86da28cc95833d…` |
| app-screens/vms-smart-alerts.webp | new | `9ff152ce5ff77cb0…` |
| secure-edge.html | new | `c42c350fe4776c58…` |
| sitemap.xml | new | `4577f02ce49bb70a…` |
| visitor-call.html | new | `f81356a61ee28a3f…` |
| adapters.html | changed | `151ca093cfb376cd…` |
| analytics.html | changed | `e5c77e2753aba42b…` |
| build-your-system.html | changed | `8173caf6d5a0f8c2…` |
| cameras.html | changed | `baec61055db73c8f…` |
| cloud-setup-wizard.html | changed | `6c16fa3edd36ad9f…` |
| cloud-storage-pricing.html | changed | `e14b9541ce674ca3…` |
| customer-checkout.html | changed | `79e6ea4d4f41191c…` |
| customer-login.html | changed | `a0cff7270b91b242…` |
| customer-setup.html | changed | `e8f486660482eca9…` |
| edge-appliance.html | changed | `99301abb1f07e528…` |
| face-access.html | changed | `70a5844ee13fba74…` |
| hardware.html | changed | `11389f3f66febbaf…` |
| how-vms-works.html | changed | `c80f6e5942fb3c21…` |
| index.html | changed | `507978436257dfef…` |
| partner-sales-calculator.html | changed | `8b3de075a6705f3c…` |
| plans.html | changed | `b32b52a6b4bb988c…` |
| pricing.html | changed | `cd1e22a6dfb13fa7…` |
| sales-calculator.html | changed | `3f844d1a3e43af23…` |
| sales-partner-login.html | changed | `33f0464c2d734a84…` |
| software-free-trial-review.html | changed | `cebddc0d60e92d32…` |
| support-diagnostic.html | changed | `9d3f1631be5eecc6…` |
| support-escalation.html | changed | `c10834f88d876b51…` |
| support.html | changed | `b906ca0ac548560f…` |
| videoloft-partner-sales-calculator.html | changed | `8b3de075a6705f3c…` |
| vms-contact.html | changed | `d6c2e87a0b443733…` |
| vms-features.html | changed | `bccb958f92c982ee…` |
| vms-footer.js | changed | `6052d1588142c633…` |
| vms-partner-admin.html | changed | `45d1565a62bbdb18…` |
| vms-partner-apply.html | changed | `a03ad30b95fda7bd…` |
| vms-partner-sales.html | changed | `e61ea70ae9970699…` |
| vms-support.html | changed | `423287cc4707ccb8…` |
| vms.html | changed | `75b27d953fafff83…` |

## 2. Remove from public_html (security / clean-up)

Move these out of public_html (for example into a private folder in your home directory):

- `CMHT1722-28LS, 2MP HD-TVI.html`
- `CMHT1752-28LS, 5 MP TVI.html`
- `CMHT1782-28LF, Platinum Plus 8 MP TVI.html`
- `CMIP3382WI-28SDL, Platinum Plus, 8MP.html`
- `CMIP3C42WI-28SDL, Platinum Plus, 4 MP Color 247.html`
- `CMIP3C82WI-28SDL, 8 MP Color 247.html`
- `LTCMIP3743W2-DLZ, Platinum Plus 4 MP.html`
- `LXIP1142W-28MA, Pro-X, 4MP, IP.html`
- `LXIP7553W4-SZ12, Pro-X, IP, 4 Lens Panoramic Dome IP Camera.html`
- `VSIP7552FW-SE, Pro-VS, Fisheye, IP, 5MP.html`
- `VSPOE-SW1602, 16 Port PoE Switch.html`
- `VSPOE-SW2402, Pro-VS, 24 Port PoE Switch.html`
- `VSPOE-SW802, 8 PoE Port Switch with 2 Port Uplink.html`
- `anyaicam-test-checkout.html`
- `anyaicam-test-checkout.php`
- `anyaicam-test-webhook.php`
- `homepage-button-snippet.html`

Also add the `.htaccess` rule that blocks internal notes, logs and archives, and move `AnyAiCam_Login_and_Terminal_Reference.zip` and `AnyAiCam-Test.zip` out of public_html.

## 3. After uploading

1. Purge the Cloudflare cache.
2. Open https://anyaicam.com/aac-features.html, aaco.html, visitor-call.html and secure-edge.html on a computer and a phone.
3. Check plans.html and analytics.html: no 'Staging only' footer line.
4. Check support-diagnostic.html: the problem buttons respond.

## Rollback

`rollback-original/` holds the exact source bytes of every changed file. New files can simply be deleted.
