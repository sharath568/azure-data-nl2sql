"""Database backends. ``open_backend("sqlite:path")`` or ``open_backend("mssql:<odbc string>")``."""

from __future__ import annotations

from nl2sql.db.base import (
    Backend,
    BackendError,
    CostEstimate,
    PrivilegeReport,
    QueryTimeout,
    ResultSet,
)

__all__ = [
    "Backend",
    "BackendError",
    "CostEstimate",
    "PrivilegeReport",
    "QueryTimeout",
    "ResultSet",
    "open_backend",
    "parse_target",
]


def parse_target(target: str) -> tuple[str, str]:
    """``sqlite:path`` -> ("sqlite", path); ``mssql:...`` -> ("mssql", conn string).

    A bare path ending in .db/.sqlite is treated as SQLite. A string that looks
    like an ODBC connection string (has ``Server=``) is treated as SQL Server.
    """
    target = target.strip()
    lower = target.lower()
    if lower.startswith("sqlite:"):
        return "sqlite", target[len("sqlite:") :].removeprefix("///")
    for prefix in ("mssql:", "sqlserver:", "azuresql:"):
        if lower.startswith(prefix):
            return "mssql", target[len(prefix) :]
    if "server=" in lower:
        return "mssql", target
    if lower.endswith((".db", ".sqlite", ".sqlite3")):
        return "sqlite", target
    raise ValueError(
        f"don't know how to open {target!r}. Use sqlite:<path> or mssql:<connection string>."
    )


def open_backend(target: str) -> Backend:
    kind, value = parse_target(target)
    if kind == "sqlite":
        from nl2sql.db.sqlite import SQLiteBackend

        return SQLiteBackend(value)
    from nl2sql.db.mssql import MSSQLBackend

    return MSSQLBackend(value)
