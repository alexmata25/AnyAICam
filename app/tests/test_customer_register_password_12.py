"""/customer-register uses the same 12-character password minimum as self-service signup and password reset
(owner decision 2026-10-17; it accepted 10). The request form, its server check, and the email-confirmation form all
enforce 12; a 12-character password is accepted.
"""
import unittest

import customer_registration as cr
from test_customer_registration_csrf import CustomerRegistrationLifecycleTests
from token_security import sign

ELEVEN = "elevenchars"            # 11 characters
TWELVE = "twelve-chars"           # 12 characters


class CustomerRegisterTwelveCharacterTests(unittest.TestCase):
    def setUp(self):
        self.base = CustomerRegistrationLifecycleTests("test_get_sets_csrf_and_form_submits_token")
        self.base.setUp()
        self.client = self.base.client

    def tearDown(self):
        self.base.tearDown()

    def _post(self, password, email="new@example.test"):
        token = sign("csrf", 28800)
        self.client.cookies.set("anyaicam_csrf", token)
        return self.client.post("/customer-register", data={"display_name": "New Customer", "email": email,
                                                            "password": password, "csrf_token": token})

    def test_the_rule_is_twelve_like_signup_and_reset(self):
        import direct_onboarding
        self.assertEqual(len(ELEVEN), 11)
        self.assertEqual(len(TWELVE), 12)
        self.assertEqual(cr.MIN_PASSWORD, 12)
        self.assertEqual(cr.MIN_PASSWORD, direct_onboarding.MIN_PASSWORD_LENGTH)

    def test_the_request_form_says_twelve(self):
        page = self.client.get("/customer-register").text
        self.assertIn('minlength="12"', page)
        self.assertIn("At least 12 characters.", page)
        self.assertNotIn('minlength="10"', page)

    def test_an_eleven_character_password_is_refused_by_the_server(self):
        response = self._post(ELEVEN)
        self.assertEqual(response.status_code, 422)
        self.assertIn("at least 12 characters", response.text)
        self.assertEqual(self.base.rows("SELECT * FROM customer_registration_requests"), [])

    def test_a_twelve_character_password_is_accepted(self):
        response = self._post(TWELVE)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.base.rows("SELECT * FROM customer_registration_requests")), 1)

    def test_the_confirmation_form_enforces_twelve_without_using_up_the_link(self):
        cr.create_pending_registration("New Customer", "new@example.test", "correct-horse-battery-staple")
        raw = cr.issue_registration_verification("new@example.test", force=True)
        self.assertIn('minlength="12"', cr._verify_form(raw, ""))
        self.assertFalse(cr.verify_registration_email(raw, ELEVEN, "Confirmed Owner"))
        self.assertTrue(cr.verify_registration_email(raw, TWELVE, "Confirmed Owner"))


if __name__ == "__main__":
    unittest.main()
