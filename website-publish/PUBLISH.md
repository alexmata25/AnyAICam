# anyaicam.com publish package (2026-09-29)

Prepared from website/ (branch website/fixes-20260929). **Nothing has been uploaded.** Publishing stays a manual owner step (cPanel or SFTP).

## Upload (cPanel File Manager -> public_html, overwrite)

| File | sha256 (new) | What changed |
|---|---|---|
| adapters.html | `b3cbc16158ca64314f86b582577e45ad46bd52d62a2f016bfea056f1a2605345` | Removes expired LiveChat widget |
| cameras.html | `e620dd834bb48c504d624616fd4c4693c92c0a7d0796a47c3adcf8bdb33e16cd` | Removes expired LiveChat widget |
| cloud-setup-wizard.html | `74253dceb2d54c4a585e8d96f94bc12edfa9ece041483f83e9a8cea5e4f67221` | Removes expired LiveChat widget |
| cloud-storage-pricing.html | `cac876ac1ea978964387f691bcb4c52990c9365fd6dbc33c3d9b759f4c33e3b9` | Removes expired LiveChat widget |
| customer-login.html | `c150800b43702a4eb54afb39922d25ad027a3d34b2db0edb6b80ed4aeb043568` | Customer Login goes to app.anyaicam.com/customer-login.html, not the /login recovery page |
| pricing.html | `35a3443defa709db6aa813555849d9bc389fd529eedba2efebd16bcbc60de547` | Removes expired LiveChat widget |
| support-diagnostic.html | `6343740fa1b30787d01a87b44377e256dcd4bda0817eae7756abbe45ba761ddd` | Fixes syntax error: the support diagnostic tool works again |
| support.html | `b7505df47c22eddf5b0e09242e87df47e6c81cdec407dd890bb95749e9b14f56` | Stops CAPTCHA script error (no behaviour change) |
| videoloft-partner-sales-calculator.html | `7f6d1a7cc44c56b4ad9cc6ebe41a83efab4ea6374c3d793cc1c3203b73c02b77` | Partner calculator shows totals and restores quotes on load again |
| vms-footer.js | `6052d1588142c63312cb31ce53bca549cfe185b21b2971e084250393f43d5878` | Removes 'Staging only / Stripe Test Mode / $1 test pricing', Test Cart (404), staging-footer.css (404) - used by plans/analytics/hardware |

## After uploading

1. In Cloudflare, purge the cache for these URLs, or use Caching -> Purge Everything.
2. Check https://anyaicam.com/plans.html and analytics.html: the footer has no 'Staging only' line and no Test Cart link.
3. Check https://anyaicam.com/support-diagnostic.html: the six problem buttons respond.
4. Check https://anyaicam.com/customer-login.html: it opens the AnyAiCam customer sign-in.
5. Check https://anyaicam.com/pricing.html: the browser console shows no 'License expired' error.

## Rollback

`rollback-original/` holds the exact bytes that were live on 2026-09-29 (sha256 values in website/MIRROR_MANIFEST.json). Upload them to restore the site as it was.
