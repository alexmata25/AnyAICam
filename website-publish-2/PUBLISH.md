# anyaicam.com update 2 (2026-09-29): footer fix + Live/Playback screenshots

Upload the contents of `upload/` into `public_html`, keeping the folder layout:

| File | Change |
|---|---|
| plans.html, analytics.html | Standard site footer (fixes the oversized logo and sideways scrolling) |
| vms.html | New "In the AnyAiCam app" section with two screenshots |
| app-screens/vms-live-camera.webp, app-screens/vms-playback-timeline.webp | New images (plates and a third-party logo blurred) |

Nothing is removed. No PHP, config or credential files are included.
Rollback: upload `rollback-original/` (the three pages exactly as published on 2026-09-29) and delete the two new images.
Checksums: `SHA256SUMS.txt`. After uploading, purge the Cloudflare cache.
