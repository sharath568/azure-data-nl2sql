"""SQL Server / Azure SQL integration tests.

Run against a database prepared with:

    nl2sql init-sample --db "mssql:<admin conn to nl2sql_sample>"
    python scripts/run_tsql.py "<admin conn>" sql/mssql/03_login_sqlserver.sql \\
        sql/mssql/04_permissions.sql sql/mssql/05_row_level_security.sql \\
        -v READER_PASSWORD=... -v WEST_PASSWORD=...

Environment:

    NL2SQL_TEST_CONN        connection string for nl2sql_reader (required)
    NL2SQL_TEST_WEST_CONN   connection string for nl2sql_reader_west (RLS tests)
    NL2SQL_TEST_ADMIN_CONN  connection string for an admin (privilege-check tests)

The important tests here call the executor *directly*, skipping every text
guardrail, and show the database itself refuses: writes, sensitive columns,
long-running statements. That is layer (g), and it is the one that does not
depend on a parser being right.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration

READER = os.environ.get("NL2SQL_TEST_CONN")
WEST = os.environ.get("NL2SQL_TEST_WEST_CONN")
ADMIN = os.environ.get("NL2SQL_TEST_ADMIN_CONN")

if not READER:
    pytest.skip("NL2SQL_TEST_CONN not set", allow_module_level=True)

from nl2sql.db.base import BackendError, QueryTimeout  # noqa: E402
from nl2sql.db.mssql import MSSQLBackend  # noqa: E402
from nl2sql.engine import Engine  # noqa: E402
from nl2sql.evaluation import load_adversarial, load_gold, results_match, run_eval  # noqa: E402
from nl2sql.evaluation.compare import has_top_level_order  # noqa: E402
from nl2sql.providers.stub import TemplateProvider  # noqa: E402


@pytest.fixture
def reader():
    b = MSSQLBackend(READER)
    yield b
    b.close()


def count(backend, table: str) -> int:
    return backend.execute(f"SELECT COUNT(*) FROM dbo.{table}", max_rows=1, timeout_s=10).rows[0][0]


# ---------------------------------------------------------------- the boundary


def test_reader_is_read_only(reader):
    report = reader.privilege_report()
    assert report.read_only, report.findings
    assert "nl2sql_reader" in report.principal


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM dbo.orders",
        "UPDATE dbo.products SET list_price = 0",
        "INSERT INTO dbo.products (product_id, name, category, list_price, discontinued) "
        "VALUES (999, N'x', N'y', 1, 0)",
        "TRUNCATE TABLE dbo.order_items",
        "DROP TABLE dbo.orders",
        "CREATE TABLE dbo.stolen (x INT)",
        "ALTER TABLE dbo.orders ADD x INT",
        "EXEC xp_cmdshell 'whoami'",
        "CREATE LOGIN evil WITH PASSWORD = 'Aa1!aaaaaaaa'",
        "GRANT CONTROL ON SCHEMA::dbo TO public",
    ],
)
def test_engine_refuses_writes_when_guardrails_are_bypassed(reader, sql):
    before = (count(reader, "orders"), count(reader, "products"), count(reader, "order_items"))
    with pytest.raises(BackendError):
        reader.execute_raw(sql)
    after = (count(reader, "orders"), count(reader, "products"), count(reader, "order_items"))
    assert before == after


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT salary FROM dbo.employees",
        "SELECT ssn FROM dbo.employees",
        "SELECT email FROM dbo.customers",
        "SELECT * FROM dbo.employees",
        "SELECT first_name FROM dbo.employees WHERE salary > 100000",
        "SELECT first_name FROM dbo.employees ORDER BY salary",
        "SELECT first_name FROM dbo.customers UNION SELECT ssn FROM dbo.employees",
        # a bare COUNT(*) names no column, so it is checked against all of them
        "SELECT COUNT(*) FROM dbo.customers",
    ],
)
def test_column_deny_enforced_by_the_engine(reader, sql):
    with pytest.raises(BackendError, match=r"230|denied"):
        reader.execute_raw(sql)


def test_allowed_columns_still_work(reader):
    rows = reader.execute_raw("SELECT COUNT(*), MIN(first_name) FROM dbo.employees")
    assert rows[0][0] == 12


def test_system_catalog_is_not_useful_to_the_reader(reader):
    # The guardrail blocks sys.* outright; this shows what the engine alone allows.
    # Metadata visibility rules mean the reader cannot see other application logins
    # (it may still see its own and a few built-in ones).
    names = {r[0] for r in reader.execute_raw("SELECT name FROM sys.sql_logins")}
    assert "nl2sql_reader_west" not in names
    assert "evil" not in names


def test_statement_timeout(reader):
    with pytest.raises(QueryTimeout):
        reader.execute_raw("WAITFOR DELAY '00:00:20'", timeout_s=2)


def test_row_level_security_west():
    if not WEST:
        pytest.skip("NL2SQL_TEST_WEST_CONN not set")
    west = MSSQLBackend(WEST)
    full = MSSQLBackend(READER)
    try:
        regions = west.execute_raw("SELECT DISTINCT region FROM dbo.customers")
        assert regions == [("West",)]
        west_n = west.execute_raw("SELECT COUNT(customer_id) FROM dbo.customers")[0][0]
        expected = full.execute_raw(
            "SELECT COUNT(customer_id) FROM dbo.customers WHERE region = N'West'"
        )[0][0]
        assert west_n == expected
        assert full.execute_raw("SELECT COUNT(customer_id) FROM dbo.customers")[0][0] == 60
        # the mapping table is not readable by the readers
        with pytest.raises(BackendError):
            west.execute_raw("SELECT * FROM security.reader_regions")
    finally:
        west.close()
        full.close()


def test_ownership_chaining_is_why_udfs_are_blocked(reader):
    """A dbo-owned inline table-valued function can return a column the reader is
    denied: SQL Server skips the column check along an unbroken ownership chain.
    The engine alone does NOT stop this. Guardrail (c) does, by refusing
    user-defined functions. This test documents both halves."""
    if not ADMIN:
        pytest.skip("NL2SQL_TEST_ADMIN_CONN not set")
    from nl2sql import guardrails
    from nl2sql.semantic import load_sample

    admin = MSSQLBackend(ADMIN)
    try:
        admin.run_script(
            "CREATE OR ALTER FUNCTION dbo.nl2sql_test_pay() RETURNS TABLE AS "
            "RETURN SELECT employee_id, salary FROM dbo.employees"
        )
        sql = "SELECT employee_id, pay.salary FROM dbo.nl2sql_test_pay() AS pay"
        # 1. the engine lets it through (ownership chaining)...
        rows = reader.execute_raw(sql)
        assert len(rows) == 12
        # 2. ...the guardrail does not.
        report = guardrails.check(sql, load_sample(), schema=reader.introspect(schemas=["dbo"]))
        assert report.blocked_by == "select_only"
        assert "table-valued function" in report.reason
    finally:
        admin.run_script("DROP FUNCTION IF EXISTS dbo.nl2sql_test_pay")
        admin.close()


def test_admin_connection_is_refused_by_the_engine():
    if not ADMIN:
        pytest.skip("NL2SQL_TEST_ADMIN_CONN not set")
    from nl2sql.semantic import load_sample

    admin = MSSQLBackend(ADMIN)
    try:
        report = admin.privilege_report()
        assert not report.read_only
        res = Engine(admin, TemplateProvider(), load_sample()).ask("How many customers do we have?")
        assert res.status == "blocked"
        assert res.guard.blocked_by == "least_privilege"
    finally:
        admin.close()


# ---------------------------------------------------------------- the pipeline


def test_introspection(reader):
    schema = reader.introspect(schemas=["dbo"])
    assert set(schema.tables) == {"customers", "products", "employees", "orders", "order_items"}
    assert schema.table("orders").column("order_date").data_type == "DATE"
    assert schema.table("products").column("list_price").data_type == "DECIMAL(10,2)"
    assert schema.table("orders").column("order_id").primary_key
    assert ("orders", "customer_id", "customers") in {
        (f.table, f.column, f.ref_table) for f in schema.foreign_keys
    }


def test_showplan_estimate(reader):
    est = reader.estimate_cost("SELECT TOP 10 name FROM dbo.products ORDER BY list_price DESC")
    assert est is not None
    assert est.estimated_rows <= 10
    assert est.estimated_cost > 0


def test_ask_end_to_end(reader):
    from nl2sql.semantic import load_sample

    res = Engine(reader, TemplateProvider(), load_sample()).ask(
        "What are the top 5 products by revenue?"
    )
    assert res.status == "ok", res.to_dict()
    assert len(res.result.rows) == 5
    cost = next(c for c in res.guard.checks if c.name == "cost")
    assert any("SHOWPLAN" in d for d in cost.details)
    priv = next(c for c in res.guard.checks if c.name == "least_privilege")
    assert priv.status.value == "pass"


def test_gold_sql_gives_the_same_answers_on_sql_server_and_sqlite(reader, tmp_path):
    """The offline backend translates T-SQL for SQLite. This checks the
    translation does not change any gold answer."""
    from nl2sql import sample
    from nl2sql.db.sqlite import SQLiteBackend

    path = tmp_path / "s.db"
    sample.build_sqlite(path)
    lite = SQLiteBackend(path)
    try:
        for item in load_gold():
            a = reader.execute(item.sql, max_rows=1000, timeout_s=30).rows
            b = lite.execute(item.sql, max_rows=1000, timeout_s=30).rows
            assert results_match(a, b, ordered=has_top_level_order(item.sql)), (
                item.id,
                a[:3],
                b[:3],
            )
    finally:
        lite.close()


def test_eval_on_sql_server(reader):
    from nl2sql.semantic import load_sample

    report = run_eval(
        Engine(reader, TemplateProvider(), load_sample()), load_gold(), load_adversarial()
    )
    s = report.summary()
    assert s["adversarial"]["executed"] == []
    assert s["gold"]["gold_sql_blocked_by_guardrails"] == 0
    assert s["gold"]["execution_accuracy"] > 0.5
