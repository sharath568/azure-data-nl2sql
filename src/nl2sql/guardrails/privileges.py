"""(g) Least-privilege execution.

This is the layer that actually matters. Layers (a) to (f) are code that
inspects text; a bug or a parser quirk can let something through. The database
permission system cannot be talked out of a DENY.

Before running anything, the connection is checked:

* SQL Server / Azure SQL: is the principal sysadmin, db_owner, a member of
  db_datawriter / db_ddladmin, or does it hold INSERT / UPDATE / DELETE /
  ALTER / CREATE TABLE / EXECUTE anywhere? (``sql/mssql/04_permissions.sql``
  creates a principal that has none of those.)
* SQLite: is the connection opened ``mode=ro`` with ``PRAGMA query_only=ON``?

If the connection can write, generated SQL is not run, unless
``policy.allow_privileged_connection`` is set (it is off by default and you
should leave it off).

Execution also gets a statement timeout (``policy.timeout_seconds``) and the
transaction is always rolled back.
"""

from __future__ import annotations

from nl2sql.guardrails.base import CheckResult, Guardrail, GuardState


class LeastPrivilegeGuard(Guardrail):
    name = "least_privilege"
    layer = "g"
    description = "Connection must be read-only (checked live); statement timeout; always rollback."

    def run(self, state: GuardState) -> CheckResult:
        if state.backend is None:
            return self.skip("no database connection; checked at execution time")
        report = state.backend.privileges()
        timeout = f"timeout {state.policy.timeout_seconds}s"
        if report.read_only:
            return self.ok(f"{report.principal} is read-only ({report.enforced_by}); {timeout}")
        detail = [f"{report.principal} can write: " + ", ".join(report.findings[:8])]
        if state.policy.allow_privileged_connection:
            return self.warn(
                "connection can write; running anyway because "
                "policy.allow_privileged_connection is true",
                detail,
            )
        return self.block(
            "refusing to run generated SQL over a connection that can write. "
            "Connect as a read-only principal (see sql/mssql/04_permissions.sql)",
            detail,
        )
