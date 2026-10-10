"""(a) parse guard."""

from __future__ import annotations

import pytest

from nl2sql.guardrails import ParseGuard, Status
from nl2sql.guardrails.base import GuardState
from nl2sql.semantic import Policy, SemanticLayer


def run(sql: str, **policy):
    state = GuardState(sql=sql, layer=SemanticLayer(policy=Policy(**policy)))
    return ParseGuard().run(state), state


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT TOP 5 name FROM products ORDER BY name",
        "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
        "SELECT [name] FROM [dbo].[products]",
        "SELECT N'caf\u00e9' AS word",
        "SELECT 1;",
        "select\n  name\nfrom\tproducts",
    ],
)
def test_parses(sql):
    result, state = run(sql)
    assert result.status is Status.PASS
    assert state.statements


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        "SELEC name FROM products",
        "SELECT * FROM orders WAITFOR DELAY '00:00:10'",
        "WAITFOR DELAY '00:00:05'",
        "BULK INSERT orders FROM 'c:\\x.csv'",
        "SELECT 1 SELECT 2",
        "select 1 go select 2",
        "SELECT (",
        "SELECT BulkColumn FROM OPENROWSET(BULK 'C:\\secrets.txt', SINGLE_CLOB) AS f",
    ],
)
def test_rejects_unparseable(sql):
    result, _ = run(sql)
    assert result.status is Status.BLOCK


@pytest.mark.parametrize(
    "char",
    [chr(0x200B), chr(0x202E), chr(0xFEFF), chr(0x00), chr(0x2066), chr(0x00AD)],
    ids=["zwsp", "rlo", "bom", "nul", "lri", "soft-hyphen"],
)
def test_rejects_hidden_characters(char):
    result, _ = run(f"SELECT name{char} FROM products")
    assert result.status is Status.BLOCK
    assert "hidden/control character" in result.message


def test_size_cap():
    sql = "SELECT " + ", ".join(f"{i} AS c{i}" for i in range(2000))
    result, _ = run(sql, max_sql_chars=1000)
    assert result.status is Status.BLOCK
    assert "limit is 1000" in result.message


def test_parse_failure_is_fail_closed_on_odd_exceptions(monkeypatch):
    # sqlglot can raise more than ParseError on hostile input (AttributeError has
    # been seen when rendering odd trees). Whatever it raises, the answer is BLOCK.
    import nl2sql.guardrails.parse as parse_mod

    def boom(*args, **kwargs):
        raise AttributeError("'Delete' object has no attribute 'selects'")

    monkeypatch.setattr(parse_mod.sqlglot, "parse", boom)
    result, _ = run("SELECT 1")
    assert result.blocked
    assert "could not parse" in result.message
