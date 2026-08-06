# Phase 6 Logout CSRF Verification

## Automated verification

The focused security suite passed 19 tests, plus the JavaScript same-origin wrapper regression.

The expanded logout, CSRF, identity-domain, customer-experience, onboarding, migration, and tenancy-policy set passed **32/32 tests**.

Covered cases:

- Successful logout with matching, valid CSRF token
- Logout without header rejected
- Logout without cookie rejected
- Mismatched token rejected
- Invalid signature rejected
- Logout destroys the active session and redirects to login
- Login, logout, login again rotates and destroys the correct session
- The CSRF cookie is retained across logout
- Login form behavior and quote normalization are unchanged
- No active native HTML form can submit directly to `/logout`
- Logout passes a relative string—not an unsupported JavaScript `URL` object—to the shared wrapper
- Redacted diagnostic logging does not contain token values

## Local limitation

Static compilation and focused unit regressions were completed. A container HTTP smoke test could not be run in this Windows workspace because Docker is not installed here.

## EC2 verification checklist

1. Build the feature branch image and restart only the application container.
2. Open a new Incognito window with DevTools cache disabled.
3. Log in successfully.
4. Click the sidebar Logout button.
5. Confirm `POST /logout` includes `X-CSRF-Token` and the session cookie.
6. Confirm HTTP 303 redirects to `/login` without a CSRF warning.
7. Log in again and repeat logout.
8. Send controlled missing/invalid-token requests and confirm HTTP 403.
9. Remove the temporary failed-logout warning after the successful production smoke test.
