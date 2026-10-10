"""(c) SELECT-only guard."""

from __future__ import annotations

import pytest

from nl2sql import guardrails
from nl2sql.semantic import Policy, SemanticLayer


def blocked_by(sql: str, layer=None) -> str | None:
    return guardrails.check(sql, layer).blocked_by


ALLOWED = [
    "SELECT name FROM products",
    "SELECT TOP 5 name FROM products ORDER BY list_price DESC",
    "WITH x AS (SELECT product_id FROM products) SELECT product_id FROM x",
    "WITH a AS (SELECT 1 AS n), b AS (SELECT n FROM a) SELECT n FROM b",
    "SELECT name FROM products WHERE product_id IN (SELECT product_id FROM order_items)",
    "SELECT p.name FROM products p WHERE EXISTS "
    "(SELECT 1 FROM order_items oi WHERE oi.product_id = p.product_id "
    "AND oi.order_id IN (SELECT order_id FROM orders WHERE status = 'shipped'))",
    "SELECT name FROM products UNION SELECT first_name FROM customers",
    "SELECT name FROM products INTERSECT SELECT name FROM products",
    "SELECT name FROM products EXCEPT SELECT name FROM products WHERE discontinued = 1",
    "SELECT category, COUNT(*) AS n FROM products GROUP BY category HAVING COUNT(*) > 2",
    "SELECT name, RANK() OVER (PARTITION BY category ORDER BY list_price DESC) AS r FROM products",
    "SELECT name FROM products WITH (NOLOCK)",
    "SELECT name FROM products ORDER BY name OFFSET 0 ROWS FETCH NEXT 5 ROWS ONLY",
    "SELECT 'DELETE FROM orders' AS just_a_string",
    "SELECT N'-- not a comment' AS s",
    "SELECT [select] = 1",
    "SELECT name FROM products FOR JSON PATH",
    "SELECT CASE WHEN list_price > 100 THEN 'big' ELSE 'small' END AS size FROM products",
    "(SELECT name FROM products)",
    "SELECT DATEADD(day, -30, '2025-12-31') AS d",
    "SELECT value FROM STRING_SPLIT('a,b', ',')",
    "SELECT [key], [value] FROM OPENJSON(N'[1, 2]')",
    "SELECT o.order_id FROM orders o CROSS APPLY (SELECT TOP 1 oi.product_id FROM order_items oi "
    "WHERE oi.order_id = o.order_id) x",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql):
    report = guardrails.check(sql)
    names = {c.name: c for c in report.checks}
    assert not names["select_only"].blocked, names["select_only"].details


DENIED = [
    ("INSERT INTO orders (order_id) VALUES (1)", "INSERT"),
    ("UPDATE products SET list_price = 0", "UPDATE"),
    ("DELETE FROM orders", "DELETE"),
    ("MERGE INTO a USING b ON a.id = b.id WHEN MATCHED THEN DELETE;", "MERGE"),
    ("DROP TABLE orders", "DROP"),
    ("TRUNCATE TABLE orders", "TRUNCATE"),
    ("CREATE TABLE x (id INT)", "CREATE"),
    ("ALTER TABLE orders ADD x INT", "ALTER"),
    ("GRANT SELECT ON orders TO public", "GRANT"),
    ("EXEC('DROP TABLE x')", "EXECUTE"),
    ("EXEC xp_cmdshell 'dir'", "EXECUTE"),
    ("EXECUTE sp_executesql N'SELECT 1'", "EXECUTE"),
    ("SET SHOWPLAN_XML ON", "SET"),
    ("SHUTDOWN", "SELECT"),
    ("RECONFIGURE", "SELECT"),
    ("DBCC CHECKDB", "SELECT"),
    ("SELECT * INTO #t FROM orders", "INTO"),
    ("SELECT * INTO backup_orders FROM orders", "INTO"),
    ("SELECT 1 UNION SELECT 2 INTO #t", "INTO"),
    ("SELECT * FROM #t", "temp table"),
    ("SELECT * FROM ##global_t", "temp table"),
    ("SELECT * FROM @tv", "table variable"),
    ("SELECT * FROM OPENROWSET('SQLNCLI', 'Server=x;', 'SELECT 1')", "OPENROWSET"),
    ("SELECT * FROM OPENQUERY(linked, 'SELECT 1')", "OPENQUERY"),
    ("SELECT * FROM OPENDATASOURCE('SQLNCLI', 'x').db.dbo.t", "OPENDATASOURCE"),
    ("SELECT * FROM srv.otherdb.dbo.orders", "linked-server"),
    ("SELECT * FROM otherdb.dbo.orders", "cross-database"),
    ("SELECT @@VERSION", "variables"),
    ("SELECT @x", "variables"),
    # sqlglot normalises SUSER_SNAME() to CURRENT_USER; either name in the message is fine
    ("SELECT SUSER_SNAME()", "SUSER_SNAME|CURRENT_USER"),
    ("SELECT SERVERPROPERTY('MachineName')", "SERVERPROPERTY"),
    ("SELECT HAS_PERMS_BY_NAME(NULL, NULL, 'CONTROL SERVER')", "HAS_PERMS_BY_NAME"),
    ("SELECT OBJECT_DEFINITION(OBJECT_ID('x'))", "OBJECT_"),
    ("SELECT name FROM products -- trailing comment", "comment"),
    ("SELECT name /* hi */ FROM products", "comment"),
    ("SELECT name FROM products WITH (UPDLOCK, HOLDLOCK)", "UPDLOCK"),
    ("SELECT name FROM products WITH (TABLOCKX)", "TABLOCKX"),
    ("SELECT fn_my_thing(1)", "FN_MY_THING"),
    ("SELECT * FROM xp_dirtree", "xp_dirtree"),
    # user-defined functions: ownership chaining can read denied columns
    ("SELECT dbo.get_pay(1) AS x", "user-defined function"),
    ("SELECT * FROM dbo.get_all_staff()", "table-valued function"),
    ("SELECT x.a FROM get_all_staff() AS x", "table-valued function"),
    ("SELECT * FROM dbo.STRING_SPLIT('a,b', ',')", "table-valued function"),
    ("SELECT NEXT VALUE FOR dbo.order_seq", "NEXTVALUEFOR"),
]


@pytest.mark.parametrize(("sql", "fragment"), DENIED)
def test_denied(sql, fragment):
    report = guardrails.check(sql)
    assert not report.allowed
    assert report.blocked_by in ("select_only", "parse", "single_statement"), report.reason
    if report.blocked_by == "select_only":
        joined = " ".join([*report.checks[-1].details, report.reason]).upper()
        assert any(f.upper() in joined for f in fragment.split("|"))


@pytest.mark.parametrize(
    "sql",
    [
        "sElEcT name FrOm products; dElEtE FrOm orders",
        "SELECT name FROM products;DELETE FROM orders",
        "SELECT name FROM products\n;\nDROP TABLE orders",
        "delete from orders",
        "DeLeTe   FROM\torders",
        "SELECT 1 /* ; DROP TABLE x */",
        "SELECT 1 /* /* nested */ ; DROP TABLE x */",
        "SELECT 1 --\nDROP TABLE x",
    ],
)
def test_case_whitespace_and_comment_tricks(sql):
    assert not guardrails.check(sql).allowed


@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (DELETE FROM orders OUTPUT deleted.*) SELECT * FROM x",
        "WITH x AS (UPDATE products SET list_price = 0 OUTPUT inserted.*) SELECT * FROM x",
        "WITH x AS (INSERT INTO orders (order_id) OUTPUT inserted.* VALUES (1)) SELECT * FROM x",
        "SELECT * FROM (DELETE FROM orders OUTPUT deleted.*) AS d",
    ],
)
def test_dml_hidden_in_cte_or_derived_table(sql):
    report = guardrails.check(sql)
    assert not report.allowed
    assert report.blocked_by in ("select_only", "parse")


def test_comments_can_be_allowed_by_policy():
    layer = SemanticLayer(policy=Policy(allow_comments=True))
    report = guardrails.check("SELECT name /* ok */ FROM products", layer)
    assert report.allowed


def test_root_must_be_select_even_if_sqlglot_parses_a_bare_word():
    # sqlglot parses a bare SHUTDOWN as a column reference; the root check catches it.
    report = guardrails.check("SHUTDOWN")
    assert report.blocked_by == "select_only"
    assert "only SELECT" in report.reason


def test_forbidden_keyword_list_covers_reserved_write_words():
    from nl2sql.guardrails.read_only import FORBIDDEN_KEYWORDS

    for word in ("INSERT", "UPDATE", "DELETE", "MERGE", "DROP", "EXEC", "INTO", "WAITFOR", "BULK"):
        assert word in FORBIDDEN_KEYWORDS


def test_quoted_identifier_named_like_a_keyword_is_fine():
    report = guardrails.check("SELECT [delete] FROM products")
    names = {c.name: c for c in report.checks}
    assert not names["select_only"].blocked
