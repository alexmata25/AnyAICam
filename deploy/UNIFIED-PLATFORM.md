# AnyAiCam unified web and mobile foundation

The public website, Partner Portal, Customer Portal, and future mobile app use the same account, customer, site, appliance, camera, event, alert, recording, subscription, and permission records.

## Experiences

- Public: `/`, `/pricing`, `/partner.html`, and `/customer-login.html`
- Partner Portal: `/partner`
- Customer Portal: `/customer-portal`
- Shared API: `/api/v1`

Partner and customer portals remain role-isolated. Customer API responses intentionally omit partner prices, wholesale costs, recurring-profit fields, commissions, and other customers.

## Authentication

Web browsers use the secure HTTP-only portal cookie. New browser logins create revocable device-session records with an eight-hour expiration. `/api/v1/auth/logout-all` revokes every recorded browser and API session for the signed-in user.

The future mobile app can request an opaque bearer token from `/api/v1/auth/token`. Only its SHA-256 digest is stored. Tokens are device-labeled, expiring, rate-limited, and use the same roles and permissions as the website. MFA schema is prepared but MFA enrollment is not enabled yet.

## Customer API

- `GET /api/v1/me`
- `GET /api/v1/customer`
- `GET /api/v1/sites`
- `GET /api/v1/cameras`
- `POST /api/v1/cameras/{camera_id}/live-session`
- `GET /api/v1/live-sessions/{session_id}`
- `POST /api/v1/cameras/{camera_id}/snapshots`
- `POST /api/v1/cameras/{camera_id}/clips`
- `GET /api/v1/clips/{job_id}`
- `GET /api/v1/recordings`
- `GET /api/v1/events`
- `GET /api/v1/alerts`
- `GET /api/v1/subscription`
- `GET/POST /api/v1/users`
- `PUT /api/v1/users/{user_id}/permissions`
- `POST /api/v1/recordings/{recording_id}/download`
- `POST /api/v1/recordings/{recording_id}/share`

Administrator customer access uses an explicit customer selection. Customer owners and viewers are always bound to their own customer ID. Optional site and camera permission rows further restrict viewers.

## Secure video boundary

The browser never receives camera usernames, passwords, RTSP URLs, or private camera IP addresses. Live authorization creates a persistent five-minute record after checking customer, site, camera, role, and user permissions. Its state is `requested`, `ready`, `failed`, or `expired`; the current transport is `not_configured` and no stream URL is returned.

Remote playback, snapshots, clip jobs, downloads, and public share URLs remain clearly labeled authorization placeholders until appliance-to-cloud video transport is implemented. Local appliance recording and offline operation are unchanged.

## PWA and mobile authentication

The Customer Portal registers `/service-worker.js` and `/manifest.webmanifest`. The service worker caches only the public navigation shell, offline page, login page, manifest, and static brand assets. It deliberately does not cache authenticated `/api/` responses or customer video data.

Mobile login uses `POST /api/v1/auth/token`. Access tokens expire after 15 minutes. Rotating refresh tokens expire after 30 days and are exchanged through `POST /api/v1/auth/refresh`. Reuse of an already-rotated refresh token revokes the complete token family, active API sessions for that device, and the device registration. Website cookie sessions remain unchanged.

Device and notification routes:

- `GET/POST /api/v1/devices`
- `DELETE /api/v1/devices/{device_id}`
- `POST /api/v1/auth/logout`
- `GET/PUT /api/v1/notification-preferences`
- `GET /api/v1/notifications`
- `POST /api/v1/notifications/{notification_id}/{action}`

In-app notifications are stored locally. Email uses the existing preview backend unless SMTP is explicitly enabled. Web push is preparation-only, and SMS is disabled by default.
