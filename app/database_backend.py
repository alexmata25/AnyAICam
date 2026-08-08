import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from cloud_config import settings


def backend(): return os.getenv('ANYAICAM_DATABASE_BACKEND',settings.database_backend).lower()


def sqlite_target_path() -> Path:
    return Path(os.getenv('ANYAICAM_PARTNER_DB',settings.sqlite_path))


def postgres_target_url() -> str:
    return os.getenv('ANYAICAM_DATABASE_URL',settings.database_url)


def target_key():
    """Hashable identity of the database `connect()` would open right now.

    Lets callers (see `partner_db.ensure_database_initialized`) notice when the
    effective connection target changes within a single process - e.g. isolated
    test databases sharing one interpreter - which a one-time import side effect
    cannot detect."""
    return ('sqlite',str(sqlite_target_path())) if backend()=='sqlite' else ('postgresql',postgres_target_url())


def _postgres_sql(sql: str) -> str:
    converted=sql.replace('INTEGER PRIMARY KEY AUTOINCREMENT','BIGSERIAL PRIMARY KEY')
    if re.match(r'\s*INSERT OR IGNORE\s+',converted,re.I):
        converted=re.sub(r'INSERT OR IGNORE','INSERT',converted,count=1,flags=re.I).rstrip().rstrip(';')+' ON CONFLICT DO NOTHING'
    return converted.replace('?','%s')


@contextmanager
def connect():
    if backend()=='sqlite':
        path=sqlite_target_path(); path.parent.mkdir(parents=True,exist_ok=True); db=sqlite3.connect(path); db.row_factory=sqlite3.Row; db.execute('PRAGMA foreign_keys=ON')
    else:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error: raise RuntimeError('Install psycopg[binary] to use PostgreSQL.') from error
        raw=psycopg.connect(postgres_target_url(),row_factory=dict_row)
        class Adapter:
            def execute(self,sql,params=()): return raw.execute(_postgres_sql(sql),params)
            def commit(self): raw.commit()
            def close(self): raw.close()
        db=Adapter()
    try: yield db; db.commit()
    except Exception:
        if backend()=='postgresql': raw.rollback()
        else: db.rollback()
        raise
    finally: db.close()


def column_names(table: str) -> set[str]:
    with connect() as db:
        if backend()=='sqlite': return {item['name'] for item in db.execute(f'PRAGMA table_info({table})').fetchall()}
        return {item['column_name'] for item in db.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?',(table,)).fetchall()}
