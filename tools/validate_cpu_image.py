"""Network-local smoke test for an isolated CPU-only VMS container."""

import http.cookiejar
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000"
sys.path.insert(0, "/app")
jar = http.cookiejar.CookieJar()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect())


def request(path, data=None, headers=None):
    req = urllib.request.Request(BASE + path, data=data, headers=headers or {})
    try:
        return opener.open(req, timeout=10)
    except urllib.error.HTTPError as error:
        return error


form = request("/customer-register")
html = form.read().decode()
assert re.search(r'name="csrf_token"', html)
token = next(cookie.value for cookie in jar if cookie.name == "anyaicam_csrf")
print(f"REGISTRATION_GET={form.status}")

missing = request(
    "/customer-register",
    urllib.parse.urlencode(
        {
            "display_name": "Missing CSRF",
            "email": "missing-csrf@cpu-only.test",
            "password": "Synthetic-Password-2026",
        }
    ).encode(),
)
print(f"MISSING_CSRF={missing.status}")
assert missing.status == 403

invalid = request(
    "/customer-register",
    urllib.parse.urlencode(
        {
            "display_name": "Invalid CSRF",
            "email": "invalid-csrf@cpu-only.test",
            "password": "Synthetic-Password-2026",
            "csrf_token": "invalid",
        }
    ).encode(),
)
print(f"INVALID_CSRF={invalid.status}")
assert invalid.status == 403

registration = request(
    "/customer-register",
    urllib.parse.urlencode(
        {
            "display_name": "CPU Image Validation",
            "email": "customer@cpu-only.test",
            "password": "Synthetic-Password-2026",
            "csrf_token": token,
        }
    ).encode(),
)
confirmation = registration.read().decode()
print(f"VALID_REGISTRATION={registration.status}")
assert "request was submitted" in confirmation

from partner_db import authenticate_detailed

pending_user, pending_reason = authenticate_detailed(
    "customer@cpu-only.test", "Synthetic-Password-2026"
)
assert pending_user is None
print(f"PENDING_LOGIN_BLOCKED={pending_reason == 'invalid'}")

login = request(
    "/api/portal-login",
    json.dumps(
        {
            "email": "admin@cpu-only.test",
            "password": "CpuOnlyValidation-2026",
            "portal": "administrator",
        }
    ).encode(),
    {"Content-Type": "application/json", "X-CSRF-Token": token},
)
print(f"ADMIN_LOGIN={login.status}")
assert login.status == 303

db = sqlite3.connect("/app/recordings/partner_portal.db")
request_id = db.execute(
    "SELECT id FROM customer_registration_requests WHERE email=?",
    ("customer@cpu-only.test",),
).fetchone()[0]
db.close()

approval = request(
    f"/api/customer-registration-requests/{request_id}/approve",
    json.dumps({"partner_id": "anyaicam-primary"}).encode(),
    {"Content-Type": "application/json", "X-CSRF-Token": token},
)
approval_body = approval.read().decode()
print(f"APPROVAL={approval.status}")
assert approval.status == 200, approval_body

user, reason = authenticate_detailed("customer@cpu-only.test", "Synthetic-Password-2026")
assert reason == "ok" and user
db = sqlite3.connect("/app/recordings/partner_portal.db")
grant_count = db.execute(
    "SELECT count(*) FROM identity_grants "
    "WHERE user_id=? AND role='customer_owner' "
    "AND scope_type='customer' AND revoked_at IS NULL",
    (user["id"],),
).fetchone()[0]
db.close()
print(f"APPROVED_LOGIN={reason == 'ok'}")
print(f"CUSTOMER_GRANT_COUNT={grant_count}")
assert grant_count == 1

customer_login = request(
    "/api/partner-login",
    json.dumps(
        {
            "email": "customer@cpu-only.test",
            "password": "Synthetic-Password-2026",
            "customer_only": True,
        }
    ).encode(),
    {"Content-Type": "application/json", "X-CSRF-Token": token},
)
print(f"CUSTOMER_HTTP_LOGIN={customer_login.status}")
assert customer_login.status == 303
