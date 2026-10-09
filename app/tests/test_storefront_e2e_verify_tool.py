"""tools/storefront_e2e_verify.py (staging E2E helper) is read-only and
reports which storefront Checkout Sessions the portal recorded."""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[2] / "tools" / "storefront_e2e_verify.py"
spec = importlib.util.spec_from_file_location("storefront_e2e_verify", TOOL)
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


@pytest.fixture
def portal_db(tmp_path):
    path = tmp_path / "portal.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE hardware_orders(id TEXT, customer_id TEXT, sku TEXT, stripe_checkout_session_id TEXT,
            amount_cents INTEGER, currency TEXT, status TEXT, created_at TEXT);
        CREATE TABLE pending_customer_links(id TEXT, normalized_email TEXT, stripe_checkout_session_id TEXT,
            product TEXT, camera_slot_quantity INTEGER, raw_event_json TEXT, status TEXT, created_at TEXT,
            resolved_customer_id TEXT);
        INSERT INTO hardware_orders VALUES ('o1', 'c1', 'AIC-APPLIANCE-RYZEN-STARTER', 'cs_test_hw', 124999, 'usd', 'paid', 't');
        INSERT INTO pending_customer_links VALUES ('p1', 'buyer@example.test', 'cs_test_lic', 'vms_license_8', 8,
            '{"secret":"raw"}', 'pending', 't', NULL);
    """)
    db.commit()
    db.close()
    return path


def test_recorded_sessions_pass_and_raw_json_is_never_printed(portal_db, capsys):
    assert verify.main(["--db", str(portal_db), "--session", "cs_test_hw", "--session", "cs_test_lic"]) == 0
    out = capsys.readouterr().out
    assert "sku=AIC-APPLIANCE-RYZEN-STARTER" in out and "amount_cents=124999" in out
    assert "product=vms_license_8" in out and "RESULT: PASS" in out
    assert "raw" not in out and "secret" not in out


def test_an_unrecorded_session_fails(portal_db, capsys):
    assert verify.main(["--db", str(portal_db), "--session", "cs_test_hw", "--session", "cs_test_lost"]) == 1
    assert "MISSING: cs_test_lost" in capsys.readouterr().out


def test_lookup_by_email_is_case_insensitive(portal_db, capsys):
    assert verify.main(["--db", str(portal_db), "--email", " Buyer@Example.TEST "]) == 0
    assert "cs_test_lic" in capsys.readouterr().out


def test_the_database_is_opened_read_only(portal_db):
    db = sqlite3.connect(f"file:{portal_db}?mode=ro", uri=True)
    verify.find(db, ["cs_test_hw"], "")
    with pytest.raises(sqlite3.OperationalError):
        db.execute("DELETE FROM hardware_orders")
    assert "mode=ro" in TOOL.read_text(encoding="utf-8")
