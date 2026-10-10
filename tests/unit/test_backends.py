"""Sample data, SQLite backend, SQL Server helpers (no server needed), target parsing."""

from __future__ import annotations

import datetime as dt
import decimal
from pathlib import Path

import pytest

from nl2sql import sample
from nl2sql.db import open_backend, parse_target
from nl2sql.db.base import BackendError, jsonable
from nl2sql.db.mssql import _format_type, _is_timeout, parse_showplan, split_batches, substitute
from nl2sql.db.sqlite import SQLiteBackend, tsql_to_sqlite

ROOT = Path(__file__).resolve().parents[2]


def test_sample_is_deterministic():
    a, b = sample.generate(), sample.generate()
    assert a.rows == b.rows
    assert a.count("customers") == 60 and a.count("orders") == 400


def test_sample_has_the_interesting_cases():
    data = sample.generate()
    ordering = {r[1] for r in data.rows["orders"]}
    assert sample.NO_ORDER_CUSTOMERS.isdisjoint(ordering)
    sold = {r[2] for r in data.rows["order_items"]}
    assert sample.UNSOLD_PRODUCT_IDS.isdisjoint(sold)
    assert {r[4] for r in data.rows["orders"]} == {"shipped", "pending", "cancelled"}
    assert all(r[9].startswith("9") for r in data.rows["employees"])  # never-issued SSN range


def test_build_sqlite_refuses_to_overwrite(tmp_path):
    p = tmp_path / "x.db"
    sample.build_sqlite(p)
    with pytest.raises(FileExistsError):
        sample.build_sqlite(p)
    sample.build_sqlite(p, force=True)


def test_committed_tsql_is_current():
    assert (ROOT / "sql/mssql/01_schema.sql").read_text(encoding="utf-8") == sample.tsql_schema()
    assert (ROOT / "sql/mssql/02_seed.sql").read_text(encoding="utf-8") == sample.tsql_seed()


def test_tsql_seed_escapes_quotes():
    assert sample._tsql_literal("O'Brien", sample.ColumnDef("x", "NVARCHAR(10)")) == "N'O''Brien'"
    assert sample._tsql_literal(None, sample.ColumnDef("x", "INT")) == "NULL"
    assert sample._tsql_literal(1.5, sample.ColumnDef("x", "DECIMAL(4,2)")) == "1.50"


def test_tsql_translation_for_sqlite():
    out = tsql_to_sqlite("SELECT TOP 5 name FROM dbo.products ORDER BY name")
    assert out == "SELECT name FROM products ORDER BY name LIMIT 5"


def test_sqlite_t_sql_date_functions(backend):
    res = backend.execute(
        "SELECT YEAR(order_date) AS y, MONTH(order_date) AS m, DAY(order_date) AS d "
        "FROM orders WHERE order_id = 1",
        max_rows=1,
        timeout_s=5,
    )
    assert res.rows[0][0] == 2025
    assert 1 <= res.rows[0][1] <= 12


def test_sqlite_execute_errors_are_backend_errors(backend):
    with pytest.raises(BackendError):
        backend.execute("SELECT nope FROM products", max_rows=1, timeout_s=5)
    with pytest.raises(BackendError):
        backend.execute("SELECT (", max_rows=1, timeout_s=5)


def test_sqlite_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="init-sample"):
        SQLiteBackend(tmp_path / "missing.db")


@pytest.mark.parametrize(
    ("target", "kind", "value"),
    [
        ("sqlite:foo.db", "sqlite", "foo.db"),
        ("sqlite:///abs/foo.db", "sqlite", "abs/foo.db"),
        ("foo.sqlite", "sqlite", "foo.sqlite"),
        ("mssql:Server=x;Database=y", "mssql", "Server=x;Database=y"),
        ("azuresql:Server=x", "mssql", "Server=x"),
        (
            "Server=tcp:x.database.windows.net,1433;Database=y",
            "mssql",
            "Server=tcp:x.database.windows.net,1433;Database=y",
        ),
    ],
)
def test_parse_target(target, kind, value):
    assert parse_target(target) == (kind, value)


def test_parse_target_rejects_unknown():
    with pytest.raises(ValueError, match="don't know how to open"):
        parse_target("postgres://nope")


def test_open_backend_sqlite(sample_db):
    with open_backend(f"sqlite:{sample_db}") as b:
        assert b.name == "sqlite"


def test_split_batches():
    script = "SELECT 1;\nGO\n  go  \nSELECT 2\nGO -- done\nSELECT 'GO' AS x\n"
    assert split_batches(script) == ["SELECT 1;", "SELECT 2", "SELECT 'GO' AS x"]


def test_substitute():
    assert substitute("PASSWORD = N'$(PW)'", {"PW": "s3cret"}) == "PASSWORD = N's3cret'"
    with pytest.raises(KeyError):
        substitute("$(MISSING)", {})


def test_parse_showplan():
    xml = (
        '<ShowPlanXML><BatchSequence><Batch><Statements><StmtSimple StatementText="x" '
        'StatementEstRows="1234.5" StatementSubTreeCost="0.0328" /></Statements></Batch>'
        "</BatchSequence></ShowPlanXML>"
    )
    est = parse_showplan(xml)
    assert est.estimated_rows == 1234.5
    assert est.estimated_cost == pytest.approx(0.0328)
    assert parse_showplan("<nothing/>") is None


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("nvarchar", 100, 0, 0), "NVARCHAR(50)"),
        (("varchar", -1, 0, 0), "VARCHAR(MAX)"),
        (("decimal", 9, 10, 2), "DECIMAL(10,2)"),
        (("int", 4, 10, 0), "INT"),
        (("char", 2, 0, 0), "CHAR(2)"),
    ],
)
def test_format_type(args, expected):
    assert _format_type(*args) == expected


def test_timeout_detection():
    assert _is_timeout(Exception("[HYT00] [Microsoft][ODBC Driver 18] Query timeout expired"))
    assert not _is_timeout(Exception("Invalid object name 'x'"))


def test_jsonable():
    assert jsonable(decimal.Decimal("1.50")) == 1.5
    assert jsonable(dt.date(2025, 1, 2)) == "2025-01-02"
    assert jsonable(b"\x01") == "01"
