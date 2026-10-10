"""SQL Server / Azure SQL Database backend.

Uses ``mssql-python`` (Microsoft's driver, no ODBC install needed) when it is
installed, otherwise ``pyodbc`` with ODBC Driver 18. Choose explicitly with
``NL2SQL_MSSQL_DRIVER=mssql-python|pyodbc``.

Connect as a least-privilege principal (see ``sql/mssql/04_permissions.sql``).
``privilege_report()`` checks that, and the engine refuses to run generated SQL
over a connection that can write unless policy says otherwise.
"""

from __future__ import annotations

import contextlib
import os
import re
import time
from collections.abc import Callable

from nl2sql.db.base import (
    Backend,
    BackendError,
    CostEstimate,
    PrivilegeReport,
    QueryTimeout,
    ResultSet,
)
from nl2sql.schema import Column, ForeignKey, Schema, Table

_GO = re.compile(r"^\s*GO\s*(?:--.*)?$", re.IGNORECASE | re.MULTILINE)
_VAR = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)")


def split_batches(script: str) -> list[str]:
    """Split a sqlcmd-style script on ``GO`` lines."""
    return [b.strip() for b in _GO.split(script) if b.strip()]


def substitute(script: str, variables: dict[str, str]) -> str:
    """Replace sqlcmd ``$(NAME)`` variables. Unknown names are an error.

    Values are pasted into the script text (usually inside N'...'), so a value
    with a single quote is rejected rather than escaped behind your back.
    """

    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name not in variables:
            raise KeyError(f"script variable $({name}) is not set")
        value = variables[name]
        if "'" in value:
            raise ValueError(f"script variable $({name}) must not contain a single quote")
        return value

    return _VAR.sub(repl, script)


def _strip_driver(conn_str: str) -> str:
    parts = [
        p for p in conn_str.split(";") if p.strip() and not p.strip().lower().startswith("driver=")
    ]
    return ";".join(parts)


def connect(conn_str: str, *, autocommit: bool = False, driver: str | None = None):
    """Open a DB-API connection with whichever driver is available."""
    driver = (driver or os.environ.get("NL2SQL_MSSQL_DRIVER", "auto")).lower()
    if driver in ("auto", "mssql-python", "mssql_python"):
        try:
            import mssql_python  # type: ignore[import-not-found]
        except ImportError:
            if driver != "auto":
                raise
        else:
            return mssql_python.connect(
                _strip_driver(conn_str), autocommit=autocommit
            ), "mssql-python"
    try:
        import pyodbc  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "No SQL Server driver installed. pip install 'nl2sql[mssql]' (mssql-python) "
            "or 'nl2sql[pyodbc]' plus Microsoft ODBC Driver 18."
        ) from e
    if "driver=" not in conn_str.lower():
        conn_str = "Driver={ODBC Driver 18 for SQL Server};" + conn_str
    return pyodbc.connect(conn_str, autocommit=autocommit), "pyodbc"


def _is_timeout(err: Exception) -> bool:
    text = str(err).lower()
    return "hyt00" in text or "timeout expired" in text or "query timeout" in text


_TYPE_WITH_LENGTH = {"char", "varchar", "nchar", "nvarchar", "binary", "varbinary"}
_TEXT_TYPES = {"char", "varchar", "nchar", "nvarchar"}


def _format_type(name: str, max_length: int, precision: int, scale: int) -> str:
    name = name.lower()
    if name in _TYPE_WITH_LENGTH:
        if max_length == -1:
            return f"{name.upper()}(MAX)"
        length = max_length // 2 if name.startswith("n") else max_length
        return f"{name.upper()}({length})"
    if name in ("decimal", "numeric"):
        return f"{name.upper()}({precision},{scale})"
    return name.upper()


_INTROSPECT_COLUMNS = """
SELECT s.name, t.name, c.name, ty.name, c.max_length, c.precision, c.scale, c.is_nullable,
       CASE WHEN pk.column_id IS NULL THEN 0 ELSE 1 END AS is_pk
FROM sys.tables AS t
JOIN sys.schemas AS s ON s.schema_id = t.schema_id
JOIN sys.columns AS c ON c.object_id = t.object_id
JOIN sys.types AS ty ON ty.user_type_id = c.user_type_id
LEFT JOIN (
    SELECT ic.object_id, ic.column_id
    FROM sys.indexes AS i
    JOIN sys.index_columns AS ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
    WHERE i.is_primary_key = 1
) AS pk ON pk.object_id = c.object_id AND pk.column_id = c.column_id
WHERE t.is_ms_shipped = 0
ORDER BY s.name, t.name, c.column_id
"""

_INTROSPECT_FKS = """
SELECT ps.name, pt.name, pc.name, rs.name, rt.name, rc.name
FROM sys.foreign_key_columns AS fkc
JOIN sys.tables AS pt ON pt.object_id = fkc.parent_object_id
JOIN sys.schemas AS ps ON ps.schema_id = pt.schema_id
JOIN sys.columns AS pc ON pc.object_id = fkc.parent_object_id
    AND pc.column_id = fkc.parent_column_id
JOIN sys.tables AS rt ON rt.object_id = fkc.referenced_object_id
JOIN sys.schemas AS rs ON rs.schema_id = rt.schema_id
JOIN sys.columns AS rc ON rc.object_id = fkc.referenced_object_id
    AND rc.column_id = fkc.referenced_column_id
ORDER BY pt.name, pc.name
"""

_PRIVILEGES = """
SELECT
    SUSER_SNAME() AS login_name,
    USER_NAME() AS user_name,
    ISNULL(IS_SRVROLEMEMBER('sysadmin'), 0) AS is_sysadmin,
    ISNULL(IS_ROLEMEMBER('db_owner'), 0) AS is_db_owner,
    ISNULL(IS_ROLEMEMBER('db_datawriter'), 0) AS is_datawriter,
    ISNULL(IS_ROLEMEMBER('db_ddladmin'), 0) AS is_ddladmin,
    ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT'), 0) AS db_insert,
    ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'UPDATE'), 0) AS db_update,
    ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'DELETE'), 0) AS db_delete,
    ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER'), 0) AS db_alter,
    ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE'), 0) AS db_create_table,
    ISNULL(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'EXECUTE'), 0) AS db_execute
"""

_TABLE_WRITE_PERMS = """
SELECT s.name + '.' + t.name AS table_name, p.perm
FROM sys.tables AS t
JOIN sys.schemas AS s ON s.schema_id = t.schema_id
CROSS JOIN (VALUES ('INSERT'), ('UPDATE'), ('DELETE'), ('ALTER')) AS p(perm)
WHERE t.is_ms_shipped = 0
  AND HAS_PERMS_BY_NAME(QUOTENAME(s.name) + '.' + QUOTENAME(t.name), 'OBJECT', p.perm) = 1
ORDER BY table_name, p.perm
"""

_EST_ROWS = re.compile(r'StatementEstRows="([0-9.eE+-]+)"')
_EST_COST = re.compile(r'StatementSubTreeCost="([0-9.eE+-]+)"')


def parse_showplan(xml: str) -> CostEstimate | None:
    rows = _EST_ROWS.search(xml)
    cost = _EST_COST.search(xml)
    if not rows or not cost:
        return None
    return CostEstimate(float(rows.group(1)), float(cost.group(1)), "SET SHOWPLAN_XML")


class MSSQLBackend(Backend):
    name = "mssql"
    native_dialect = "tsql"

    def __init__(self, conn_str: str, *, driver: str | None = None):
        self.conn_str = conn_str
        self._driver_choice = driver
        self.conn, self.driver = connect(conn_str, autocommit=False, driver=driver)
        self._plan_conn = None  # opened on first estimate_cost()

    def _set_timeout(self, seconds: float) -> None:
        with contextlib.suppress(Exception):  # driver specific; best effort
            self.conn.timeout = max(1, round(seconds))

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        cur = self.conn.cursor()
        try:
            cur.execute(sql, params) if params else cur.execute(sql)
            return [tuple(r) for r in cur.fetchall()]
        finally:
            cur.close()
            self.conn.rollback()

    def introspect(
        self,
        *,
        sample_values: bool = False,
        schemas: list[str] | None = None,
        sample_filter: Callable[[str, str], bool] | None = None,
    ) -> Schema:
        allowed = {s.lower() for s in schemas} if schemas else None
        schema = Schema()
        for sname, tname, cname, tyname, max_len, prec, scale, nullable, is_pk in self._query(
            _INTROSPECT_COLUMNS
        ):
            if allowed is not None and sname.lower() not in allowed:
                continue
            table = schema.tables.setdefault(tname.lower(), Table(name=tname, schema=sname))
            table.columns.append(
                Column(
                    cname,
                    _format_type(tyname, max_len, prec, scale),
                    nullable=bool(nullable),
                    primary_key=bool(is_pk),
                )
            )
        for ps, pt, pc, rs, rt, rc in self._query(_INTROSPECT_FKS):
            if allowed is not None and (ps.lower() not in allowed or rs.lower() not in allowed):
                continue
            schema.foreign_keys.append(ForeignKey(pt, pc, rt, rc))
        if sample_values:
            self._add_samples(schema, sample_filter)
        return schema

    def _add_samples(self, schema: Schema, keep, max_distinct: int = 12) -> None:
        for table in schema.tables.values():
            for col in table.columns:
                base = col.data_type.split("(")[0].lower()
                if col.primary_key or base not in _TEXT_TYPES:
                    continue
                if keep is not None and not keep(table.name, col.name):
                    continue
                sql = (
                    f"SELECT DISTINCT TOP ({max_distinct + 1}) [{col.name}] "
                    f"FROM [{table.schema}].[{table.name}] WHERE [{col.name}] IS NOT NULL"
                )
                try:
                    rows = self._query(sql)
                except Exception:
                    continue  # e.g. column-level DENY; just skip samples
                if 0 < len(rows) <= max_distinct:
                    col.sample_values = sorted(str(r[0]) for r in rows)

    def execute(self, sql: str, *, max_rows: int, timeout_s: float) -> ResultSet:
        self._set_timeout(timeout_s)
        cur = self.conn.cursor()
        start = time.perf_counter()
        try:
            cur.execute(sql)
            if cur.description is None:
                raise BackendError("statement returned no result set")
            cols = [d[0] for d in cur.description]
            rows = [tuple(r) for r in cur.fetchmany(max_rows + 1)]
        except BackendError:
            raise
        except Exception as e:
            if _is_timeout(e):
                raise QueryTimeout(f"query exceeded {timeout_s:g}s timeout") from e
            raise BackendError(str(e)) from e
        finally:
            try:
                cur.close()
            finally:
                # Never commit. Even if a write slipped through every other
                # layer, it is rolled back here.
                self.conn.rollback()
        elapsed = (time.perf_counter() - start) * 1000
        return ResultSet(cols, rows[:max_rows], len(rows) > max_rows, elapsed, sql)

    def execute_raw(self, sql: str, *, timeout_s: float = 10) -> list[tuple]:
        """Run SQL with no guardrails. For tests that prove the engine enforces
        permissions on its own. Always rolled back."""
        self._set_timeout(timeout_s)
        cur = self.conn.cursor()
        try:
            cur.execute(sql)
            return [tuple(r) for r in cur.fetchall()] if cur.description else []
        except Exception as e:
            if _is_timeout(e):
                raise QueryTimeout(str(e)) from e
            raise BackendError(str(e)) from e
        finally:
            cur.close()
            self.conn.rollback()

    def estimate_cost(self, sql: str) -> CostEstimate | None:
        """Ask the optimizer for an estimate without running the query.

        Uses a second, autocommit connection so SET SHOWPLAN_XML never runs inside
        the main connection's transaction. Needs the SHOWPLAN permission (granted in
        sql/mssql/04_permissions.sql).
        """
        try:
            if self._plan_conn is None:
                self._plan_conn, _ = connect(
                    self.conn_str, autocommit=True, driver=self._driver_choice
                )
            cur = self._plan_conn.cursor()
            try:
                cur.execute("SET SHOWPLAN_XML ON")
                try:
                    cur.execute(sql)
                    rows = cur.fetchall()  # drain fully before the next statement
                finally:
                    cur.execute("SET SHOWPLAN_XML OFF")
            finally:
                cur.close()
        except Exception as e:
            raise BackendError(f"SHOWPLAN failed: {e}") from e
        return parse_showplan(str(rows[0][0])) if rows else None

    def privilege_report(self) -> PrivilegeReport:
        row = self._query(_PRIVILEGES)[0]
        login, user = row[0], row[1]
        labels = [
            "sysadmin", "db_owner", "db_datawriter", "db_ddladmin",
            "INSERT (database)", "UPDATE (database)", "DELETE (database)",
            "ALTER (database)", "CREATE TABLE (database)", "EXECUTE (database)",
        ]  # fmt: skip
        findings = [
            label for label, flag in zip(labels, row[2:], strict=True) if int(flag or 0) == 1
        ]
        for table, perm in self._query(_TABLE_WRITE_PERMS):
            findings.append(f"{perm} on {table}")
        return PrivilegeReport(
            read_only=not findings,
            principal=f"{login} (user {user})",
            findings=findings,
            enforced_by="SQL Server permissions (GRANT SELECT only, column DENY)",
        )

    def run_script(self, script: str, variables: dict[str, str] | None = None) -> int:
        """Run a GO-separated script with autocommit. Returns the batch count."""
        text = substitute(script, variables or {})
        batches = split_batches(text)
        conn, _ = connect(self.conn_str, autocommit=True)
        try:
            cur = conn.cursor()
            for batch in batches:
                cur.execute(batch)
                try:
                    while cur.nextset():  # drain any result sets
                        pass
                except Exception:  # some drivers raise when there is no next set
                    pass
            cur.close()
        finally:
            conn.close()
        return len(batches)

    def close(self) -> None:
        for conn in (self.conn, self._plan_conn):
            if conn is None:
                continue
            with contextlib.suppress(Exception):
                conn.close()
