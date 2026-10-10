"""Backend interface. Generated SQL is always T-SQL; a backend runs it natively
(SQL Server / Azure SQL) or translates it first (SQLite, offline)."""

from __future__ import annotations

import datetime as dt
import decimal
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nl2sql.schema import Schema


class QueryTimeout(RuntimeError):
    """The statement ran longer than the configured timeout."""


class BackendError(RuntimeError):
    """The database refused or failed to run the statement."""


@dataclass
class ResultSet:
    columns: list[str]
    rows: list[tuple]
    truncated: bool = False
    elapsed_ms: float = 0.0
    executed_sql: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": [[jsonable(v) for v in r] for r in self.rows],
            "truncated": self.truncated,
            "elapsed_ms": round(self.elapsed_ms, 2),
        }


@dataclass
class CostEstimate:
    estimated_rows: float
    estimated_cost: float
    source: str


@dataclass
class PrivilegeReport:
    read_only: bool
    principal: str
    findings: list[str] = field(default_factory=list)
    enforced_by: str = ""


def jsonable(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return v


class Backend(ABC):
    name: str = "backend"
    #: dialect the backend natively speaks (for sqlglot)
    native_dialect: str = "tsql"

    @abstractmethod
    def introspect(
        self,
        *,
        sample_values: bool = False,
        schemas: list[str] | None = None,
        sample_filter: Callable[[str, str], bool] | None = None,
    ) -> Schema:
        """Read tables, columns, keys. Optionally low-cardinality sample values
        for columns where ``sample_filter(table, column)`` is true."""

    @abstractmethod
    def execute(self, sql: str, *, max_rows: int, timeout_s: float) -> ResultSet:
        """Run one T-SQL query. Fetches at most ``max_rows`` rows."""

    def estimate_cost(self, sql: str) -> CostEstimate | None:
        """Planner estimate without running the query, or None if unsupported."""
        return None

    @abstractmethod
    def privilege_report(self) -> PrivilegeReport:
        """Can the connected principal write anything?"""

    def privileges(self) -> PrivilegeReport:
        """``privilege_report()``, computed once per connection."""
        cached = getattr(self, "_privileges", None)
        if cached is None:
            cached = self._privileges = self.privilege_report()
        return cached

    def close(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Release the connection. Backends override this."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
