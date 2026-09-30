# Shared VMS push notifications — implementation and release boundary

Date: 2026-09-29. Code-only work based on `e3ee24e` on the authoritative
`reconcile/golden-foundation-20260911` branch. Review branch:
`feat/shared-vms-mobile-push`. No deployment or Firebase modification performed.

## What was already present

- `notification_engine.fanout_appliance_event` creates tenant/user-scoped
  notifications and applies camera permissions, email/SMS preferences, quiet
  hours and cooldowns. Intrusion alarms already bypass normal suppression.
- Email media waiting and `skipped_voice_call` duplicate-email suppression,
  Twilio SMS delivery, and the email/SMS retry worker existed.
- `notification_service.WebPushPreparation` was a placeholder, not a sender.
- `main.py` has a separate legacy Web Push path using browser `PushManager`,
  `pywebpush`, and `ANYAICAM_VAPID_PUBLIC_KEY`, `ANYAICAM_VAPID_PRIVATE_KEY`,
  `ANYAICAM_VAPID_SUBJECT`. Its subscription store is not the shared customer
  notification engine. `mobile_notifications.py` separately stores JSON
  enrollments but does not deliver native mobile push.
- No Android/iOS application source was found in this repository.

## Implemented shared service

`mobile_push.py` is connected to the shared VMS notification transaction, not
Secure Edge. Each eligible recipient/device gets a durable outbox row. The
existing notification ID is the client action/deduplication identifier.

Supported events include intrusion alarms, Visitor Calls, person/vehicle,
analytics, LPR, camera/appliance offline, recording/storage/system health and
other existing supported VMS event types. The existing `system_health`
preference is now accepted by fanout. Missing selectable analytic/system event
labels were added to the existing preferences catalog; no saved selections,
email/SMS toggles, cooldown values or licensing were changed.

- Enrollment is explicit per device; no devices are enabled by default.
- Uses existing event selection, camera scope, current camera permissions and
  quiet hours, independently of email/SMS enablement.
- Intrusion alarms bypass event selection, quiet hours and normal cooldowns,
  but never bypass account/camera authorization or device revocation.
- Visitor Calls use their existing dedicated cooldown interval and a 120-second
  push lifetime. Intrusion alarms expire after 300 seconds; routine alerts after
  3600 seconds. Expiry prevents late stale notifications after outages.
- Push cooldown history is independent of email/SMS history. Email media waiting,
  `skipped_voice_call`, SMS preferences and their retry paths are unchanged.
- Duplicate upstream event IDs deduplicate per device, including intrusion alarms.
- Device token refresh updates the same installation; another account cannot
  silently take over an existing token. Logout/account-switch clients must
  unregister the installation before switching accounts. Portal users can
  disconnect devices in Settings → Notifications.
- Permissions, preferences and device enablement are rechecked before sending.
- Atomic leases protect against concurrent workers; expired leases recover after
  a crash. Successes on one device are never retried because another device failed.
- Transient failures retry with exponential backoff, at most five attempts.
  Unconfigured/authentication failures are recorded as unavailable and reconsidered
  until expiry. Only an FCM Unregistered error disables a token; an in-flight
  failure cannot disable a newly refreshed token.
- Separate urgent/routine loops prevent a routine request from occupying the alarm
  delivery lane. Workers start only for cloud/combined runtime roles.
- Provider acceptance is `sent`, not proof that the handset displayed anything.
  FCM/APNs cannot provide an exactly-once handset guarantee. A crash after provider
  acceptance can repeat a send; clients must deduplicate stable notification IDs.
  Browser notification tags collapse repeats.

The outbox is separate from email/SMS delivery rows deliberately, so push cannot
change their cooldown accounting. Read its scoped delivery history at
`GET /api/mobile/push/deliveries` (latest 100, no device tokens). Terminal rows
remain an audit trail; ordinary database backup/retention operations must include
these tables. This change does not run a data-retention job.

## Firebase architecture and configuration

The server uses Firebase Admin SDK 7.7.0 / FCM HTTP v1. Android uses FCM; native
iOS uses FCM with APNs configured in Firebase. The portal obtains FCM registration
tokens using Firebase Web Messaging and a public VAPID key. Legacy FCM is not used.
The public browser SDK is pinned to 12.0.0 on Google's gstatic CDN.

The owner reports project **Any AI Cam**, ID **any-ai-cam**, FCM HTTP v1 enabled.
The supplied screenshot confirms a Web Push key pair exists and sender ID
`208354453610`. These are deployment facts, not hardcoded product defaults.
The later screenshot also supplies the registered web app configuration. Firebase
Analytics initialization, measurement ID and Storage bucket settings are not needed
for push. No keys were copied from screenshots into code or environment files.

| Setting | Value/source | Storage |
| --- | --- | --- |
| `ANYAICAM_MOBILE_PUSH_BACKEND` | `fcm` to opt in; unset disables the provider | Cloud runtime configuration |
| `ANYAICAM_FCM_PROJECT_ID` | Deployment's Firebase project ID (`any-ai-cam` for the reported project) | Cloud runtime configuration |
| `ANYAICAM_FIREBASE_WEB_CONFIG_JSON` | JSON with actual `apiKey`, `appId`, `messagingSenderId`, `projectId`; optional `authDomain`, from Firebase General → Your apps → web app config | Public runtime configuration exposed through an explicit allowlist; never put service-account JSON here |
| `ANYAICAM_FIREBASE_WEB_VAPID_PUBLIC_KEY` | Public Web Push key copied from Firebase | Public runtime configuration |
| Application Default Credentials | Identity with permission `cloudmessaging.messages.create` on the target Firebase project | Prefer workload identity/federation; otherwise the secret mount described below |
| `GOOGLE_APPLICATION_CREDENTIALS` (when needed) | Absolute path to an ADC/workload-federation config or service-account credential JSON | Environment contains the path only; private credential file is read-only, outside Git/images/public files, obtained from the deployment secret manager |

The browser Firebase API key is a public application identifier, not the server's
sending credential. Apply appropriate Firebase/Google API restrictions during a
separately authorized configuration task. Do not request private keys in chat.

For Firebase-managed Web Push, retain the VAPID **private key in Firebase**.
Do not set the legacy `ANYAICAM_VAPID_PRIVATE_KEY` just to activate this new path.
Native iOS additionally needs the correct Apple app/bundle registration and APNs
signing key, Key ID and Team ID configured in Firebase. Those signing credentials
belong in Firebase's secured Apple app configuration and a controlled secret
backup, never browser JavaScript. Browser Web Push does not require a native iOS
APNs key to be copied to the AnyAiCam server.

Optional server dependency: `requirements-push.txt`. Its installation is a future
release step, not performed on any server in this task. Absent dependency or
credentials results in `unavailable`; no fabricated successful delivery.

`/api/mobile/push/config` exposes only an allowlisted public config. Config presence
is not a credential or delivery test. No credential file or private VAPID key is
exposed. CSP permits only the required gstatic SDK version path and Firebase
installation/registration endpoints on Notifications and the push worker, not
through a wildcard policy on the whole portal.

## Browser and native client contract

- `PUT /api/mobile/push/devices`: authenticated customer session plus existing CSRF
  protection. Body: `installation_id` (stable random identifier), `platform`
  (`web`, `android`, `ios`), FCM `token`, optional `enabled` (default true).
- `GET /api/mobile/push/devices`: only the caller's devices, with no tokens.
  The current database role must still be a customer role, not merely a stale
  customer role in a session cookie.
- `DELETE /api/mobile/push/devices/{id}`: only the caller's device; cancels queued
  work and unregisters it. Refresh tokens with PUT using the same installation ID.
- `GET /api/mobile/push/notifications/{id}`: caller-owned notification only;
  current camera visibility is checked and a same-origin authenticated destination
  is resolved. Visitor Calls link to their call; alarms link to live view; activity
  uses existing event/playback linking.
- Payloads contain an opaque notification ID and event type, not a camera image,
  plate, visitor transcript, user name, credential or bearer link. Alarm/call titles
  are distinct from normal activity. Opening content always requires portal access.
- Settings → Notifications includes browser opt-in and per-device disconnect.
  The scoped worker `/mobile-push-sw.js` uses scope `/mobile-push/`; it does not
  replace the portal's root offline/service worker. It renders push in both
  foreground and background and rejects cross-origin click destinations.
- Only the exact worker/config URLs bypass global login middleware. Device and
  delivery APIs do not. The service-worker activation check handles a worker that
  activates before its state-change listener is attached. Phone access recognizes
  FCM configuration without requiring the legacy private VAPID key.
- Native Android clients must create `anyaicam_intrusion`, `anyaicam_visitor_call`
  and `anyaicam_activity` notification channels with appropriate user-consented
  importance. Native clients refresh tokens and unregister before account switching.
- Existing browser tokens refresh on revisiting Notifications if the installation
  is still enrolled; a revoked device is not silently re-enabled.
- This does not convert legacy subscriptions automatically or redirect legacy
  appliance alerts into customer accounts. Never send a new event through both
  transports for one installation during a later migration.
- Push is immediate, independent of the existing summary setting; no daily push
  summary engine is represented as implemented.

## Verification and remaining gates

Tests use isolated local databases and fake provider sends. Real SDK 7.7.0 payload
serialization was separately verified for alarms, Visitor Calls, person, LPR and
camera alerts with the network send mocked and no credentials loaded.

Local headless Chromium smoke checks at 1440×900 and 390×844 verified honest
unconfigured state, explicit enrollment, token refresh, disconnect and no silent
re-enrollment. All browser SDK and network responses were mocked; these are not
real FCM/browser delivery validations.

Automated coverage includes all shared event types; selected/no/enrolled devices;
quiet hours; emergency exceptions; duplicate events; independent email/SMS
cooldowns; multiple-device partial failure; expiry; retries/leases; account/camera
revocation; in-flight token refresh; cross-tenant registration/revocation; scoped
notification opening; public-config allowlisting; Node syntax/click/display tests;
and existing email/SMS/preference/generated-page regressions. Exact results and failure IDs are saved in `shared-vms-push-test-results.json`.
The suite is not fully green; no newly failing test IDs were observed.

Still required before a release:

1. Secure cloud sender identity and deployment-time installation of the supplied
   public Firebase web app configuration. No private credentials were inspected
   or configured during this work.
2. A separately authorized deployment. No Ryzen, Samsung, EC2, staging, production,
   networking, cameras, appliance settings, pricing, Stripe or website files changed.
3. Browser testing on actual supported desktop/Android/iOS Home Screen environments:
   opt-in, background/foreground/closed app, click-to-event, token refresh, logout,
   revoked access, offline expiry and one displayed alert per event.
4. Native apps must integrate this token/permission/lifecycle contract; this repo
   does not contain those native apps. Native iOS requires its APNs setup.
5. Real Visitor Call and intrusion event delivery/latency tests after permission to
   deploy; no physical camera walk-up or mobile delivery has been claimed.
6. APNs `priority=10` / Android high priority does not imply an Apple Critical Alerts
   entitlement, override of Focus/Do Not Disturb, or a guaranteed response time.
   The server never promises those OS behaviors.
7. PostgreSQL compatibility follows the existing SQL adapter; PostgreSQL runtime
   validation is pending unless separately reported. SQLite migration is exercised
   by every focused test. Provider/network timeouts and failure classes are tested
   using mocks; no real FCM outage or acceptance test was run.

References: [Firebase Web setup](https://firebase.google.com/docs/cloud-messaging/web/get-started),
[Admin SDK setup](https://firebase.google.com/docs/admin/setup),
[HTTP v1 message fields](https://firebase.google.com/docs/reference/fcm/rest/v1/projects.messages),
[FCM error codes](https://firebase.google.com/docs/cloud-messaging/error-codes).

## Rollback boundary

The schema is additive. Reverting the code in a separately authorized future release
can leave the new tables intact for review; no destructive database rollback is
needed. Existing email/SMS delivery history and preferences have not been migrated
or rewritten. These instructions describe a future release; no rollback or
configuration action was performed on a running system.

## Final code/test handoff

- `3b9f6e8bf9c0b19144656890ca283a54bfe14d8a`: shared FCM service, schema/outbox,
  provider boundary, portal enrollment, CSP, and initial regressions.
- `e5c468d7a00bfe1e8b9ba00be3a430201c1c9d7b`: worker public-route integration,
  activation race, current-role enforcement, phone status and media-aware clip links.
- Final focused run on the follow-up: **164 passed**. Includes real Firebase Admin
  7.7.0 serialization with sending mocked, real application session/CSRF routing,
  notification/email/SMS/preferences, phone access, generated JS and deep links.
- Full application suite on the core implementation: **4,887 passed, 87 failed,
  128 skipped** (24m45s). Baseline: **4,819 passed, 101 failed, 127 skipped**
  (24m27s). All 87 failure IDs also failed in the baseline; no new failure IDs.
  The 14 baseline-only failures did not recur; no claim of fixing them is made.
  The later follow-up was validated by the focused suite rather than repeating
  the whole full suite. The full-suite optional SDK skip was exercised successfully
  in the separate focused environment.
- A broader auth-routing check also reproduced the existing
  `test_mobile_app_is_already_public_so_this_fix_deliberately_excludes_it` failure;
  it is in both full baseline and full implementation failure sets.
- Local environment: Windows / Python 3.14. Public Firebase SDK 12.0.0 CDN assets
  returned HTTP 200 to read-only HEAD checks. No Firebase project requests made.
- No source changes outside the VMS notification/integration tests and docs;
  no website, prices, Stripe, licensing, camera or appliance configuration changes.

Recommended next step: review the two code commits and this handoff, then separately
authorize secure sender-identity configuration and a controlled cloud-only test
release. Real browser/native-device validation follows that authorization. No
Ryzen/Samsung release or other deployment is authorized by this completed task.
