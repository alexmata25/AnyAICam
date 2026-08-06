import os
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path

TEST_DB=Path(tempfile.gettempdir())/'anyaicam-partner-website-test.db'
TEST_DB.unlink(missing_ok=True)
os.environ['ANYAICAM_DATABASE_BACKEND']='sqlite'
os.environ['ANYAICAM_PARTNER_DB']=str(TEST_DB)

import partner_db

PARTNER_ROLES={'administrator','partner_owner','salesperson','technician'}


class PartnerWebsiteTests(unittest.TestCase):
    def setUp(self):
        now=datetime.now().isoformat()
        with partner_db.connection() as db:
            for table in ('partner_terms_acceptances','invitations','partner_users','partner_applications','partners'):
                db.execute(f'DELETE FROM {table}')
            db.execute('INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)',('approved-partner','Approved','approved','real',now))

    def add_user(self,email,role='partner_owner',approved=1,status='active',must_change=0):
        with partner_db.connection() as db:
            db.execute('''INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at,account_status,must_change_password)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',(email,'approved-partner',email,'Test User',role,partner_db.password_hash('CorrectPassword123!'),approved,datetime.now().isoformat(),status,must_change))

    def test_role_destinations_and_customer_exclusion(self):
        for role in PARTNER_ROLES: self.assertIn(role,partner_db.ROLE_PERMISSIONS)
        self.assertNotIn('customer_owner',PARTNER_ROLES)

    def test_detailed_auth_reports_account_state(self):
        self.add_user('pending@example.test',approved=0); self.assertEqual(partner_db.authenticate_detailed('pending@example.test','CorrectPassword123!')[1],'pending')
        self.add_user('suspended@example.test',status='suspended'); self.assertEqual(partner_db.authenticate_detailed('suspended@example.test','CorrectPassword123!')[1],'suspended')
        self.add_user('revoked@example.test',status='revoked'); self.assertEqual(partner_db.authenticate_detailed('revoked@example.test','CorrectPassword123!')[1],'revoked')
        self.assertEqual(partner_db.authenticate_detailed('missing@example.test','wrong')[1],'invalid')

    def test_expired_invitation_cannot_activate(self):
        self.add_user('invited@example.test',must_change=1)
        with partner_db.connection() as db:
            db.execute('INSERT INTO invitations(id,email,role,status,temporary_password_hash,expires_at,created_at) VALUES(?,?,?,?,?,?,?)',('invite','invited@example.test','partner_owner','pending','unused',(datetime.now()-timedelta(minutes=1)).isoformat(),datetime.now().isoformat()))
        self.assertEqual(partner_db.authenticate_detailed('invited@example.test','CorrectPassword123!')[1],'invitation_expired')

    def test_partner_application_tables_are_migrated(self):
        tables={item['name'] for item in partner_db.rows("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'partner_applications','partner_terms_acceptances'}<=tables)
        columns={item['name'] for item in partner_db.rows('PRAGMA table_info(partner_users)')}
        self.assertTrue({'account_status','must_change_password','terms_accepted_at'}<=columns)


if __name__=='__main__': unittest.main()
