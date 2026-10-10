"""(h) result redaction."""

from __future__ import annotations

from nl2sql.db.base import ResultSet
from nl2sql.guardrails import REDACTED, Status, redact
from nl2sql.semantic import SemanticLayer, from_dict


def rs(columns, rows):
    return ResultSet(columns=columns, rows=rows)


def test_nothing_to_redact():
    out, check = redact(rs(["name"], [("a",)]), SemanticLayer())
    assert out.rows == [("a",)]
    assert check.status is Status.PASS


def test_pattern_columns_redacted():
    out, check = redact(rs(["name", "salary", "ssn"], [("a", 100, "900-11-2222")]), SemanticLayer())
    assert out.rows == [("a", REDACTED, REDACTED)]
    assert check.status is Status.REWRITE
    assert "salary" in check.message and "ssn" in check.message


def test_semantic_layer_sensitive_column_redacted():
    layer = from_dict({"tables": {"customers": {"columns": {"email": {"sensitive": True}}}}})
    out, _ = redact(rs(["first_name", "email"], [("a", "a@x")]), layer, ["customers"])
    assert out.rows == [("a", REDACTED)]


def test_denied_columns_entry_redacted():
    layer = from_dict({"policy": {"denied_columns": ["notes"]}})
    out, _ = redact(rs(["notes"], [("secret",)]), layer)
    assert out.rows == [(REDACTED,)]


def test_qualified_result_column_names():
    out, _ = redact(rs(["e.base_salary"], [(1,)]), SemanticLayer())
    assert out.rows == [(REDACTED,)]


def test_original_result_not_mutated():
    original = rs(["salary"], [(1,)])
    redact(original, SemanticLayer())
    assert original.rows == [(1,)]
