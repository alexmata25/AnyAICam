"""PostgreSQL-only (Green 2026-10-17): the held-alert-email scan and the email-delivery readiness lookup used SQLite's
rowid, which PostgreSQL does not have -- the scan failed on every retry-worker pass ("column later.rowid does not
exist"), so emails held for their thumbnail were never sent, and readiness quietly reported no delivery at all.
Requires ANYAICAM_TEST_POSTGRES_URL (a disposable database)."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

TEST_URL = os.environ.get("ANYAICAM_TEST_POSTGRES_URL", "")
if TEST_URL:
    os.environ["ANYAICAM_DATABASE_BACKEND"] = "postgresql"
    os.environ["ANYAICAM_DATABASE_URL"] = TEST_URL
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))

import psycopg
from partner_db import initialize_database


@unittest.skipUnless(TEST_URL, "set ANYAICAM_TEST_POSTGRES_URL to a disposable PostgreSQL database")
class PostgreSQLEmailInsertionOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        initialize_database()

    def setUp(self):
        with psycopg.connect(TEST_URL) as db:
            db.execute("DELETE FROM notification_deliveries"); db.execute("DELETE FROM email_messages")

    # The fixture rows have no parent notification; never leave them behind (a pg_dump/pg_restore of the database
    # would then fail re-adding the foreign key).
    tearDown = setUp

    def insert(self, sql, args):
        # Parent rows (notifications, customers) are irrelevant here; skip the foreign-key triggers for the fixture.
        with psycopg.connect(TEST_URL) as db:
            db.execute("SET session_replication_role = replica"); db.execute(sql, args)

    def delivery(self, delivery_id, created_at, status="pending_media", notification_id="n1"):
        self.insert("INSERT INTO notification_deliveries(id,notification_id,channel,status,provider,error,created_at) "
                    "VALUES(%s,%s,'email',%s,NULL,NULL,%s)", (delivery_id, notification_id, status, created_at))

    def test_held_email_scan_runs_and_only_takes_the_newest_row_per_notification(self):
        import notification_retry_worker
        self.delivery("old", "2026-10-17T10:00:00")
        self.delivery("new", "2026-10-17T10:05:00")
        self.delivery("other", "2026-10-17T10:01:00", notification_id="n2")
        seen = []
        with patch("notification_email.alert_context", side_effect=lambda db, nid: seen.append(nid) or None):
            result = notification_retry_worker.send_pending_media_emails()
        self.assertEqual(result["pending"], 2)
        self.assertEqual(sorted(seen), ["n1", "n2"])

    def test_held_email_is_superseded_by_a_newer_row_with_the_same_timestamp_by_id(self):
        import notification_retry_worker
        self.delivery("a", "2026-10-17T10:00:00")
        self.delivery("b", "2026-10-17T10:00:00", status="sent")
        with patch("notification_email.alert_context", return_value=None):
            self.assertEqual(notification_retry_worker.send_pending_media_emails()["pending"], 0)

    def test_last_email_delivery_reports_the_newest_rows(self):
        import main
        self.insert("INSERT INTO email_messages(id,message_type,recipient,status,provider,metadata_json,created_at) "
                    "VALUES('m1','invitation','a@example.test','sent','smtp','{}','2026-10-17T09:00:00')", ())
        self.insert("INSERT INTO email_messages(id,message_type,recipient,status,provider,metadata_json,created_at) "
                    "VALUES('m2','invitation','a@example.test','failed','smtp','{\"error\": \"535\"}','2026-10-17T11:00:00')", ())
        self.delivery("d1", "2026-10-17T10:00:00", status="sent")
        latest = main._last_email_delivery()
        self.assertIsNotNone(latest)
        self.assertEqual((latest["status"], latest["at"]), ("failed", "2026-10-17T11:00:00"))


if __name__ == "__main__":
    unittest.main()
