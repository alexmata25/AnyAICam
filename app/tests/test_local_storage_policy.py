"""Local recording storage management (2026-09-17): regression coverage
for local_storage_policy.py's system defaults / RDM-override resolution."""

import pytest

from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_local_storage_policy.db"


@pytest.fixture()
def db(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database, connection
        initialize_database()
        with connection() as conn:
            now = "2026-09-17T00:00:00"
            conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('p1','P',?)", (now,))
            conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','p1','A','a@example.test','active',?)", (now,))
            conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-b','p1','B','b@example.test','active',?)", (now,))
            yield conn


def test_defaults_when_no_row_exists(db):
    from local_storage_policy import local_storage_policy_for_customer, DEFAULT_RESERVED_FREE_PERCENT, DEFAULT_WARNING_FREE_PERCENT
    result = local_storage_policy_for_customer(db, "cust-never-configured")
    assert result == {"reserved_free_percent": DEFAULT_RESERVED_FREE_PERCENT, "warning_free_percent": DEFAULT_WARNING_FREE_PERCENT}


def test_rdm_override_is_used_when_set(db):
    from local_storage_policy import local_storage_policy_for_customer
    db.execute(
        "INSERT INTO local_storage_policy(customer_id,reserved_free_percent,warning_free_percent,updated_at,updated_by) VALUES(?,?,?,?,?)",
        ("cust-a", 15, 30, "2026-09-17T00:00:00", "admin@example.test"),
    )
    result = local_storage_policy_for_customer(db, "cust-a")
    assert result == {"reserved_free_percent": 15, "warning_free_percent": 30}


def test_partial_override_falls_back_to_default_for_the_unset_field(db):
    from local_storage_policy import local_storage_policy_for_customer, DEFAULT_WARNING_FREE_PERCENT
    db.execute(
        "INSERT INTO local_storage_policy(customer_id,reserved_free_percent,warning_free_percent,updated_at,updated_by) VALUES(?,?,NULL,?,?)",
        ("cust-a", 5, "2026-09-17T00:00:00", "admin@example.test"),
    )
    result = local_storage_policy_for_customer(db, "cust-a")
    assert result == {"reserved_free_percent": 5, "warning_free_percent": DEFAULT_WARNING_FREE_PERCENT}


def test_a_different_customers_override_never_leaks(db):
    from local_storage_policy import local_storage_policy_for_customer, DEFAULT_RESERVED_FREE_PERCENT
    db.execute(
        "INSERT INTO local_storage_policy(customer_id,reserved_free_percent,warning_free_percent,updated_at,updated_by) VALUES(?,?,?,?,?)",
        ("cust-a", 25, 40, "2026-09-17T00:00:00", "admin@example.test"),
    )
    result = local_storage_policy_for_customer(db, "cust-b")
    assert result["reserved_free_percent"] == DEFAULT_RESERVED_FREE_PERCENT
