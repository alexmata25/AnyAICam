ANY AI CAM — PHASE 6D
MOBILE APP EXPERIENCE, PUBLIC VMS ACCESS, PAID ANALYTICS, AND PUSH SETTINGS

Adds:
- A real /customer-portal route.
- Installable PWA support for Android, iPhone, iPad, and desktop.
- /manifest.webmanifest, /service-worker.js, and /offline.
- /mobile-app installation instructions and install prompt.
- /customer-app-settings for per-camera paid analytics and alerts.
- Smart Motion, People Counting, LPR, and PPE entitlements per camera.
- Backend enforcement: unpaid features cannot be enabled from the browser.
- Per-camera email, push, event type, and quiet-hour settings.
- Master-admin /analytics-entitlements page.
- Mobile push-device enrollment records.

Install:
1. Back up main.py, customer_platform.py, pwa_routes.py, and mobile_notifications.py.
2. Copy all four included Python files into the same VMS application folder.
3. Run: docker compose up -d --build
4. Test:
   https://app.anyaicam.com/customer-portal
   https://app.anyaicam.com/customer-app-settings
   https://app.anyaicam.com/mobile-app
   https://app.anyaicam.com/manifest.webmanifest
5. Grant paid analytics from /analytics-entitlements.
6. Sign in as the customer and configure each camera.
7. Install from Chrome on Android or Safari Share → Add to Home Screen on iPhone.

Phase 6E will connect Stripe webhooks to these entitlement records and activate
real encrypted Web Push delivery using private VAPID keys.
