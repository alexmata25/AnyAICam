"""Actual extracted claim-begin logic against disposable synthetic SQLite."""
import secrets
import sqlite3
import sys
import tempfile
import textwrap
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from app import gateway_schema_readiness as readiness


class HttpError(Exception):
    def __init__(self,status_code,detail): self.status_code,self.detail=status_code,detail


class ClaimSessionRaceTests(unittest.TestCase):
    def test_concurrent_begin_retries_and_stale_code_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'synthetic.db')
            db = sqlite3.connect(path)
            db.executescript('''CREATE TABLE appliances(id TEXT,cloud_id TEXT);
                CREATE TABLE appliance_claims(id TEXT,device_id TEXT,claim_session_id TEXT UNIQUE,claim_code_hash TEXT,status TEXT,expires_at TEXT,created_at TEXT,device_secret_hash TEXT,label_verifier_hash TEXT,revoked_at TEXT,proof_expires_at TEXT,claim_proof_encrypted TEXT,claim_proof_plaintext TEXT,claim_public_id TEXT);
                CREATE UNIQUE INDEX idx_claim_one_live_session_per_device ON appliance_claims(LOWER(device_id)) WHERE status IN ('pending','claimed') AND revoked_at IS NULL;''')
            db.close()
            @contextmanager
            def connection():
                con = sqlite3.connect(path,timeout=5); con.row_factory=sqlite3.Row
                try:
                    yield con; con.commit()
                except Exception:
                    con.rollback(); raise
                finally: con.close()
            def row(sql,args):
                with connection() as con:
                    item=con.execute(sql,args).fetchone()
                    return dict(item) if item else None
            barrier=threading.Barrier(2)
            def initially_none(device_id):
                barrier.wait(timeout=5); return None
            source=(Path(__file__).resolve().parents[1]/'appliance_claims.py').read_text(encoding='utf-8')
            begin=source[source.index('    def claim_begin('):source.index("    @app.post('/api/appliance/claim/status')")]
            ns={'Request':object,'HTTPException':HttpError,'_client_ip':lambda request:'synthetic',
                'claim_begin_limiter':SimpleNamespace(allow=lambda client:True),'_valid_device_id':lambda v:True,
                '_normalize_device_id':lambda v:v.lower(),'_device_secret':lambda payload:payload['secret'],
                '_label_verifier_from':lambda payload:None,'_clear_label_requested':lambda payload:False,
                '_possesses':lambda claim,secret:claim['device_secret_hash']=='h'+secret,'password_hash':lambda value:'h'+value,
                '_find_pending_claim_for_device':initially_none,'_generate_claim_code':lambda long_code=False:secrets.token_hex(4),
                'claim_id_for':lambda device_id:'synthetic-claim-id',
                '_now':datetime.now,'timedelta':timedelta,'CLAIM_SESSION_TTL_MINUTES':15,'secrets':secrets,
                '_seconds_until':lambda value:900,'connection':connection,'row':row,'logger':SimpleNamespace(info=lambda *a:None)}
            exec(compile(textwrap.dedent(begin),'claim_begin','exec',flags=__import__('__future__').annotations.compiler_flag),ns)
            results=[]; errors=[]
            def run():
                try: results.append(ns['claim_begin'](object(),{'device_id':'device','secret':'same'}))
                except HttpError as error: errors.append(error.status_code)
            with patch.dict(sys.modules, {'gateway_schema_readiness':readiness,'database_backend':SimpleNamespace(backend=lambda:'sqlite')}):
                threads=[threading.Thread(target=run) for _ in range(2)]
                for thread in threads: thread.start()
                for thread in threads: thread.join(timeout=10)
                self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertEqual(len(results),1); self.assertEqual(errors,[409])
                def pending(device_id):
                    return row("SELECT * FROM appliance_claims WHERE device_id=? AND status='pending'",(device_id,))
                ns['_find_pending_claim_for_device']=pending
                retry=ns['claim_begin'](object(),{'device_id':'device','secret':'same'})
                self.assertTrue(retry['resumed'])
                self.assertEqual(retry['claim_session_id'],results[0]['claim_session_id'])
                stale=dict(pending('device')); stale['claim_code_hash']='old-hash'
                ns['_find_pending_claim_for_device']=lambda device_id:stale
                with self.assertRaises(HttpError) as error: ns['claim_begin'](object(),{'device_id':'device','secret':'same'})
                self.assertEqual(error.exception.status_code,409)
                with connection() as con:
                    con.execute("UPDATE appliance_claims SET status='claimed',proof_expires_at=?",((datetime.now()-timedelta(seconds=1)).isoformat(),))
                ns['_find_pending_claim_for_device']=lambda device_id:None
                replacement=ns['claim_begin'](object(),{'device_id':'device','secret':'same'})
                self.assertFalse(replacement['resumed'])


if __name__ == '__main__': unittest.main()
