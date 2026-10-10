"""(g) least-privilege execution: the connection is checked, and the engine
itself refuses writes even when every text guardrail is bypassed."""

from __future__ import annotations

import sqlite3

import pytest

from nl2sql import guardrails
from nl2sql.db.base import BackendError, PrivilegeReport
from nl2sql.db.sqlite import SQLiteBackend
from nl2sql.guardrails import LeastPrivilegeGuard
from nl2sql.guardrails.base import GuardState, Status
from nl2sql.semantic import Policy, SemanticLayer


class FakeBackend:
    name = "fake"

    def __init__(self, report):
        self.report = report

    def privileges(self):
        return self.report

    def estimate_cost(self, sql):
        return None


def run(report, **policy):
    state = GuardState(
        sql="SELECT 1", layer=SemanticLayer(policy=Policy(**policy)), backend=FakeBackend(report)
    )
    return LeastPrivilegeGuard().run(state)


def test_read_only_connection_passes():
    r = run(PrivilegeReport(True, "nl2sql_reader", [], "SQL Server permissions"))
    assert r.status is Status.PASS
    assert "timeout 10s" in r.message


def test_writable_connection_is_refused():
    r = run(PrivilegeReport(False, "sa", ["sysadmin", "DELETE on dbo.orders"], "x"))
    assert r.blocked
    assert "can write" in r.details[0]
    assert "sysadmin" in r.details[0]


def test_writable_connection_can_be_allowed_explicitly():
    r = run(PrivilegeReport(False, "sa", ["sysadmin"], "x"), allow_privileged_connection=True)
    assert r.status is Status.WARN


def test_skipped_without_backend():
    state = GuardState(sql="SELECT 1", layer=SemanticLayer())
    assert LeastPrivilegeGuard().run(state).status is Status.SKIP


def test_sqlite_sample_reports_read_only(backend):
    report = backend.privilege_report()
    assert report.read_only
    assert "query_only" in report.enforced_by


def test_sqlite_read_write_connection_is_flagged(sample_db, tmp_path):
    copy = tmp_path / "rw.db"
    copy.write_bytes(sample_db.read_bytes())
    rw = SQLiteBackend(copy, read_only=False)
    try:
        report = rw.privilege_report()
        assert not report.read_only
        result = guardrails.check("SELECT name FROM products", backend=rw)
        assert result.blocked_by == "least_privilege"
    finally:
        rw.close()


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "DROP TABLE orders",
        "UPDATE products SET list_price = 0",
        "INSERT INTO products (product_id, name, category, list_price, discontinued) "
        "VALUES (999, 'x', 'y', 1, 0)",
        "CREATE TABLE stolen (x INT)",
    ],
)
def test_engine_refuses_writes_even_when_guardrails_are_bypassed(backend, sample_db, sql):
    """The point of layer (g): call the executor directly, skipping (a)-(f)."""
    before = backend.execute_native("SELECT COUNT(*) FROM orders", max_rows=1, timeout_s=5).rows
    with pytest.raises(BackendError, match=r"readonly|read-only|attempt to write"):
        backend.execute_native(sql, max_rows=1, timeout_s=5)
    after = backend.execute_native("SELECT COUNT(*) FROM orders", max_rows=1, timeout_s=5).rows
    assert before == after
    # and the file on disk is untouched
    conn = sqlite3.connect(sample_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 24
    finally:
        conn.close()


def test_timeout_is_enforced(backend):
    from nl2sql.db.base import QueryTimeout

    slow = (
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 50000000) "
        "SELECT COUNT(*) FROM n"
    )
    with pytest.raises(QueryTimeout):
        backend.execute_native(slow, max_rows=1, timeout_s=0.2)
