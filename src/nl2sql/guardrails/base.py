"""Shared types for guardrails.

Each guardrail is a small class with a ``name``, a ``layer`` letter (matching
the README diagram) and ``run(state) -> CheckResult``. Guardrails run in order;
the first BLOCK stops the pipeline. A guardrail may REWRITE the query (the row
limit does), in which case it updates ``state.tree`` and ``state.sql``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from sqlglot import exp

if TYPE_CHECKING:
    from nl2sql.db.base import Backend
    from nl2sql.schema import Schema
    from nl2sql.semantic import SemanticLayer

DIALECT = "tsql"


class Status(str, Enum):
    PASS = "pass"
    REWRITE = "rewrite"
    WARN = "warn"
    SKIP = "skip"
    BLOCK = "block"


@dataclass
class CheckResult:
    name: str
    status: Status
    message: str = ""
    details: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.status is Status.BLOCK

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
        }
        if self.details:
            d["details"] = self.details
        return d


@dataclass
class GuardState:
    sql: str
    layer: SemanticLayer
    schema: Schema | None = None
    backend: Backend | None = None
    statements: list[exp.Expression] = field(default_factory=list)
    tree: exp.Expression | None = None
    tokens: list[Any] = field(default_factory=list)

    @property
    def policy(self):
        return self.layer.policy

    def render(self) -> str:
        assert self.tree is not None
        return self.tree.sql(dialect=DIALECT)


class Guardrail:
    name: str = "guardrail"
    layer: str = "?"
    description: str = ""

    def run(self, state: GuardState) -> CheckResult:  # pragma: no cover - interface
        raise NotImplementedError

    # helpers
    def ok(self, message: str = "", details: list[str] | None = None) -> CheckResult:
        return CheckResult(self.name, Status.PASS, message, details or [])

    def block(self, message: str, details: list[str] | None = None) -> CheckResult:
        return CheckResult(self.name, Status.BLOCK, message, details or [])

    def warn(self, message: str, details: list[str] | None = None) -> CheckResult:
        return CheckResult(self.name, Status.WARN, message, details or [])

    def skip(self, message: str) -> CheckResult:
        return CheckResult(self.name, Status.SKIP, message)

    def rewrite(self, message: str, details: list[str] | None = None) -> CheckResult:
        return CheckResult(self.name, Status.REWRITE, message, details or [])


def cte_names(tree: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}


def base_tables(tree: exp.Expression) -> list[exp.Table]:
    """Table nodes that refer to real tables (not CTEs, not table functions)."""
    ctes = cte_names(tree)
    out = []
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            continue
        if not t.args.get("db") and t.name.lower() in ctes:
            continue
        out.append(t)
    return out
