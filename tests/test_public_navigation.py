import importlib
import os
import sys
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parent
APP=ROOT/'app' if (ROOT/'app').exists() else (ROOT.parent/'app' if (ROOT.parent/'app').exists() else ROOT)
sys.path.insert(0,str(APP))


class PublicNavigationTests(unittest.TestCase):
    def load_settings(self,environment,**values):
        names=['ANYAICAM_ENV','ANYAICAM_DEVELOPMENT_PARTNER_URL','ANYAICAM_DEVELOPMENT_CUSTOMER_URL','ANYAICAM_STAGING_PARTNER_URL','ANYAICAM_STAGING_CUSTOMER_URL','ANYAICAM_PRODUCTION_PARTNER_URL','ANYAICAM_PRODUCTION_CUSTOMER_URL']
        previous={name:os.environ.get(name) for name in names}
        try:
            os.environ['ANYAICAM_ENV']=environment
            for name,value in values.items(): os.environ[name]=value
            import cloud_config
            return importlib.reload(cloud_config).settings
        finally:
            for name,value in previous.items():
                if value is None: os.environ.pop(name,None)
                else: os.environ[name]=value

    def test_development_uses_local_login_pages(self):
        settings=self.load_settings('development')
        self.assertEqual(settings.partner_login_url,'http://localhost:8000/partner.html')
        self.assertEqual(settings.customer_login_url,'http://localhost:8000/customer-login.html')

    def test_staging_and_production_use_selected_https_urls(self):
        staging=self.load_settings('staging',ANYAICAM_STAGING_PARTNER_URL='https://stage.example/partner.html',ANYAICAM_STAGING_CUSTOMER_URL='https://stage.example/customer-login.html')
        self.assertEqual(staging.partner_login_url,'https://stage.example/partner.html')
        production=self.load_settings('production',ANYAICAM_PRODUCTION_PARTNER_URL='https://portal.anyaicam.com/partner.html',ANYAICAM_PRODUCTION_CUSTOMER_URL='https://portal.anyaicam.com/customer-login.html')
        self.assertTrue(production.partner_login_url.startswith('https://'))
        self.assertTrue(production.customer_login_url.startswith('https://'))
        self.assertNotIn('localhost',production.partner_login_url+production.customer_login_url)

    def test_public_pages_have_both_login_paths_and_authenticated_labels(self):
        partner=(APP/'partner.html').read_text(encoding='utf-8')
        customer=(APP/'customer-login.html').read_text(encoding='utf-8')
        routes=(APP/'website_partner.py').read_text(encoding='utf-8')
        for page in (partner,customer):
            self.assertIn('Partner Login',page)
            self.assertIn('Customer Login',page)
            self.assertIn('/api/website/partner-session',page)
            self.assertNotIn('rtsp://',page.lower())
            self.assertNotIn('192.168.',page)
        for label in ('Administration','Partner Portal','My Cameras'):
            self.assertIn(label,routes)


if __name__=='__main__': unittest.main()
