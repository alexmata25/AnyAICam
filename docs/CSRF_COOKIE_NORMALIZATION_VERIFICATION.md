# CSRF Cookie Normalization Verification

Branch: `feature/csrf-cookie-normalization`

Base: `aws-production-20260806` (`4a52140`, tag `v0.9.0`)

Date: 2026-08-06

## Root cause

The quote-stripping normalization and fetch-based authentication form helper existed only as uncommitted changes in the local `build/v1.2-modular-foundation` working tree. Searches across every official GitHub branch and tag found no commit containing either `auth_form_script()` or the quote-stripping expression. The production branch and v0.9.0 tag were therefore created from the older implementation.

The live `/login` page was also inspected before deployment. It contained a normal POST form and no inline JavaScript, confirming that the production image did not contain the intended fetch-based authentication helper.

## Change

- Authentication forms submit with `fetch()` using `FormData` and `URLSearchParams`.
- Successful HTTP redirects are followed with `location.assign(response.url)`.
- The `anyaicam_csrf` cookie value is decoded and surrounding double quotes are stripped before setting `X-CSRF-Token`.
- The shared authenticated-page fetch wrapper applies the same normalization.
- `app/cloud_security.py` is unchanged and continues strict cookie/header comparison and signature validation.

## Focused verification

- Python compilation: passed.
- Three CSRF regression tests: passed.
- JavaScript browser-wrapper simulation: passed with `browser-header=signed-token`.
- Git whitespace validation: passed.
- Middleware diff: empty.

## Baseline-suite note

The existing v0.9.0 test suite has pre-existing failures unrelated to this patch: missing local test database tables and stale source-string assertions. The focused CSRF tests pass and no baseline production module beyond `app/main.py` is changed.

## Deployment verification

Pending access to the production Docker host. The current development machine has no Docker CLI, AWS CLI credentials, configured AWS region, or SSH host/key configuration. No rebuild, restart, or production mutation has been claimed in this report.
