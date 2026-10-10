"""(c) SELECT only.

This is an allow-list on the *shape* of the statement plus a deny-list on what
can appear inside it:

* the statement root must be a SELECT or a set operation (UNION / INTERSECT /
  EXCEPT) of SELECTs, CTEs included. A bare ``SHUTDOWN`` parses as a column
  reference in sqlglot, so checking for "bad statement types" alone is not
  enough; checking for "good root types" is.
* no DML/DDL/EXEC/SET/DECLARE/transaction nodes anywhere in the tree
  (``WITH x AS (DELETE ...)`` and friends),
* no ``SELECT ... INTO``, temp tables (``#t``, ``##t``) or table variables,
* no ``OPENROWSET`` / ``OPENQUERY`` / ``OPENDATASOURCE`` / ``BULK``,
  ``xp_*`` / ``sp_*`` / ``fn_*`` procedures and functions, or system
  information functions (``SUSER_SNAME()``, ``SERVERPROPERTY()``...),
* no linked-server (4-part) or cross-database (3-part) names,
* no user-defined functions (``dbo.f(...)`` or ``FROM dbo.tvf()``): a function
  owned by dbo can read columns the caller is denied, because SQL Server skips
  permission checks along an unbroken ownership chain. ``OPENJSON``,
  ``STRING_SPLIT`` and ``GENERATE_SERIES`` are allowed,
* no ``NEXT VALUE FOR`` (it changes a sequence),
* no variables or ``@@`` globals,
* no locking hints other than the harmless ones,
* no comments (generated SQL has no reason to contain them, and they are a
  classic way to hide a second statement from a human reviewer).

As a second net, the raw token stream is checked for T-SQL reserved words that
never belong in a read-only query. They are reserved, so they cannot be
legitimate unquoted identifiers; this catches anything the AST walk misses if
a future sqlglot version parses something differently.
"""

from __future__ import annotations

import re

from sqlglot import exp
from sqlglot.tokens import TokenType

from nl2sql.guardrails.base import CheckResult, Guardrail, GuardState

_FORBIDDEN_NODE_NAMES = [
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterColumn",
    "TruncateTable", "Command", "Execute", "ExecuteSql", "StoredProcedure", "Grant",
    "Revoke", "Set", "Use", "Transaction", "Commit", "Rollback", "Declare", "Kill",
    "Copy", "LoadData", "Pragma", "Into", "IfBlock", "WhileBlock", "Return", "Comment",
    "Cache", "Uncache", "Refresh", "Describe", "Show", "Analyze", "Detach", "Attach",
    "Export", "NextValueFor",
]  # fmt: skip
FORBIDDEN_NODES: tuple[type, ...] = tuple(
    getattr(exp, n) for n in _FORBIDDEN_NODE_NAMES if hasattr(exp, n)
)

# T-SQL reserved words that have no place in a read-only query.
FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "CREATE", "ALTER", "DROP",
    "GRANT", "REVOKE", "DENY", "EXEC", "EXECUTE", "INTO", "BULK", "WAITFOR", "SHUTDOWN",
    "RECONFIGURE", "DBCC", "KILL", "BACKUP", "RESTORE", "DECLARE", "SET", "USE",
    "OPENROWSET", "OPENQUERY", "OPENDATASOURCE", "OPENXML", "WRITETEXT", "UPDATETEXT",
    "READTEXT", "CHECKPOINT", "SETUSER", "BEGIN", "COMMIT", "ROLLBACK", "SAVE",
    "TRAN", "TRANSACTION", "SYSTEM_USER", "SESSION_USER", "CURRENT_USER",
}  # fmt: skip

DENIED_FUNCTIONS = {
    "OPENROWSET", "OPENQUERY", "OPENDATASOURCE", "OPENXML",
    # who am I / where am I
    "SUSER_NAME", "SUSER_SNAME", "SUSER_ID", "SUSER_SID", "USER_NAME", "USER_ID",
    "ORIGINAL_LOGIN", "HOST_NAME", "HOST_ID", "APP_NAME", "CURRENT_USER", "SESSION_USER",
    "SYSTEM_USER", "SESSION_CONTEXT", "CONTEXT_INFO",
    # server and catalog metadata
    "SERVERPROPERTY", "DATABASEPROPERTYEX", "CONNECTIONPROPERTY", "SESSIONPROPERTY",
    "LOGINPROPERTY", "OBJECT_DEFINITION", "OBJECT_ID", "OBJECT_NAME", "OBJECT_SCHEMA_NAME",
    "COL_NAME", "COL_LENGTH", "DB_NAME", "DB_ID", "SCHEMA_NAME", "SCHEMA_ID", "FILE_NAME",
    "FILEPROPERTY", "HAS_PERMS_BY_NAME", "HAS_DBACCESS", "IS_MEMBER", "IS_ROLEMEMBER",
    "IS_SRVROLEMEMBER", "PERMISSIONS",
    # credentials
    "PWDENCRYPT", "PWDCOMPARE",
}  # fmt: skip
DENIED_FUNCTION_PREFIXES = ("XP_", "SP_", "FN_", "DECRYPTBY", "ENCRYPTBY", "SIGNBY")

# Built-in table-valued functions that only read their arguments. Anything else
# in a FROM clause that is a function call is a user-defined function, and a
# dbo-owned function can read denied columns through ownership chaining.
ALLOWED_TABLE_FUNCTIONS = {"OPENJSON", "STRING_SPLIT", "GENERATE_SERIES"}

_STRING_TOKENS = {
    getattr(TokenType, n)
    for n in ("STRING", "NATIONAL_STRING", "RAW_STRING", "BYTE_STRING", "HEX_STRING",
              "BIT_STRING", "HEREDOC_STRING", "UNICODE_STRING", "IDENTIFIER")
    if hasattr(TokenType, n)
}  # fmt: skip

# Strip string literals and [bracketed] identifiers before looking for comment markers.
_LITERALS = re.compile(r"N?'(?:[^']|'')*'|\[(?:[^\]]|\]\])*\]|\"(?:[^\"]|\"\")*\"")


def _func_names(node: exp.Func) -> set[str]:
    names = {node.sql_name().upper(), type(node).__name__.upper()}
    if isinstance(node, exp.Anonymous):
        names.add(str(node.name).upper())
    return names


class ReadOnlyGuard(Guardrail):
    name = "select_only"
    layer = "c"
    description = (
        "SELECT/CTE/set operations only; no DML/DDL/EXEC/INTO/temp tables/OPENROWSET/"
        "xp_/system functions/variables/comments/locking hints."
    )

    def run(self, state: GuardState) -> CheckResult:
        tree = state.tree
        assert tree is not None
        problems: list[str] = []

        root = tree
        while isinstance(root, (exp.Subquery, exp.Paren)):
            root = root.this
        if not isinstance(root, (exp.Select, exp.SetOperation)):
            return self.block(
                f"only SELECT queries are allowed (statement is {type(root).__name__.upper()})"
            )

        for node in tree.walk():
            if isinstance(node, FORBIDDEN_NODES):
                label = "SELECT ... INTO" if isinstance(node, exp.Into) else type(node).__name__
                problems.append(f"{label.upper()} is not allowed")
            elif isinstance(node, (exp.Parameter, exp.SessionParameter, exp.Placeholder)):
                problems.append(f"variables are not allowed ({node.sql(dialect='tsql')})")
            elif isinstance(node, exp.Func) and isinstance(node.parent, exp.Dot):
                problems.append(
                    "user-defined function calls are not allowed "
                    f"({node.parent.sql(dialect='tsql')})"
                )
            elif isinstance(node, exp.Func):
                names = _func_names(node)
                bad = names & DENIED_FUNCTIONS or {
                    n for n in names if n.startswith(DENIED_FUNCTION_PREFIXES)
                }
                if bad:
                    problems.append(f"function {sorted(bad)[0]}() is not allowed")
            elif isinstance(node, exp.Table):
                problems.extend(self._table_problems(node))
            elif isinstance(node, exp.WithTableHint):
                allowed = {h.upper() for h in state.policy.allowed_table_hints}
                for hint in node.expressions:
                    text = hint.sql(dialect="tsql").upper()
                    if text not in allowed:
                        problems.append(f"table hint {text} is not allowed")

        for tok in state.tokens:
            if tok.comments and not state.policy.allow_comments:
                problems.append("comments are not allowed in generated SQL")
            if tok.token_type in _STRING_TOKENS:
                continue
            word = tok.text.upper()
            if word in FORBIDDEN_KEYWORDS:
                problems.append(f"keyword {word} is not allowed")
            elif word.startswith("@"):
                problems.append(f"variables are not allowed ({tok.text})")
        if not state.policy.allow_comments:
            bare = _LITERALS.sub("''", state.sql)
            if "--" in bare or "/*" in bare:
                problems.append("comments are not allowed in generated SQL")

        problems = list(dict.fromkeys(problems))  # dedupe, keep order
        if problems:
            return self.block(problems[0], problems)
        return self.ok("read-only SELECT")

    @staticmethod
    def _table_problems(table: exp.Table) -> list[str]:
        out = []
        this = table.this
        if isinstance(this, exp.Parameter):
            out.append(f"table variables are not allowed ({table.sql(dialect='tsql')})")
        if isinstance(this, exp.Identifier) and (
            this.args.get("temporary") or this.args.get("global_") or this.name.startswith("#")
        ):
            out.append(f"temp tables are not allowed ({table.sql(dialect='tsql')})")
        if isinstance(this, exp.Func) and (
            table.args.get("db") or not _func_names(this) & ALLOWED_TABLE_FUNCTIONS
        ):
            out.append(
                f"table-valued function {table.sql(dialect='tsql')} is not allowed "
                "(user-defined functions can read restricted data through ownership chaining)"
            )
        if isinstance(this, exp.Dot):
            out.append(
                f"linked-server (4-part) names are not allowed ({table.sql(dialect='tsql')})"
            )
        elif table.args.get("catalog"):
            out.append(f"cross-database references are not allowed ({table.sql(dialect='tsql')})")
        if isinstance(this, exp.Identifier) and this.name.upper().startswith(
            DENIED_FUNCTION_PREFIXES
        ):
            out.append(f"object {this.name} is not allowed")
        return out
