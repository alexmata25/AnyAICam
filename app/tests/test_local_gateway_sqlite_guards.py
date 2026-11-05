"""Disposable SQLite guard probes. Never imports the VMS or runs migrations."""
import _sqlite3
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from app.tests.gateway_http_guards import (
    make_sqlite_connect_guard, make_sqlite_connection_class, sqlite_audit_scope,
)


class SQLiteGuardTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Keep the original Windows temporary path spelling to exercise 8.3 paths.
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="aac-sqlite-guard-")))
        self.approved = self.root / "approved.sqlite3"
        self.other = self.root / "other.sqlite3"
        self.captured_connect = sqlite3.connect
        self.native_connect = _sqlite3.connect
        self.original_class = sqlite3.Connection
        self.stack.enter_context(sqlite_audit_scope())
        cls = make_sqlite_connection_class(self.approved, self.original_class)
        connect = make_sqlite_connect_guard(self.approved, self.captured_connect, cls)
        for module in (sqlite3, _sqlite3):
            self.stack.enter_context(patch.object(module, "connect", connect))
            self.stack.enter_context(patch.object(module, "Connection", cls))

    def test_approved_file_opens_and_short_windows_path_resolves(self):
        db = sqlite3.connect(self.approved)
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT 1").fetchone(), (1,))

    def test_other_paths_uris_and_factories_are_rejected(self):
        cases = [((self.other,), {}), ((":memory:",), {}),
                 (("file:" + self.approved.as_posix(),), {"uri": True}),
                 ((self.approved,), {"uri": True}),
                 ((self.approved,), {"factory": self.original_class})]
        for args, kwargs in cases:
            with self.subTest(args=args, kwargs=kwargs), self.assertRaises(RuntimeError):
                sqlite3.connect(*args, **kwargs)
        self.assertFalse(self.other.exists())

    def test_attach_and_extension_loading_are_rejected(self):
        db = sqlite3.connect(self.approved)
        self.addCleanup(db.close)
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("ATTACH DATABASE ? AS other", (str(self.other),))
        with self.assertRaises(RuntimeError):
            db.enable_load_extension(True)
        with self.assertRaises(RuntimeError):
            self.original_class.enable_load_extension(db, True)
        with self.assertRaises(RuntimeError):
            self.original_class.load_extension(db, "synthetic")
        with self.assertRaises(sqlite3.DatabaseError):
            db.execute("SELECT load_extension('synthetic')")
        self.assertFalse(self.other.exists())

    def test_captured_and_native_entry_points_cannot_open(self):
        for connect in (self.captured_connect, self.native_connect, self.original_class):
            for target in (self.approved, self.other):
                with self.subTest(connect=connect, target=target), self.assertRaises(RuntimeError):
                    connect(target)
        self.assertFalse(self.other.exists())

    def test_base_class_authorizer_override_is_a_known_unconfined_boundary(self):
        # Demonstrate why an independent confinement gate must stay closed.
        db = sqlite3.connect(self.approved)
        self.addCleanup(db.close)
        with self.assertRaises(RuntimeError):
            db.set_authorizer(None)
        self.original_class.set_authorizer(db, None)
        db.execute("ATTACH DATABASE ? AS other", (str(self.other),))
        self.assertTrue(self.other.exists())


if __name__ == "__main__":
    unittest.main()
