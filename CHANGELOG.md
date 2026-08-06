# Changelog

All notable changes to AnyAiCam are documented here.

## [0.9.1] - 2026-08-06

### Fixed

- Authentication forms now submit through the shared `fetch()` wrapper so protected state-changing requests include the CSRF header.
- Quoted `anyaicam_csrf` cookie values are decoded and stripped of surrounding double quotes before being sent as `X-CSRF-Token`.
- Absolute URLs whose origin matches the current page are now recognized as same-origin. Previously, the wrapper treated every absolute HTTP or HTTPS URL as cross-origin and omitted the CSRF header from `POST /login`.

### Security

- Strict server-side CSRF validation remains unchanged: the cookie and header must both exist, compare successfully, and contain a valid signed `csrf` value.
- CSRF tokens remain limited to same-origin `POST`, `PUT`, `PATCH`, and `DELETE` requests.
- Temporary server-side CSRF diagnostics were removed after verification and never logged complete token or secret values.

### Tests

- Added regression coverage for `/login`, `login`, and `https://app.anyaicam.com/login`.
- Added a cross-origin control proving that `https://example.invalid/login` does not receive `X-CSRF-Token`.
- Preserved coverage for quoted-cookie normalization and login redirect handling.

## [0.9.0] - 2026-08-06

- Established the stable AWS production baseline.
- Enabled the Cloudflare Tunnel deployment path.
- Completed Docker, authentication, CSRF, build-context optimization, and Edge Foundation work.
