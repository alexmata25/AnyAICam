# anyaicam.com public website (version-controlled copy)

## Where the live site comes from
- **Host:** Bluehost shared hosting (`shared.bluehost.com`, response header `host-header`) behind Cloudflare.
- **Publishing:** manual, by the owner, through cPanel File Manager or SFTP with an operator-added key. Nothing publishes to Bluehost automatically; see docs/PROJECT_CHECKPOINT.md, "Website publish method".
- **Before 2026-09-29** the site had no source-controlled copy. Only partial page snapshots existed, in `AnyAiCam-VMS/website-pricing-review` and `storefront/` (the staging checkout review).

## This directory
- **First commit:** a read-only HTTP mirror of every public file the site references, taken 2026-09-29 (GET only; see `MIRROR_MANIFEST.json` for each file's sha256 and Last-Modified). It is byte-for-byte what visitors were served. Cloudflare's injected email-protection markup and analytics beacon are included wherever the served HTML contained them.
- **Later commits:** reviewable fixes. Each one lists the files to upload.

## Not in this copy (server-side, cannot be read over HTTP)
The pages call these PHP endpoints on the host. The owner must download them from cPanel into this directory, **without** secrets:
- `api.php` (quote save, partner lookup, software trial, referral tracking)
- `contact-submit.php`
- `save-checkout-lead.php`
- `stripe-checkout.php`
- `submit-software-trial-wizard.php`
- `config.php`: **never commit it.** It is listed in `.gitignore`. Commit a `config.example.php` with placeholder values instead.

## Adopting this as the source of truth (owner steps)
1. From cPanel, download the full `public_html` (File Manager → Compress → Download).
2. Compare it with this directory. The mirror is the served output, so any file that exists only on the host (PHP, `.htaccess`, unlinked pages) gets added here, secrets excluded.
3. From then on, change files here, review the diff, then upload only the changed files listed in the commit.
4. Optional later: an SFTP deploy key and a scripted upload of exactly the changed files, still run by the owner.
