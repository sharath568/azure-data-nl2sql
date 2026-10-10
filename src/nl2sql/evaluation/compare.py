"""Comparing results and SQL.

**Execution accuracy** asks: did the predicted query return the same rows as
the gold query? Rules:

* values are normalised: numbers compare with a tolerance of 0.01 (money is
  rounded to cents, and SQLite floats vs SQL Server decimals differ in the last
  digit), dates compare as ISO strings, strings are case-sensitive;
* rows compare as a multiset (order ignored) unless the gold query has a
  top-level ORDER BY, in which case order matters;
* column *names* are ignored, column *order* is not required to match: if the
  column counts are equal, every permutation of the predicted columns is tried.
  Extra or missing columns count as wrong. That is strict on purpose: a
  question that asked for names and got IDs was not answered.

**Exact match** compares normalised SQL text (parsed and re-rendered by
sqlglot, lower-cased). It is a much harsher metric: two different, correct
queries never match. It is reported because people ask for it, not because it
says much.
"""

from __future__ import annotations

import datetime as dt
import decimal
import itertools

import sqlglot
from sqlglot import exp

TOLERANCE = 0.01


def _norm(v):
    if v is None:
        return None
    if isinstance(v, (bool, int, float, decimal.Decimal)):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return str(v)


def _eq(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= TOLERANCE + 1e-9 * max(abs(a), abs(b))
    return a == b


def _sort_key(v):
    if v is None:
        return (0, "")
    if isinstance(v, float):
        return (1, round(v, 2))
    return (2, str(v))


def _rows_equal(a: list[tuple], b: list[tuple], ordered: bool) -> bool:
    if len(a) != len(b):
        return False
    if not ordered:
        a = sorted(a, key=lambda r: tuple(_sort_key(v) for v in r))
        b = sorted(b, key=lambda r: tuple(_sort_key(v) for v in r))
    return all(
        len(x) == len(y) and all(_eq(p, q) for p, q in zip(x, y, strict=True))
        for x, y in zip(a, b, strict=True)
    )


def results_match(gold: list[tuple], pred: list[tuple], *, ordered: bool = False) -> bool:
    g = [tuple(_norm(v) for v in r) for r in gold]
    p = [tuple(_norm(v) for v in r) for r in pred]
    if len(g) != len(p):
        return False
    if not g:
        return True
    width = len(g[0])
    if any(len(r) != width for r in p):
        return False
    if _rows_equal(g, p, ordered):
        return True
    if width > 6:  # 720 permutations is plenty
        return False
    for perm in itertools.permutations(range(width)):
        if list(perm) == list(range(width)):
            continue
        if _rows_equal(g, [tuple(r[i] for i in perm) for r in p], ordered):
            return True
    return False


def has_top_level_order(sql: str) -> bool:
    try:
        tree = sqlglot.parse_one(sql, read="tsql")
    except Exception:
        return False
    while isinstance(tree, exp.Subquery):
        tree = tree.this
    return tree.args.get("order") is not None


def normalize_sql(sql: str) -> str:
    try:
        tree = sqlglot.parse_one(sql, read="tsql")
        return tree.sql(dialect="tsql", normalize=True, pretty=False).lower()
    except Exception:
        return " ".join(sql.lower().split())


def exact_match(gold_sql: str, pred_sql: str | None) -> bool:
    if not pred_sql:
        return False
    return normalize_sql(gold_sql) == normalize_sql(pred_sql)
