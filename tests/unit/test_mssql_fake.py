"""MSSQLBackend against a fake DB-API driver. No server needed.

The real-server behaviour is covered by tests/integration (CI runs it against
SQL Server 2025 in a container). These tests pin down the Python side: which
statements are sent, that every call rolls back, how errors are mapped.
"""

from __future__ import annotations

import pytest

import nl2sql.db.mssql as mssql
from nl2sql.db.base import BackendError, QueryTimeout


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows: list[tuple] = []

    def execute(self, sql, *params):
        self.conn.log.append(sql)
        handler = self.conn.responder(sql)
        if isinstance(handler, Exception):
            raise handler
        cols, rows = handler
        self.description = [(c,) for c in cols] if cols else None
        self._rows = list(rows)

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def fetchmany(self, n):
        rows, self._rows = self._rows[:n], self._rows[n:]
        return rows

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def nextset(self):
        return False

    def close(self):
        pass


class FakeConn:
    def __init__(self, responder, autocommit):
        self.responder = responder
        self.autocommit = autocommit
        self.log: list[str] = []
        self.rollbacks = 0
        self.commits = 0
        self.timeout = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.rollbacks += 1

    def commit(self):  # pragma: no cover - must never be called
        self.commits += 1

    def close(self):
        self.closed = True


@pytest.fixture
def fake(monkeypatch):
    conns: list[FakeConn] = []
    state = {"responder": lambda sql: ([], [])}

    def connect(conn_str, *, autocommit=False, driver=None):
        c = FakeConn(lambda sql: state["responder"](sql), autocommit)
        conns.append(c)
        return c, "fake"

    monkeypatch.setattr(mssql, "connect", connect)

    def make(responder):
        state["responder"] = responder
        return mssql.MSSQLBackend("Server=x;Database=y")

    make.conns = conns
    return make


def test_execute_fetches_cap_plus_one_and_rolls_back(fake):
    b = fake(lambda sql: (["n"], [(i,) for i in range(10)]))
    res = b.execute("SELECT TOP 100 n FROM t", max_rows=3, timeout_s=7)
    assert res.rows == [(0,), (1,), (2,)]
    assert res.truncated
    assert b.conn.rollbacks == 1 and b.conn.commits == 0
    assert b.conn.timeout == 7


def test_execute_maps_timeout(fake):
    b = fake(lambda sql: Exception("[HYT00] Query timeout expired"))
    with pytest.raises(QueryTimeout):
        b.execute("SELECT 1", max_rows=1, timeout_s=1)
    assert b.conn.rollbacks == 1


def test_execute_maps_other_errors(fake):
    b = fake(lambda sql: Exception("The SELECT permission was denied on the column 'salary' (230)"))
    with pytest.raises(BackendError, match="230"):
        b.execute("SELECT salary FROM employees", max_rows=1, timeout_s=1)


def test_execute_requires_a_result_set(fake):
    b = fake(lambda sql: ([], []))
    with pytest.raises(BackendError, match="no result set"):
        b.execute("SELECT 1", max_rows=1, timeout_s=1)


PRIV_COLS = ["login", "user"] + [f"f{i}" for i in range(10)]


def test_privilege_report_read_only(fake):
    def respond(sql):
        if "SUSER_SNAME" in sql:
            return PRIV_COLS, [("nl2sql_reader", "nl2sql_reader", *[0] * 10)]
        return ["t", "p"], []

    report = fake(respond).privilege_report()
    assert report.read_only
    assert report.principal == "nl2sql_reader (user nl2sql_reader)"


def test_privilege_report_flags_writers(fake):
    def respond(sql):
        if "SUSER_SNAME" in sql:
            return PRIV_COLS, [("sa", "dbo", 1, 1, 0, 0, 1, 1, 1, 1, 1, 1)]
        return ["t", "p"], [("dbo.orders", "DELETE")]

    report = fake(respond).privilege_report()
    assert not report.read_only
    assert "sysadmin" in report.findings and "db_owner" in report.findings
    assert "DELETE on dbo.orders" in report.findings


def test_estimate_cost_uses_separate_autocommit_connection(fake):
    plan = (
        '<ShowPlanXML><StmtSimple StatementEstRows="42" StatementSubTreeCost="0.5"/></ShowPlanXML>'
    )

    def respond(sql):
        if sql.startswith("SET SHOWPLAN"):
            return [], []
        return ["Microsoft SQL Server 2005 XML Showplan"], [(plan,)]

    b = fake(respond)
    est = b.estimate_cost("SELECT TOP 10 name FROM products")
    assert est.estimated_rows == 42 and est.estimated_cost == 0.5
    plan_conn = fake.conns[-1]
    assert plan_conn is not b.conn and plan_conn.autocommit
    assert plan_conn.log == [
        "SET SHOWPLAN_XML ON",
        "SELECT TOP 10 name FROM products",
        "SET SHOWPLAN_XML OFF",
    ]
    b.estimate_cost("SELECT 1")
    assert len(fake.conns) == 2  # reused, not reopened
    b.close()
    assert plan_conn.closed and b.conn.closed


def test_estimate_cost_turns_showplan_off_even_on_error(fake):
    def respond(sql):
        if sql.startswith("SET SHOWPLAN"):
            return [], []
        return Exception("permission denied: SHOWPLAN")

    b = fake(respond)
    with pytest.raises(BackendError, match="SHOWPLAN failed"):
        b.estimate_cost("SELECT 1")
    assert fake.conns[-1].log[-1] == "SET SHOWPLAN_XML OFF"


def test_introspect(fake):
    def respond(sql):
        if "sys.columns" in sql and "foreign_key" not in sql:
            return ["s"] * 9, [
                ("dbo", "orders", "order_id", "int", 4, 10, 0, 0, 1),
                ("dbo", "orders", "status", "nvarchar", 24, 0, 0, 0, 0),
                ("dbo", "orders", "amount", "decimal", 9, 10, 2, 1, 0),
                ("security", "reader_regions", "user_name", "sysname", 256, 0, 0, 0, 1),
            ]
        if "foreign_key_columns" in sql:
            return ["s"] * 6, [("dbo", "orders", "customer_id", "dbo", "customers", "customer_id")]
        if "SELECT DISTINCT TOP" in sql:
            return ["v"], [("pending",), ("shipped",)]
        return [], []

    schema = fake(respond).introspect(sample_values=True, schemas=["dbo"])
    assert set(schema.tables) == {"orders"}  # security schema filtered out
    orders = schema.table("orders")
    assert orders.column("order_id").primary_key and orders.column("order_id").data_type == "INT"
    assert orders.column("status").data_type == "NVARCHAR(12)"
    assert orders.column("status").sample_values == ["pending", "shipped"]
    assert orders.column("amount").data_type == "DECIMAL(10,2)"
    assert schema.foreign_keys[0].ref_table == "customers"


def test_run_script_splits_and_substitutes(fake):
    b = fake(lambda sql: ([], []))
    n = b.run_script(
        "CREATE LOGIN x WITH PASSWORD = N'$(PW)'\nGO\nSELECT 1\nGO\n", {"PW": "Pw-1!x"}
    )
    assert n == 2
    script_conn = fake.conns[-1]
    assert script_conn.autocommit
    assert script_conn.log == ["CREATE LOGIN x WITH PASSWORD = N'Pw-1!x'", "SELECT 1"]
    with pytest.raises(ValueError, match="single quote"):
        b.run_script("SELECT N'$(PW)'", {"PW": "it's"})


def test_connect_without_any_driver(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_drivers(name, *a, **k):
        if name in ("mssql_python", "pyodbc"):
            raise ImportError(name)
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_drivers)
    with pytest.raises(RuntimeError, match="No SQL Server driver"):
        mssql.connect("Server=x", driver="auto")


def test_strip_driver_for_mssql_python():
    assert mssql._strip_driver("Driver={ODBC Driver 18 for SQL Server};Server=x;") == "Server=x"
