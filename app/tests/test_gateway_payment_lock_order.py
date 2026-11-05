"""PostgreSQL-shaped lock order test; no database, migrations or providers."""
import ast
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


class GatewayPaymentLockTests(unittest.TestCase):
    def test_postgres_payment_locks_follow_charge_intent_invoice_order(self):
        source = Path(__file__).resolve().parents[1] / "billing_status.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "lock_payment")
        ns = {"_LOCK_ORDER":{"ch":0, "pi":1, "in":2},
              "_now":lambda:datetime(2026, 1, 1, tzinfo=timezone.utc)}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), ns)
        db = Mock()
        with patch.dict(sys.modules, {"database_backend":SimpleNamespace(backend=lambda:"postgresql")}):
            ns["lock_payment"](db, ["in:invoice", "pi:intent", "ch:charge", "pi:intent"])
        calls = db.execute.call_args_list
        self.assertEqual(len(calls), 6)
        for index, key in enumerate(("ch:charge", "pi:intent", "in:invoice")):
            self.assertIn("INSERT INTO payment_locks", calls[index * 2].args[0])
            self.assertEqual(calls[index * 2].args[1][0], key)
            self.assertIn("FOR UPDATE", calls[index * 2 + 1].args[0])
            self.assertEqual(calls[index * 2 + 1].args[1], (key,))


if __name__ == "__main__":
    unittest.main()
