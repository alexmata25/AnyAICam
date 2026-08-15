import os
import tempfile
import unittest
from pathlib import Path

TEST_DB=Path(tempfile.gettempdir())/'anyaicam-cloud-readiness-test.db'
TEST_DB.unlink(missing_ok=True)
os.environ['ANYAICAM_DATABASE_BACKEND']='sqlite'
os.environ['ANYAICAM_PARTNER_DB']=str(TEST_DB)

from token_security import sign,unsign
from database_backend import _postgres_sql
from email_service import PreviewEmail
from object_storage import LocalStorage,safe_key
import partner_db


class CloudReadinessTests(unittest.TestCase):
    def test_cloud_migrations_apply_to_local_database(self):
        # RDM-2 Group 2D: initialize_database() (base tables + migrations)
        # only runs once, as a side effect of partner_db's own first
        # import within this process -- whichever test module happens to
        # import it first "wins" that one-time run for whatever
        # ANYAICAM_PARTNER_DB path was current at that moment, which may
        # not be THIS file's own TEST_DB if another test module imported
        # partner_db earlier in the same test run. apply_migrations() is
        # explicitly re-invoked here (idempotent -- schema_migrations-
        # gated) so this assertion verifies migrations actually apply to
        # ITS OWN local database, not whichever database happened to be
        # current when partner_db was first imported.
        from db_migrations import apply_migrations
        apply_migrations()
        tables={item['name'] for item in partner_db.rows("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'schema_migrations','account_lockouts','password_reset_tokens','data_retention_policies','storage_objects','email_messages','appliance_update_history'}<=tables)

    def test_local_storage_round_trip_and_path_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            storage=LocalStorage(folder); result=storage.put('snapshots','customer/camera.jpg',b'image','image/jpeg'); self.assertEqual(storage.get('snapshots','customer/camera.jpg'),b'image'); self.assertEqual(result['backend'],'local')
            with self.assertRaises(ValueError): storage.put('snapshots','../escape',b'bad')

    def test_preview_email_writes_local_record(self):
        with tempfile.TemporaryDirectory() as folder:
            message=PreviewEmail(folder).send('invitation','test@example.invalid','Welcome','Preview only'); self.assertEqual(message['status'],'preview'); self.assertTrue((Path(folder)/(message['id']+'.json')).exists())

    def test_signed_values_validate_and_detect_tampering(self):
        token=sign('csrf',60); self.assertEqual(unsign(token),'csrf'); self.assertIsNone(unsign(token[:-2]+'xx'))

    def test_postgresql_translation(self):
        self.assertEqual(_postgres_sql('SELECT * FROM users WHERE email=?'),'SELECT * FROM users WHERE email=%s')
        translated=_postgres_sql('INSERT OR IGNORE INTO events(id) VALUES(?)'); self.assertIn('ON CONFLICT DO NOTHING',translated); self.assertNotIn('OR IGNORE',translated)

    def test_rdm2_update_history_insert_translates_for_postgresql(self):
        # RDM-2 Group 2D: appliance_update_history's own INSERT statement --
        # no INSERT OR IGNORE / AUTOINCREMENT special forms are used (see
        # db_migrations.py), so this only needs the plain ?->%s rewrite.
        translated=_postgres_sql('INSERT INTO appliance_update_history(appliance_id,update_id,from_version,to_version,state,error,rollback_from,duration_seconds,created_at) VALUES(?,?,?,?,?,?,?,?,?)')
        self.assertNotIn('?',translated); self.assertEqual(translated.count('%s'),9)


if __name__=='__main__': unittest.main()
