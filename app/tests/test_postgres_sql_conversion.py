"""database_backend._postgres_sql (d338ef3): a literal % is escaped before ?
becomes psycopg's %s, so parameterised statements with LIKE '...%' work on
PostgreSQL. Pure conversion tests; the PostgreSQL execution is covered by
test_postgres_capacity_races_green.py::test_capacity_without_a_per_camera_plan_reads_on_postgresql."""
import pytest

from database_backend import _postgres_sql


@pytest.mark.parametrize("sql,expected", [
    ("SELECT id FROM hardware_orders WHERE customer_id=? AND sku LIKE 'AIC-APPLIANCE-%' LIMIT 1",
     "SELECT id FROM hardware_orders WHERE customer_id=%s AND sku LIKE 'AIC-APPLIANCE-%%' LIMIT 1"),
    ("SELECT 1 FROM d WHERE d.status NOT LIKE 'skipped%' AND n.user_id=?",
     "SELECT 1 FROM d WHERE d.status NOT LIKE 'skipped%%' AND n.user_id=%s"),
    ("SELECT id FROM cameras WHERE id=? AND customer_id=?", "SELECT id FROM cameras WHERE id=%s AND customer_id=%s"),
    ("SELECT 100 % 7", "SELECT 100 %% 7"),
])
def test_literal_percent_is_escaped_and_placeholders_converted(sql, expected):
    assert _postgres_sql(sql) == expected


def test_the_three_production_queries_with_a_literal_percent_convert_cleanly():
    import pathlib
    import re
    app = pathlib.Path(__file__).resolve().parents[1]
    found = []
    for name in ("customer_entitlements.py", "build_orders.py", "notification_engine.py"):
        found += re.findall(r"LIKE '[^']*%'", (app / name).read_text(encoding="utf-8"))
    assert len(found) >= 3
    for fragment in found:
        assert "%%" in _postgres_sql(fragment) and "%'" not in _postgres_sql(fragment).replace("%%'", "")


def test_psycopg_accepts_the_converted_statement_with_parameters():
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql as _unused  # noqa: F401
    from psycopg._queries import PostgresQuery
    query = PostgresQuery(psycopg.adapt.Transformer())
    query.convert(_postgres_sql("SELECT 1 WHERE 'AIC-APPLIANCE-X' LIKE 'AIC-APPLIANCE-%' AND ?=?"), ("a", "a"))
    assert b"%%" not in query.query and b"LIKE 'AIC-APPLIANCE-%'" in query.query
