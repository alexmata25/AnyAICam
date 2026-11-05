"""Synthetic disposable SQLite race; does not run migration source."""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path


class ClaimInvariantTests(unittest.TestCase):
    def test_simultaneous_current_claims_allow_only_one(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'claims.db')
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE claims(id TEXT PRIMARY KEY, appliance_id TEXT, status TEXT, revoked_at TEXT)')
            db.execute("CREATE UNIQUE INDEX one_current ON claims(appliance_id) WHERE appliance_id IS NOT NULL AND status='completed' AND revoked_at IS NULL")
            db.commit(); db.close()
            barrier = threading.Barrier(2); results = []; errors = []
            def create(claim_id):
                con = sqlite3.connect(path,timeout=5)
                try:
                    barrier.wait(timeout=5)
                    con.execute('INSERT INTO claims VALUES(?,?,?,NULL)',(claim_id,'appliance','completed'))
                    con.commit(); results.append('created')
                except sqlite3.IntegrityError:
                    con.rollback(); results.append('conflict')
                except Exception as error:
                    errors.append(str(error))
                finally: con.close()
            workers = [threading.Thread(target=create,args=(str(i),)) for i in range(2)]
            for thread in workers: thread.start()
            for thread in workers: thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in workers))
            self.assertEqual(errors,[])
            self.assertCountEqual(results,['created','conflict'])
            con = sqlite3.connect(path)
            try:
                con.execute("UPDATE claims SET revoked_at='now' WHERE status='completed'")
                con.execute("INSERT INTO claims VALUES('replacement','appliance','completed',NULL)")
                con.execute("INSERT INTO claims VALUES('historical','appliance','expired',NULL)")
                con.commit()
                self.assertEqual(con.execute('SELECT COUNT(*) FROM claims').fetchone()[0],3)
            finally: con.close()


if __name__ == '__main__': unittest.main()
