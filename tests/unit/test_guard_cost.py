"""(f) cost guard: static checks offline, optimizer estimate when available."""

from __future__ import annotations

import pytest

from nl2sql import guardrails
from nl2sql.db.base import BackendError, CostEstimate, PrivilegeReport
from nl2sql.guardrails import CostGuard
from nl2sql.guardrails.base import GuardState, Status
from nl2sql.semantic import CostPolicy, Policy, SemanticLayer


def cost_check(sql, *, backend=None, **cost):
    layer = SemanticLayer(policy=Policy(cost=CostPolicy(**cost)))
    report = guardrails.check(sql, layer, backend=backend)
    return report, next((c for c in report.checks if c.name == "cost"), None)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT o.order_id FROM orders o CROSS JOIN customers c",
        "SELECT o.order_id FROM orders o, customers c",
        "SELECT a.order_id FROM orders a CROSS JOIN customers b CROSS JOIN products p",
    ],
)
def test_cartesian_products_blocked(sql):
    report, _ = cost_check(sql)
    assert report.blocked_by == "cost"
    assert "cartesian" in report.reason


def test_comma_join_with_where_is_allowed():
    report, _ = cost_check(
        "SELECT o.order_id FROM orders o, customers c WHERE c.customer_id = o.customer_id"
    )
    assert report.allowed


def test_cross_join_to_derived_total_is_allowed():
    sql = (
        "SELECT p.category, t.total FROM products p "
        "CROSS JOIN (SELECT COUNT(*) AS total FROM products) AS t"
    )
    report, _ = cost_check(sql)
    assert report.allowed


def test_cross_join_to_cte_is_allowed():
    sql = (
        "WITH t AS (SELECT COUNT(*) AS n FROM orders) "
        "SELECT o.order_id, t.n FROM orders o CROSS JOIN t"
    )
    report, _ = cost_check(sql)
    assert report.allowed


def test_cartesian_can_be_allowed_by_policy():
    report, _ = cost_check("SELECT 1 AS x FROM orders CROSS JOIN customers", block_cartesian=False)
    assert report.allowed


def test_too_many_joins():
    joins = " ".join(f"JOIN orders o{i} ON o{i}.order_id = o.order_id" for i in range(4))
    report, _ = cost_check(f"SELECT o.order_id FROM orders o {joins}", max_joins=3)
    assert report.blocked_by == "cost"
    assert "joins" in report.reason


def test_nesting_depth():
    sql = "SELECT 1 AS x"
    for _ in range(4):
        sql = f"SELECT x FROM ({sql}) AS t"
    report, _ = cost_check(sql, max_subquery_depth=2)
    assert report.blocked_by == "cost"
    assert "nested" in report.reason


def test_offline_estimate_is_reported_as_unavailable(backend):
    report, check = cost_check("SELECT name FROM products", backend=backend)
    assert report.allowed
    assert check.status is Status.PASS
    assert any("not available on sqlite" in d for d in check.details)


class FakeBackend:
    name = "fake"

    def __init__(self, est=None, fail=False):
        self.est, self.fail = est, fail

    def estimate_cost(self, sql):
        if self.fail:
            raise BackendError("no SHOWPLAN permission")
        return self.est

    def privileges(self):
        return PrivilegeReport(True, "fake", [], "test")


def _run(est=None, fail=False, **cost):
    layer = SemanticLayer(policy=Policy(cost=CostPolicy(**cost)))
    import sqlglot

    state = GuardState(
        sql="SELECT TOP 10 name FROM products", layer=layer, backend=FakeBackend(est, fail)
    )
    state.tree = sqlglot.parse_one(state.sql, read="tsql")
    return CostGuard().run(state)


def test_estimate_over_row_limit_blocks():
    r = _run(CostEstimate(5e6, 1.0, "SET SHOWPLAN_XML"), max_estimated_rows=1e6)
    assert r.blocked and "rows" in r.message


def test_estimate_over_cost_limit_blocks():
    r = _run(CostEstimate(10, 120.0, "SET SHOWPLAN_XML"), max_estimated_cost=50)
    assert r.blocked and "cost" in r.message


def test_estimate_within_limits_passes():
    r = _run(CostEstimate(10, 0.01, "SET SHOWPLAN_XML"))
    assert r.status is Status.PASS
    assert any("SHOWPLAN" in d for d in r.details)


def test_estimate_failure_is_a_warning_not_a_block():
    r = _run(fail=True)
    assert r.status is Status.WARN


def test_estimates_can_be_turned_off():
    r = _run(CostEstimate(5e9, 1e6, "x"), use_plan_estimates=False)
    assert r.status is Status.PASS
