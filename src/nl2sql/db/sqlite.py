"""Offline backend: SQLite, opened read-only.

Generated SQL is T-SQL (the target is Azure SQL). Here it is translated with
sqlglot (tsql -> sqlite) and a few T-SQL date functions are registered as
Python functions. That is good enough for the sample questions; it is not a
general T-SQL emulator.

The connection is opened with ``mode=ro`` and ``PRAGMA query_only = ON``, so
SQLite itself refuses writes even if every guardrail were bypassed. That is the
offline stand-in for a least-privilege SQL login.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel

from nl2sql.db.base import (
    Backend,
    BackendError,
    PrivilegeReport,
    QueryTimeout,
    ResultSet,
)
from nl2sql.schema import Column, ForeignKey, Schema, Table

_TEXT_TYPES = {"TEXT", "CHAR", "VARCHAR", "NCHAR", "NVARCHAR", ""}


def _date_part(part: str):
    def fn(value):
        if value is None:
            return None
        d = dt.date.fromisoformat(str(value)[:10])
        return getattr(d, part)

    return fn


def tsql_to_sqlite(sql: str) -> str:
    """Translate one T-SQL statement for SQLite. ``dbo.`` prefixes are dropped."""
    tree = sqlglot.parse_one(sql, read="tsql")
    for table in tree.find_all(exp.Table):
        if table.args.get("db") and table.db.lower() == "dbo" and not table.args.get("catalog"):
            table.set("db", None)
    # DATEDIFF(day, ...) translates fine but sqlglot logs a warning; keep output quiet.
    return tree.sql(dialect="sqlite", unsupported_level=ErrorLevel.IGNORE)


class SQLiteBackend(Backend):
    name = "sqlite"
    native_dialect = "sqlite"

    def __init__(self, path: str | Path, *, read_only: bool = True):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(
                f"SQLite database not found: {self.path}. Run `nl2sql init-sample` first."
            )
        self.read_only = read_only
        if read_only:
            uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self.conn.execute("PRAGMA query_only = ON")
        else:
            self.conn = sqlite3.connect(self.path, check_same_thread=False)
        for part in ("year", "month", "day"):
            self.conn.create_function(part.upper(), 1, _date_part(part), deterministic=True)

    # -- introspection -----------------------------------------------------

    def introspect(
        self,
        *,
        sample_values: bool = False,
        schemas: list[str] | None = None,
        sample_filter: Callable[[str, str], bool] | None = None,
    ) -> Schema:
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        schema = Schema()
        for (tname,) in cur.fetchall():
            table = Table(name=tname, schema="dbo")
            for _cid, cname, ctype, notnull, _default, pk in self.conn.execute(
                f'PRAGMA table_info("{tname}")'
            ):
                table.columns.append(
                    Column(
                        cname, (ctype or "TEXT").upper(), nullable=not notnull, primary_key=bool(pk)
                    )
                )
            for row in self.conn.execute(f'PRAGMA foreign_key_list("{tname}")'):
                schema.foreign_keys.append(ForeignKey(tname, row[3], row[2], row[4]))
            schema.tables[tname.lower()] = table
        if sample_values:
            self._add_samples(schema, sample_filter)
        return schema

    def _add_samples(self, schema: Schema, keep, max_distinct: int = 12) -> None:
        for table in schema.tables.values():
            for col in table.columns:
                base = col.data_type.split("(")[0].upper()
                if col.primary_key or base not in _TEXT_TYPES:
                    continue
                if keep is not None and not keep(table.name, col.name):
                    continue
                rows = self.conn.execute(
                    f'SELECT DISTINCT "{col.name}" FROM "{table.name}" '
                    f'WHERE "{col.name}" IS NOT NULL LIMIT {max_distinct + 1}'
                ).fetchall()
                if 0 < len(rows) <= max_distinct:
                    col.sample_values = sorted(str(r[0]) for r in rows)

    # -- execution ---------------------------------------------------------

    def execute(self, sql: str, *, max_rows: int, timeout_s: float) -> ResultSet:
        try:
            native = tsql_to_sqlite(sql)
        except Exception as e:  # sqlglot can raise more than ParseError
            raise BackendError(f"could not translate T-SQL for SQLite: {e}") from e
        return self.execute_native(native, max_rows=max_rows, timeout_s=timeout_s)

    def execute_native(self, sql: str, *, max_rows: int, timeout_s: float) -> ResultSet:
        deadline = time.monotonic() + timeout_s
        timed_out = False

        def progress() -> int:
            nonlocal timed_out
            if time.monotonic() > deadline:
                timed_out = True
                return 1  # non-zero aborts the statement
            return 0

        self.conn.set_progress_handler(progress, 10_000)
        start = time.perf_counter()
        try:
            cur = self.conn.execute(sql)
            rows = cur.fetchmany(max_rows + 1)
            cols = [d[0] for d in cur.description or []]
        except sqlite3.OperationalError as e:
            if timed_out:
                raise QueryTimeout(f"query exceeded {timeout_s:g}s timeout") from e
            raise BackendError(str(e)) from e
        except sqlite3.Error as e:
            raise BackendError(str(e)) from e
        finally:
            self.conn.set_progress_handler(None, 0)
            if self.conn.in_transaction:
                self.conn.rollback()
        elapsed = (time.perf_counter() - start) * 1000
        truncated = len(rows) > max_rows
        return ResultSet(cols, [tuple(r) for r in rows[:max_rows]], truncated, elapsed, sql)

    def privilege_report(self) -> PrivilegeReport:
        query_only = self.conn.execute("PRAGMA query_only").fetchone()[0]
        findings = []
        if not self.read_only:
            findings.append("connection opened read-write")
        if not query_only:
            findings.append("PRAGMA query_only is OFF")
        return PrivilegeReport(
            read_only=not findings,
            principal=f"sqlite:{self.path.name}",
            findings=findings,
            enforced_by="SQLite mode=ro + PRAGMA query_only=ON",
        )

    def close(self) -> None:
        self.conn.close()
