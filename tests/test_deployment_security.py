import importlib
import os
import sys
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parent
APP=ROOT/'app' if (ROOT/'app').exists() else (ROOT.parent/'app' if (ROOT.parent/'app').exists() else ROOT)
DEPLOY=ROOT/'deploy' if (ROOT/'deploy').exists() else (ROOT.parent/'deploy' if (ROOT.parent/'deploy').exists() else ROOT)
sys.path.insert(0,str(APP))


def deployment_file(name):
    candidate=ROOT/name
    return candidate if candidate.exists() else DEPLOY/name


PRODUCTION={
 'ANYAICAM_ENV':'production','ANYAICAM_APP_SECRETS':'a'*40,'ANYAICAM_DATABASE_BACKEND':'sqlite',
 'ANYAICAM_HTTPS_ONLY':'true','ANYAICAM_SECURE_COOKIES':'true','ANYAICAM_CSRF_ENABLED':'true',
 'ANYAICAM_PUBLIC_WEBSITE_URL':'https://anyaicam.com','ANYAICAM_PORTAL_URL':'https://portal.anyaicam.com',
 'ANYAICAM_API_BASE_URL':'https://portal.anyaicam.com/api/v1','ANYAICAM_PASSWORD_RESET_URL':'https://portal.anyaicam.com/reset-password',
 'ANYAICAM_INVITATION_URL':'https://portal.anyaicam.com/partner.html','ANYAICAM_APPLIANCE_ACTIVATION_URL':'https://portal.anyaicam.com/api/appliance/activate',
 'ANYAICAM_PRODUCTION_PARTNER_URL':'https://portal.anyaicam.com/partner.html','ANYAICAM_PRODUCTION_CUSTOMER_URL':'https://portal.anyaicam.com/customer-login.html',
 'ANYAICAM_ALLOWED_ORIGINS':'https://anyaicam.com,https://portal.anyaicam.com'
}


class DeploymentSecurityTests(unittest.TestCase):
    def settings_for(self,values):
        previous={key:os.environ.get(key) for key in values}
        os.environ.update(values)
        try:
            import cloud_config
            settings=importlib.reload(cloud_config).settings
            return settings
        finally:
            for key,value in previous.items():
                if value is None: os.environ.pop(key,None)
                else: os.environ[key]=value

    def test_production_urls_are_https_and_defaults_are_rejected(self):
        settings=self.settings_for(PRODUCTION); settings.validate()
        self.assertEqual(settings.public_website_url,'https://anyaicam.com')
        self.assertEqual(settings.api_base_url,'https://portal.anyaicam.com/api/v1')
        invalid=dict(PRODUCTION,ANYAICAM_PORTAL_URL='http://localhost:8000')
        with self.assertRaisesRegex(RuntimeError,'HTTPS'): self.settings_for(invalid).validate()
        weak=dict(PRODUCTION,ANYAICAM_APP_SECRETS='REPLACE_CURRENT_SECRET')
        with self.assertRaisesRegex(RuntimeError,'secret'): self.settings_for(weak).validate()

    def test_staging_is_separate_and_secure(self):
        text=deployment_file('.env.staging.example').read_text(encoding='utf-8')
        self.assertIn('https://staging.anyaicam.com',text)
        self.assertIn('https://portal-staging.anyaicam.com',text)
        self.assertIn('ANYAICAM_SECURE_COOKIES=true',text)
        self.assertNotIn('portal.anyaicam.com/partner.html',text)

    def test_safe_redirects_and_security_headers_exist(self):
        source=(APP/'cloud_security.py').read_text(encoding='utf-8')
        from redirect_security import safe_redirect
        self.assertEqual(safe_redirect('/customer-portal?site=home'),'/customer-portal?site=home')
        for unsafe in ('https://evil.example','//evil.example','javascript:alert(1)','\\evil.example'):
            self.assertEqual(safe_redirect(unsafe,'/customer-portal'),'/customer-portal')
        for header in ('Content-Security-Policy','X-Frame-Options','X-Content-Type-Options','Strict-Transport-Security'):
            self.assertIn(header,source)
        self.assertIn('settings.allowed_origins',source)
        self.assertIn("del response.headers['server']",source)

    def test_proxy_health_and_websocket_preparation(self):
        caddy=deployment_file('Caddyfile').read_text(encoding='utf-8')
        self.assertIn('reverse_proxy',caddy)
        self.assertIn('max_size 250MB',caddy)
        self.assertIn('health_uri /health/live',caddy)
        self.assertIn('WebSocket',caddy)
        routes=(APP/'cloud_features.py').read_text(encoding='utf-8')
        self.assertIn("'/health/live'",routes)
        self.assertIn("'/health/ready'",routes)

    def test_secure_cookie_and_no_query_session_redirect(self):
        portal=(APP/'partner_portal.py').read_text(encoding='utf-8')
        self.assertIn('secure=settings.secure_cookies',portal)
        self.assertNotIn('?token='+"_token(",portal)


if __name__=='__main__': unittest.main()
