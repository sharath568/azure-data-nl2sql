"""Layered guardrails for generated SQL.

Static layers, in order (``check()`` runs these; they need no database):

    a  parse             sqlglot T-SQL parse, hidden characters, size cap
    b  single_statement  exactly one statement
    c  select_only       SELECT/CTE/UNION only; nothing that writes, executes or escapes
    d  allow_list        tables and columns from the semantic layer; sensitive columns
    e  row_limit         inject / clamp TOP or FETCH
    f  cost              join/nesting/cartesian checks; optimizer estimate on SQL Server

Runtime layers (the engine applies these around execution):

    g  least_privilege   connection must be read-only; timeout; always rollback
    h  redaction         mask sensitive-looking result columns
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nl2sql.guardrails.allowlist import AllowListGuard
from nl2sql.guardrails.base import CheckResult, Guardrail, GuardState, Status
from nl2sql.guardrails.cost import CostGuard
from nl2sql.guardrails.parse import ParseGuard
from nl2sql.guardrails.privileges import LeastPrivilegeGuard
from nl2sql.guardrails.read_only import ReadOnlyGuard
from nl2sql.guardrails.redaction import REDACTED, redact
from nl2sql.guardrails.row_limit import RowLimitGuard
from nl2sql.guardrails.single_statement import SingleStatementGuard
from nl2sql.semantic import SemanticLayer

if TYPE_CHECKING:
    from nl2sql.db.base import Backend
    from nl2sql.schema import Schema

__all__ = [
    "REDACTED",
    "AllowListGuard",
    "CheckResult",
    "CostGuard",
    "GuardReport",
    "Guardrail",
    "LeastPrivilegeGuard",
    "ParseGuard",
    "ReadOnlyGuard",
    "RowLimitGuard",
    "SingleStatementGuard",
    "Status",
    "check",
    "default_guards",
    "redact",
]


def default_guards(*, runtime: bool = False) -> list[Guardrail]:
    guards: list[Guardrail] = [
        ParseGuard(),
        SingleStatementGuard(),
        ReadOnlyGuard(),
        AllowListGuard(),
        RowLimitGuard(),
        CostGuard(),
    ]
    if runtime:
        guards.append(LeastPrivilegeGuard())
    return guards


@dataclass
class GuardReport:
    original_sql: str
    sql: str
    checks: list[CheckResult] = field(default_factory=list)
    not_run: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return not any(c.blocked for c in self.checks)

    @property
    def blocked_by(self) -> str | None:
        return next((c.name for c in self.checks if c.blocked), None)

    @property
    def reason(self) -> str:
        return next((c.message for c in self.checks if c.blocked), "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "blocked_by": self.blocked_by,
            "original_sql": self.original_sql,
            "sql": self.sql if self.allowed else None,
            "checks": [c.to_dict() for c in self.checks],
            "not_run": self.not_run,
        }


def check(
    sql: str,
    layer: SemanticLayer | None = None,
    *,
    schema: Schema | None = None,
    backend: Backend | None = None,
    guards: list[Guardrail] | None = None,
) -> GuardReport:
    """Run the guardrails over ``sql``. Stops at the first BLOCK."""
    layer = layer or SemanticLayer()
    guards = guards if guards is not None else default_guards(runtime=backend is not None)
    state = GuardState(sql=sql, layer=layer, schema=schema, backend=backend)
    report = GuardReport(original_sql=sql, sql=sql)
    for i, guard in enumerate(guards):
        try:
            result = guard.run(state)
        except Exception as e:  # a guard crashing must never mean "allowed"
            result = CheckResult(guard.name, Status.BLOCK, f"guard error: {type(e).__name__}: {e}")
        report.checks.append(result)
        if result.blocked:
            report.not_run = [g.name for g in guards[i + 1 :]]
            break
    report.sql = state.sql
    return report
