import contextvars
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from cloud_config import settings


def backend(): return os.getenv('ANYAICAM_DATABASE_BACKEND',settings.database_backend).lower()


_sqlite_path_override: 'contextvars.ContextVar[str | None]' = contextvars.ContextVar('anyaicam_sqlite_path_override',default=None)
_database_url_override: 'contextvars.ContextVar[str | None]' = contextvars.ContextVar('anyaicam_database_url_override',default=None)


def sqlite_target_path() -> Path:
    override=_sqlite_path_override.get()
    return Path(override) if override is not None else Path(os.getenv('ANYAICAM_PARTNER_DB',settings.sqlite_path))


def postgres_target_url() -> str:
    override=_database_url_override.get()
    return override if override is not None else os.getenv('ANYAICAM_DATABASE_URL',settings.database_url)


@contextmanager
def override_target(*,sqlite_path=None,database_url=None):
    """Scope connect()/target_key() to an explicit database for the duration of
    this block, regardless of what ANYAICAM_PARTNER_DB/ANYAICAM_DATABASE_URL
    currently hold in os.environ.

    os.environ is process-global, but pytest imports every test module before
    running any test, so whichever module's import last wrote
    ANYAICAM_PARTNER_DB is what every test in the session sees during
    execution - an import-order-sensitive collision, not real isolation. This
    override is a contextvars.ContextVar, so it is scoped to wherever it's
    actually entered (e.g. a test's own setUp/tearDown window) instead of
    leaking across unrelated test files or code that never asked for it."""
    sqlite_token=_sqlite_path_override.set(str(sqlite_path)) if sqlite_path is not None else None
    url_token=_database_url_override.set(database_url) if database_url is not None else None
    try: yield
    finally:
        if sqlite_token is not None: _sqlite_path_override.reset(sqlite_token)
        if url_token is not None: _database_url_override.reset(url_token)


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
