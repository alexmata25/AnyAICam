from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = ROOT / "app" / "main.py"
SECURITY_SOURCE = ROOT / "app" / "cloud_security.py"


class CsrfClientNormalizationTests(unittest.TestCase):
    def test_fetch_wrapper_strips_cookie_quotes_before_setting_header(self):
        source = MAIN_SOURCE.read_text(encoding="utf-8")

        self.assertIn(
            "token=token.replace(/^\"(.*)\"$/,'$1')",
            source,
        )
        self.assertIn("'X-CSRF-Token':token", source)
        self.assertNotIn(
            "'X-CSRF-Token':decodeURIComponent(csrf.split('=').slice(1).join('='))",
            source,
        )

    def test_auth_forms_submit_with_fetch_and_preserve_redirects(self):
        source = MAIN_SOURCE.read_text(encoding="utf-8")

        self.assertIn("def auth_form_script()", source)
        self.assertIn("new URL(input,window.location.href)", source)
        self.assertIn("requestUrl.origin===window.location.origin", source)
        self.assertNotIn("!input.startsWith('http://')", source)
        self.assertIn("new FormData(form)", source)
        self.assertIn("new URLSearchParams()", source)
        self.assertIn("if(response.redirected){location.assign(response.url);return}", source)
        self.assertEqual(source.count("{auth_form_script()}</body></html>"), 3)

    def test_server_validation_remains_strict(self):
        source = SECURITY_SOURCE.read_text(encoding="utf-8")

        self.assertIn("hmac.compare_digest(cookie,header)", source)
        self.assertIn("unsign(cookie)!='csrf'", source)


if __name__ == "__main__":
    unittest.main()
