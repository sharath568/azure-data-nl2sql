"""(h) Result redaction. Defense in depth, applied after execution.

If a result column is named like a denied column (``salary``, ``ssn``,
``*password*``...), its values are replaced with ``[REDACTED]``. With layers
(d) and (g) working this should never fire. It is here for the day one of them
does not, and for databases you connect to without a curated semantic layer.

It is name-based, so it cannot catch a denied value under an innocent alias.
That is what (d) and column-level DENY in (g) are for.
"""

from __future__ import annotations

from dataclasses import replace

from nl2sql.db.base import ResultSet
from nl2sql.guardrails.base import CheckResult, Status
from nl2sql.semantic import SemanticLayer

REDACTED = "[REDACTED]"


def redact(
    result: ResultSet, layer: SemanticLayer, tables: list[str] | None = None
) -> tuple[ResultSet, CheckResult]:
    tables = tables or []
    hits = []
    for i, col in enumerate(result.columns):
        name = col.split(".")[-1]
        if layer.is_column_denied(None, name) or any(
            layer.is_column_denied(t, name) for t in tables
        ):
            hits.append(i)
    if not hits:
        return result, CheckResult("redaction", Status.PASS, "no sensitive column names in result")
    rows = [tuple(REDACTED if i in hits else v for i, v in enumerate(row)) for row in result.rows]
    names = [result.columns[i] for i in hits]
    return replace(result, rows=rows), CheckResult(
        "redaction", Status.REWRITE, f"redacted column(s): {', '.join(names)}"
    )
