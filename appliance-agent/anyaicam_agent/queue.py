import json
import sqlite3
import time
from contextlib import contextmanager


class OfflineQueue:
    def __init__(self,path):
        self.path=path; self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.db() as db: db.execute('CREATE TABLE IF NOT EXISTS queue(id TEXT PRIMARY KEY,method TEXT,path TEXT,payload_json TEXT,attempts INTEGER DEFAULT 0,next_attempt REAL,created_at REAL)')
    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path); db.row_factory=sqlite3.Row
        try: yield db; db.commit()
        finally: db.close()
    def put(self,item_id,method,path,payload):
        with self.db() as db: return db.execute('INSERT OR IGNORE INTO queue(id,method,path,payload_json,next_attempt,created_at) VALUES(?,?,?,?,?,?)',(item_id,method,path,json.dumps(payload),time.time(),time.time())).rowcount==1
    def ready(self,limit=50):
        with self.db() as db: return [dict(item) for item in db.execute('SELECT * FROM queue WHERE next_attempt<=? ORDER BY created_at LIMIT ?',(time.time(),limit))]
    def success(self,item_id):
        with self.db() as db: db.execute('DELETE FROM queue WHERE id=?',(item_id,))
    def fail(self,item_id):
        with self.db() as db:
            item=db.execute('SELECT attempts FROM queue WHERE id=?',(item_id,)).fetchone(); attempts=(item['attempts'] if item else 0)+1; delay=min(3600,2**min(attempts,11)); db.execute('UPDATE queue SET attempts=?,next_attempt=? WHERE id=?',(attempts,time.time()+delay,item_id)); return delay
    def count(self):
        with self.db() as db: return db.execute('SELECT COUNT(*) count FROM queue').fetchone()['count']
