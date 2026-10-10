"""(e) row limit: inject or clamp TOP / FETCH."""

from __future__ import annotations

import pytest
import sqlglot

from nl2sql import guardrails
from nl2sql.semantic import Policy, SemanticLayer


def run(sql: str, cap: int = 100):
    layer = SemanticLayer(policy=Policy(max_rows=cap))
    report = guardrails.check(sql, layer)
    assert report.allowed, report.to_dict()
    check = next(c for c in report.checks if c.name == "row_limit")
    return report.sql, check


@pytest.mark.parametrize(
    ("sql", "expected", "status"),
    [
        ("SELECT name FROM products", "SELECT TOP 100 name FROM products", "rewrite"),
        ("SELECT TOP 5 name FROM products", "SELECT TOP 5 name FROM products", "pass"),
        ("SELECT TOP 100 name FROM products", "SELECT TOP 100 name FROM products", "pass"),
        ("SELECT TOP 5000 name FROM products", "SELECT TOP 100 name FROM products", "rewrite"),
        ("SELECT TOP (5000) name FROM products", "SELECT TOP 100 name FROM products", "rewrite"),
        (
            "SELECT TOP 10 PERCENT name FROM products",
            "SELECT TOP 100 name FROM products",
            "rewrite",
        ),
        (
            "SELECT DISTINCT category FROM products",
            "SELECT DISTINCT TOP 100 category FROM products",
            "rewrite",
        ),
    ],
)
def test_top(sql, expected, status):
    out, check = run(sql)
    assert out == expected
    assert check.status.value == status


def test_fetch_is_clamped_not_replaced_with_top():
    out, check = run(
        "SELECT name FROM products ORDER BY name OFFSET 0 ROWS FETCH NEXT 500 ROWS ONLY"
    )
    assert "FETCH NEXT 100 ROWS ONLY" in out
    assert "TOP" not in out
    assert check.status.value == "rewrite"


def test_fetch_within_cap_untouched():
    sql = "SELECT name FROM products ORDER BY name OFFSET 10 ROWS FETCH NEXT 5 ROWS ONLY"
    out, check = run(sql)
    assert "FETCH NEXT 5 ROWS ONLY" in out
    assert check.status.value == "pass"


def test_offset_without_fetch_gets_fetch():
    out, _ = run("SELECT name FROM products ORDER BY name OFFSET 10 ROWS")
    assert "OFFSET 10 ROWS FETCH NEXT 100 ROWS ONLY" in out
    assert "TOP" not in out


def test_cte_query_gets_top_on_outer_select():
    out, _ = run("WITH x AS (SELECT name FROM products) SELECT name FROM x")
    assert out.startswith("WITH x AS")
    assert "SELECT TOP 100 name FROM x" in out


def test_inner_subquery_limits_are_left_alone():
    out, _ = run("SELECT name FROM (SELECT TOP 5000 name FROM products ORDER BY name) AS t")
    assert out.startswith("SELECT TOP 100 name FROM (SELECT TOP 5000")


def test_union_is_wrapped():
    out, check = run("SELECT name FROM products UNION SELECT first_name FROM customers")
    assert out.startswith("SELECT TOP 100 * FROM (")
    assert out.endswith(") AS _q")
    assert check.status.value == "rewrite"
    sqlglot.parse_one(out, read="tsql")  # still valid T-SQL


def test_union_keeps_simple_order_by_outside():
    out, _ = run("SELECT name FROM products UNION SELECT first_name FROM customers ORDER BY name")
    assert out.endswith("AS _q ORDER BY name")


def test_union_with_cte_hoists_the_cte():
    out, _ = run("WITH x AS (SELECT 1 AS a) SELECT a FROM x UNION SELECT 2 ORDER BY a")
    assert out.startswith("WITH x AS (SELECT 1 AS a) SELECT TOP 100 * FROM (")


def test_union_with_qualified_order_by_is_left_with_warning():
    out, check = run(
        "SELECT p.name FROM products p UNION SELECT c.first_name FROM customers c ORDER BY p.name"
    )
    assert check.status.value == "warn"
    assert "TOP" not in out


def test_cap_is_configurable():
    out, _ = run("SELECT name FROM products", cap=7)
    assert out == "SELECT TOP 7 name FROM products"


def test_executor_also_caps_rows(backend):
    # Defense in depth: even without the rewrite, execute() never returns more than max_rows.
    result = backend.execute("SELECT order_id FROM orders", max_rows=3, timeout_s=5)
    assert len(result.rows) == 3
    assert result.truncated
