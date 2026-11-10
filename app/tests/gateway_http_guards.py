"""Test-process-only database and provider guards; no I/O at import time."""
import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

_audit_state = threading.local()
_audit_lock = threading.Lock()
_audit_active = False
_audit_installed = False
_audit_probe = object()


def _sqlite_audit(event, args):
    if event == "anyaicam.sqlite_guard_probe" and args == (_audit_probe,):
        raise RuntimeError("SQLite audit guard active")
    if not _audit_active:
        return
    if event in ("sqlite3.enable_load_extension", "sqlite3.load_extension"):
        raise RuntimeError("SQLite extensions denied")
    if event in ("sqlite3.connect", "sqlite3.connect/handle"):
        if not getattr(_audit_state, "opening", False):
            raise RuntimeError("Unmediated SQLite connection denied")


@contextmanager
def sqlite_audit_scope():
    """Guard CPython sqlite opens for this scope; use only in a test process.

    Python audit hooks cannot be removed. The installed hook is inert outside
    this scope; it is not a security boundary for native or hostile Python code.
    """
    global _audit_active, _audit_installed
    if not _audit_lock.acquire(blocking=False):
        raise RuntimeError("A SQLite guard scope is already active")
    try:
        if not _audit_installed:
            sys.addaudithook(_sqlite_audit)
            try:
                sys.audit("anyaicam.sqlite_guard_probe", _audit_probe)
            except RuntimeError as exc:
                if str(exc) != "SQLite audit guard active":
                    raise
            else:
                raise RuntimeError("Could not install SQLite audit guard")
            _audit_installed = True
        _audit_active = True
        yield
    finally:
        _audit_active = False
        _audit_lock.release()


def validate_sqlite_target(database, approved_file, *, uri=False):
    """Allow only the one absolute SQLite path created for this test run."""
    if uri or not isinstance(database, (str, os.PathLike)):
        raise RuntimeError("SQLite URI or non-path target denied")
    text = os.fspath(database)
    if not isinstance(text, str) or not text or text == ":memory:" or text.lower().startswith("file:"):
        raise RuntimeError("Non-file SQLite target denied")
    path = Path(text)
    target = Path(approved_file)
    if not path.is_absolute() or not target.is_absolute():
        raise RuntimeError("Relative database path denied")
    if os.path.normcase(os.path.abspath(path)) != os.path.normcase(os.path.abspath(target)):
        raise RuntimeError("Unexpected database path denied")
    if path.is_symlink() or target.is_symlink():
        raise RuntimeError("Database link denied")
    if os.path.normcase(os.path.realpath(path)) != os.path.normcase(os.path.realpath(target)):
        raise RuntimeError("Database path resolves outside its approved location")
    return str(target)


def make_sqlite_connect_guard(approved_file, connect, connection_class):
    def guarded(database, *args, **kwargs):
        if not _audit_active:
            raise RuntimeError("SQLite audit guard is not active")
        if args or kwargs.get("uri", False) or "factory" in kwargs:
            raise RuntimeError("SQLite positional options, URI and custom connection factories denied")
        path = validate_sqlite_target(database, approved_file)
        if getattr(_audit_state, "opening", False):
            raise RuntimeError("Nested SQLite open denied")
        _audit_state.opening = True
        try:
            return connect(path, factory=connection_class, **kwargs)
        finally:
            _audit_state.opening = False
    return guarded


def make_sqlite_connection_class(approved_file, original_class):
    class ConfinedConnection(original_class):
        def __init__(self, database, *args, **kwargs):
            # CPython's connect(factory=...) supplies connection options positionally.
            # The public wrapper rejects those options; direct constructors fail closed.
            if len(args) > 7 or kwargs.get("uri", False) or (len(args) >= 7 and args[6]):
                raise RuntimeError("SQLite URI or unexpected connection options denied")
            super().__init__(validate_sqlite_target(database, approved_file), *args, **kwargs)
            super().set_authorizer(self._authorizer)

        @staticmethod
        def _authorizer(action, arg1, arg2, database, trigger):
            import sqlite3
            if action == sqlite3.SQLITE_ATTACH or (action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() == "load_extension"):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        def set_authorizer(self, *args, **kwargs):
            raise RuntimeError("Changing the SQLite confinement authorizer is denied")

        def enable_load_extension(self, *args, **kwargs):
            raise RuntimeError("Loading SQLite extensions is denied")
    return ConfinedConnection


def deny_provider(*args, **kwargs):
    raise RuntimeError("Unexpected provider or outbound network access denied")
