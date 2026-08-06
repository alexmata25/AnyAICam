# Logout CSRF Hardening

## Root cause

The active sidebar rendered logout as a normal HTML `POST /logout` form and separately installed a JavaScript submit interceptor. Under the normal path the interceptor called `window.fetch`, allowing the shared fetch wrapper to add `X-CSRF-Token`. If the interceptor was missing, stale, not yet installed, or interrupted by another script error, the browser retained a second path: direct form submission. Native form submission includes cookies but cannot add the custom CSRF header, so the unchanged middleware correctly returned HTTP 403.

The first hardening pass then passed a JavaScript `URL` object to `window.fetch`. The shared wrapper distinguished only string inputs from Request-like inputs and tried to read `.url` from that `URL` object. Because a `URL` object has no `.url` property, it called `new URL(undefined)` and the browser displayed `Failed to construct 'URL': Invalid URL` before sending `/logout`. Logout now passes the relative string `'/logout'`. The wrapper is also hardened to accept relative or absolute strings, `URL` objects, and Request-like objects.

## Hardened flow

```text
Logout button (type=button; no HTML form action)
  -> click handler
  -> window.fetch('/logout', POST)
  -> shared origin-based fetch wrapper
  -> quoted-cookie normalization
  -> X-CSRF-Token header
  -> strict CSRF middleware
  -> POST /logout
  -> session destroyed and session cookie deleted
  -> redirect to /login
```

There is no active anchor, navigation assignment, GET route, or native form submission targeting `/logout`.

## Validation behavior

The middleware still requires:

- The `anyaicam_csrf` cookie
- The `X-CSRF-Token` request header
- Constant-time equality between cookie and header
- A valid signed cookie whose payload is `csrf`

Missing, mismatched, and invalidly signed tokens remain HTTP 403 failures.

## Temporary diagnostics

Failed `POST /logout` validation records a redacted warning containing only:

- Failure category
- Cookie/header presence
- Whether lengths match
- Method and path
- Host
- Whether Origin and forwarded headers were present

Token values are never logged. Remove the warning after one successful EC2 login/logout/login smoke test confirms the production request carries the header.
