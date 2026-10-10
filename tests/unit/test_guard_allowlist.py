"""(d) table/column allow-list."""

from __future__ import annotations

import pytest

from nl2sql import guardrails
from nl2sql.semantic import Policy, SemanticLayer, from_dict


def check(sql, layer, schema=None):
    return guardrails.check(sql, layer, schema=schema)


ALLOWED = [
    "SELECT first_name, last_name, title FROM employees",
    "SELECT COUNT(*) FROM employees",
    "SELECT department, COUNT(*) AS n FROM employees GROUP BY department",
    "SELECT e.first_name, m.first_name AS manager FROM employees e "
    "LEFT JOIN employees m ON m.employee_id = e.manager_id",
    "SELECT c.first_name, o.order_id FROM customers c "
    "JOIN orders o ON o.customer_id = c.customer_id",
    "SELECT * FROM orders",
    "SELECT o.* FROM orders o JOIN customers c ON c.customer_id = o.customer_id",
    "SELECT * FROM products p",
    "WITH e AS (SELECT employee_id, first_name FROM employees) SELECT * FROM e",
    "SELECT * FROM (SELECT first_name FROM employees) AS x",
    "SELECT dbo.products.name FROM dbo.products",
    "SELECT [name] FROM [dbo].[products]",
    "SELECT first_name FROM customers UNION SELECT first_name FROM employees",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql, layer, schema):
    report = check(sql, layer, schema)
    assert report.allowed, report.to_dict()


DENIED = [
    # sensitive columns, directly
    ("SELECT salary FROM employees", "salary"),
    ("SELECT first_name, ssn FROM employees", "ssn"),
    ("SELECT email FROM customers", "email"),
    ("SELECT e.[salary] FROM employees e", "salary"),
    ("SELECT [SALARY] FROM [dbo].[employees]", "salary"),
    ("SELECT AVG(salary) FROM employees", "salary"),
    # inference: never selected, still leaks
    ("SELECT first_name FROM employees WHERE salary > 150000", "salary"),
    ("SELECT first_name FROM employees ORDER BY salary DESC", "salary"),
    ("SELECT department FROM employees GROUP BY department HAVING MAX(salary) > 100000", "salary"),
    ("SELECT first_name, CASE WHEN salary > 100000 THEN 'hi' END AS band FROM employees", "salary"),
    # aliasing / nesting does not hide it
    ("SELECT x.s FROM (SELECT salary AS s FROM employees) x", "salary"),
    ("WITH pay AS (SELECT ssn AS id FROM employees) SELECT id FROM pay", "ssn"),
    ("SELECT (SELECT TOP 1 ssn FROM employees) AS leak", "ssn"),
    ("SELECT first_name FROM customers WHERE email LIKE '%@example.com'", "email"),
    # UNION-based exfiltration
    ("SELECT first_name FROM customers UNION SELECT ssn FROM employees", "ssn"),
    (
        "SELECT name FROM products UNION ALL SELECT CAST(salary AS NVARCHAR(20)) FROM employees",
        "salary",
    ),
    ("SELECT name FROM products UNION SELECT email FROM customers", "email"),
    # SELECT * expansion
    ("SELECT * FROM employees", "SELECT *"),
    ("SELECT e.* FROM employees e", "SELECT *"),
    (
        "SELECT o.order_id, e.* FROM orders o JOIN employees e ON e.employee_id = o.employee_id",
        "SELECT *",
    ),
    ("SELECT * FROM customers", "SELECT *"),
    ("WITH x AS (SELECT * FROM employees) SELECT first_name FROM x", "SELECT *"),
    ("SELECT BINARY_CHECKSUM(*) FROM employees", "SELECT *"),
    ("SELECT * FROM orders o JOIN customers c ON c.customer_id = o.customer_id", "SELECT *"),
    # tables
    ("SELECT name FROM sys.tables", "system catalog"),
    ("SELECT name, password_hash FROM sys.sql_logins", "system catalog"),
    ("SELECT * FROM INFORMATION_SCHEMA.COLUMNS", "system catalog"),
    ("SELECT name FROM sysobjects", "system catalog"),
    ("SELECT name FROM hr.payroll", "schema 'hr'"),
    ("SELECT * FROM audit_log", "allow-list"),
    # CTE shadowing a real table
    ("WITH products AS (SELECT 1 AS name) SELECT name FROM products", "shadows"),
]


@pytest.mark.parametrize(("sql", "fragment"), DENIED)
def test_denied(sql, fragment, layer, schema):
    report = check(sql, layer, schema)
    assert report.blocked_by == "allow_list", report.to_dict()
    assert fragment.lower() in " ".join([*report.checks[-1].details, report.reason]).lower()


def test_patterns_apply_without_a_schema():
    # No schema, no semantic layer: the default *salary* / *ssn* patterns still apply.
    report = guardrails.check("SELECT base_salary FROM payroll")
    assert report.blocked_by == "allow_list"


def test_no_schema_means_table_check_is_a_warning():
    report = guardrails.check("SELECT name FROM whatever")
    allow = next(c for c in report.checks if c.name == "allow_list")
    assert allow.status.value == "warn"
    assert report.allowed


def test_allowed_tables_from_policy_without_schema():
    layer = SemanticLayer(policy=Policy(allowed_tables=["orders"]))
    assert guardrails.check("SELECT order_id FROM orders", layer).allowed
    assert guardrails.check("SELECT name FROM products", layer).blocked_by == "allow_list"


def test_denied_tables_pattern():
    layer = from_dict({"policy": {"denied_tables": ["audit_*"]}})
    assert guardrails.check("SELECT id FROM audit_log", layer).blocked_by == "allow_list"


def test_denied_columns_by_qualified_name(schema):
    layer = from_dict({"policy": {"denied_columns": ["products.list_price"]}})
    report = guardrails.check("SELECT name, list_price FROM products", layer, schema=schema)
    assert report.blocked_by == "allow_list"
    report = guardrails.check("SELECT * FROM products", layer, schema=schema)
    assert report.blocked_by == "allow_list"


def test_sensitive_flag_in_semantic_layer(schema):
    layer = from_dict({"tables": {"products": {"columns": {"category": {"sensitive": True}}}}})
    report = guardrails.check("SELECT category FROM products", layer, schema=schema)
    assert report.blocked_by == "allow_list"


def test_count_star_over_sensitive_table_is_fine(layer, schema):
    assert check("SELECT COUNT(*) AS n FROM employees", layer, schema).allowed


def test_star_over_cte_built_from_safe_columns_is_fine(layer, schema):
    sql = "WITH safe AS (SELECT first_name, title FROM employees) SELECT * FROM safe"
    assert check(sql, layer, schema).allowed
