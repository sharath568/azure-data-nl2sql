"""(a) Parse. If sqlglot cannot parse it as T-SQL, nothing else can reason about
it, so it is rejected. Fail closed.

Also rejects invisible and control characters (zero-width spaces, bidi
overrides, NUL) that can make a query look different to a human reviewer than
to the server, and anything longer than ``policy.max_sql_chars``.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager

import sqlglot
from sqlglot.dialects.tsql import TSQL

from nl2sql.guardrails.base import DIALECT, CheckResult, Guardrail, GuardState

# C0 controls except tab/newline/CR, DEL, zero-width and bidi control characters, BOM.
_SUSPICIOUS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u00ad\u200b-\u200f\u202a-\u202e"
    r"\u2060-\u2064\u2066-\u2069\ufeff]"
)


@contextmanager
def quiet_sqlglot():
    """sqlglot logs a warning when it falls back to a generic Command node.
    That is expected for hostile input; the guard reports it instead."""
    logger = logging.getLogger("sqlglot")
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(previous)


class ParseGuard(Guardrail):
    name = "parse"
    layer = "a"
    description = "Must parse as T-SQL (sqlglot, tsql dialect); no hidden characters; size cap."

    def run(self, state: GuardState) -> CheckResult:
        sql = state.sql or ""
        if not sql.strip():
            return self.block("empty SQL")
        if len(sql) > state.policy.max_sql_chars:
            return self.block(
                f"SQL is {len(sql)} characters; the limit is {state.policy.max_sql_chars}"
            )
        m = _SUSPICIOUS.search(sql)
        if m:
            return self.block(f"hidden/control character U+{ord(m.group()):04X} in SQL")
        try:
            with quiet_sqlglot():
                state.tokens = TSQL().tokenize(sql)
                statements = sqlglot.parse(sql, read=DIALECT)
        except Exception as e:  # ParseError, TokenError, and the odd AttributeError
            first = str(e).splitlines()[0] if str(e) else type(e).__name__
            return self.block(f"could not parse as T-SQL: {first}")
        state.statements = [s for s in statements if s is not None]
        if not state.statements:
            return self.block("no statement found")
        return self.ok(f"parsed {len(state.statements)} statement(s)")
