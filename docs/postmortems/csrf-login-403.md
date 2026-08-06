# Postmortem: Production Login CSRF 403

Date: 2026-08-06

Status: Resolved

Affected component: Shared browser fetch wrapper and authentication forms

## Summary

Production login requests returned HTTP 403 with `CSRF validation failed.` The CSRF middleware behaved correctly: it rejected the request because `X-CSRF-Token` was absent. The browser had a valid signed `anyaicam_csrf` cookie, but the client wrapper did not copy it into the request header.

The failure was caused by the wrapper's same-origin test. Authentication submitted with `fetch(form.action, ...)`. Although the form declared `action="/login"`, the DOM `form.action` property resolved it to the absolute URL `https://app.anyaicam.com/login`. The wrapper classified every string beginning with `http://` or `https://` as cross-origin, including absolute URLs on the current origin. It therefore skipped the CSRF-header block.

## Impact

Users could load the production login page but could not authenticate. Each login submission reached the server without `X-CSRF-Token` and was rejected before credential validation. Existing authentication rules, customer data, sessions, Cloudflare routing, and server-side token validation were not compromised.

## Detection and diagnosis

Chrome DevTools showed that the login form submitted through `fetch()` and received HTTP 403. Inspection confirmed that the fetch wrapper, form `preventDefault()` handler, CSRF header assignment code, and quoted-cookie normalization were present in the deployed HTML.

Temporary redacted server diagnostics then recorded the first failed condition without exposing token values. The request contained a valid cookie, `unsign(cookie)` succeeded, and the current signing secret matched the cookie signature. The header was absent, its normalized length was zero, and `compare_digest()` was false. Expected Host, Origin, forwarded-protocol, and Cloudflare request indicators were present.

Reviewing the exact deployed JavaScript connected those observations: `form.action` supplied an absolute same-origin string, while the prefix-based check treated it as cross-origin. This explained why the header-assignment code existed but never executed.

## Resolution

The wrapper now resolves both relative and absolute request targets with `new URL(...)` and compares origins:

```javascript
const requestUrl = typeof input === "string"
  ? new URL(input, window.location.href)
  : new URL(input.url);
const sameOrigin = requestUrl.origin === window.location.origin;
```

The wrapper continues to attach `X-CSRF-Token` only to same-origin `POST`, `PUT`, `PATCH`, and `DELETE` requests. Cookie decoding and surrounding-quote removal remain in place. Redirect behavior remains unchanged. No server-side CSRF rule was relaxed.

Production verification confirmed that the cookie and header were both present, `compare_digest()` succeeded, CSRF validation passed, authentication completed, and the administrator portal was accessible. Temporary diagnostics were removed afterward.

## Regression prevention

Automated coverage now checks all relevant URL forms:

- `/login` — root-relative URL.
- `login` — path-relative URL.
- `https://app.anyaicam.com/login` — absolute same-origin URL.
- `https://example.invalid/login` — absolute cross-origin control that must not receive the header.

Separate assertions preserve quoted-cookie normalization, fetch-based authentication submission, and successful redirect handling. The tests also verify that strict server-side comparison and signature validation remain present.

## Lessons learned

- URL security decisions should compare parsed origins instead of relying on string prefixes.
- Browser DOM properties can normalize relative HTML attributes into absolute URLs; tests must cover both forms.
- Security fixes and their regression tests should be committed together and promoted through a dedicated feature branch.
- Diagnosis should identify the first failed condition before changing security logic.
- Temporary diagnostics should be narrowly scoped, redact secrets, and be removed immediately after verification.
