"""(b) single statement guard, run through the pipeline so parse happens first."""

from __future__ import annotations

import pytest

from nl2sql import guardrails


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE orders",
        "SELECT 1;SELECT 2",
        "SELECT name FROM products;\nDELETE FROM products",
        "SELECT 1; EXEC('DROP TABLE x')",
        "DECLARE @x int = 1; SELECT @x",
        "SELECT 1; SELECT 2; SELECT 3",
    ],
)
def test_multiple_statements_blocked(sql):
    report = guardrails.check(sql)
    assert report.blocked_by == "single_statement"
    assert "expected 1 statement" in report.reason


@pytest.mark.parametrize("sql", ["SELECT 1", "SELECT 1;", "SELECT 1 ;  ", "SELECT ';' AS semi"])
def test_single_statement_allowed(sql):
    report = guardrails.check(sql)
    assert report.checks[1].name == "single_statement"
    assert not report.checks[1].blocked


def test_semicolon_inside_string_is_not_a_separator():
    report = guardrails.check("SELECT 'a; DROP TABLE x' AS s")
    assert report.allowed
