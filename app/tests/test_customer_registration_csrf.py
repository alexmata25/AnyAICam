import dataclasses
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ANYAICAM_DATABASE_BACKEND", "sqlite")
os.environ["ANYAICAM_PARTNER_DB"] = str(Path(tempfile.gettempdir()) / "anyaicam-registration-csrf.db")
os.environ.setdefault("ANYAICAM_CSRF_ENABLED", "true")

from fastapi.testclient import TestClient

import cloud_security
import main
from token_security import sign


class CustomerRegistrationCsrfTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.users_file = root / "users.json"
        self.audit_file = root / "audit.jsonl"
        self.patches = [
            patch.object(main, "USERS_FILE", self.users_file),
            patch.object(main, "AUDIT_LOG_FILE", self.audit_file),
            patch.object(
                cloud_security,
                "settings",
                dataclasses.replace(
                    cloud_security.settings,
                    csrf_enabled=True,
                    secure_cookies=False,
                    allowed_origins=["http://testserver"],
                ),
            ),
        ]
        for item in self.patches:
            item.start()
        self.client = TestClient(main.app, base_url="http://testserver", follow_redirects=False)

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def _post(self, email="new.customer@example.test", token=None):
        if token is None:
            token = sign("csrf", 28800)
        self.client.cookies.set("anyaicam_csrf", token)
        return self.client.post(
            "/customer-register",
            data={
                "display_name": "New Customer",
                "email": email,
                "password": "correct-horse-battery-staple",
                "csrf_token": token,
            },
        )

    def test_get_sets_cookie_and_wires_existing_double_submit_model(self):
        response = self.client.get("/customer-register")
        self.assertEqual(response.status_code, 200)
        self.assertIn("anyaicam_csrf=", response.headers.get("set-cookie", ""))
        self.assertIn('id="customer-register-form"', response.text)
        self.assertIn('<input type="hidden" name="csrf_token" value="">', response.text)
        self.assertIn("this.csrf_token.value=decodeURIComponent(match[1])", response.text)

    def test_valid_registration_returns_branded_confirmation_not_json(self):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Create customer account", response.text)
        self.assertIn("Your customer account request was submitted", response.text)
        self.assertNotEqual(response.headers.get("content-type"), "application/json")

    def test_missing_csrf_is_rejected(self):
        response = self.client.post(
            "/customer-register",
            data={"display_name": "New Customer", "email": "missing@example.test", "password": "correct-horse-battery-staple"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "CSRF validation failed.")

    def test_invalid_csrf_is_rejected(self):
        cookie = sign("csrf", 28800)
        self.client.cookies.set("anyaicam_csrf", cookie)
        response = self.client.post(
            "/customer-register",
            data={"display_name": "New Customer", "email": "invalid@example.test", "password": "correct-horse-battery-staple", "csrf_token": "invalid"},
        )
        self.assertEqual(response.status_code, 403)

    def test_duplicate_email_returns_branded_conflict(self):
        main.save_users([{"id": "existing", "email": "duplicate@example.test", "enabled": True, "role": "customer_owner"}])
        response = self._post(email="duplicate@example.test")
        self.assertEqual(response.status_code, 409)
        self.assertIn("already registered", response.text)
        self.assertIn("Create customer account", response.text)

    def test_success_creates_pending_disabled_customer_account(self):
        response = self._post(email="pending@example.test")
        self.assertEqual(response.status_code, 200)
        users = json.loads(self.users_file.read_text(encoding="utf-8"))
        accounts = [item for item in users if item.get("email") == "pending@example.test"]
        self.assertEqual(len(accounts), 1)
        account = accounts[0]
        self.assertEqual(account["email"], "pending@example.test")
        self.assertEqual(account["role"], "customer_owner")
        self.assertFalse(account["enabled"])
        self.assertEqual(account["invitation_status"], "pending")
        self.assertNotEqual(account["password_hash"], "correct-horse-battery-staple")


if __name__ == "__main__":
    unittest.main()
