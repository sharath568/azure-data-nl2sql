"""The semantic layer: a YAML file a human curates next to the database.

It does two jobs:

1. **Grounding.** Table and column descriptions, synonyms and metric
   definitions ("revenue means ...") that go into the schema card the model
   sees. These are *documentation*, and they are treated as data, not as
   instructions (see the prompt in ``providers/prompt.py``).
2. **Policy.** Which tables may be queried, which columns are off limits,
   the row cap, the timeout and the cost limits. The guardrails read this.

A minimal file looks like::

    version: 1
    policy:
      allowed_tables: [customers, orders]
      denied_columns: [customers.email]
    tables:
      customers:
        description: People who buy from us.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

DEFAULT_DENIED_PATTERNS = (
    "*ssn*",
    "*social_security*",
    "*salary*",
    "*password*",
    "*passwd*",
    "*secret*",
    "*credit_card*",
    "*card_number*",
)


@dataclass
class CostPolicy:
    max_joins: int = 6
    max_subquery_depth: int = 4
    block_cartesian: bool = True
    use_plan_estimates: bool = True
    max_estimated_rows: float = 1_000_000
    max_estimated_cost: float = 50.0


@dataclass
class Policy:
    allowed_tables: list[str] | None = None
    denied_tables: list[str] = field(default_factory=list)
    allowed_schemas: list[str] = field(default_factory=lambda: ["dbo"])
    denied_columns: list[str] = field(default_factory=list)
    denied_column_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_DENIED_PATTERNS))
    max_rows: int = 100
    timeout_seconds: int = 10
    allow_comments: bool = False
    max_sql_chars: int = 8000
    allow_privileged_connection: bool = False
    allowed_table_hints: list[str] = field(
        default_factory=lambda: ["NOLOCK", "READUNCOMMITTED", "NOWAIT"]
    )
    cost: CostPolicy = field(default_factory=CostPolicy)

    def is_column_denied(self, table: str | None, column: str) -> bool:
        """True if ``table.column`` is off limits by name or by pattern."""
        col = column.lower()
        if table:
            qualified = f"{table.lower()}.{col}"
            if qualified in {d.lower() for d in self.denied_columns}:
                return True
        # A bare entry in denied_columns applies to that column name anywhere.
        if col in {d.lower() for d in self.denied_columns if "." not in d}:
            return True
        return any(fnmatch.fnmatchcase(col, p.lower()) for p in self.denied_column_patterns)

    def is_table_denied(self, table: str) -> bool:
        t = table.lower()
        return any(fnmatch.fnmatchcase(t, p.lower()) for p in self.denied_tables)


@dataclass
class ColumnDoc:
    description: str = ""
    sensitive: bool = False
    synonyms: list[str] = field(default_factory=list)


@dataclass
class TableDoc:
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    columns: dict[str, ColumnDoc] = field(default_factory=dict)


@dataclass
class Metric:
    name: str
    description: str
    sql: str
    tables: list[str] = field(default_factory=list)
    filter: str = ""
    synonyms: list[str] = field(default_factory=list)


@dataclass
class SemanticLayer:
    policy: Policy = field(default_factory=Policy)
    tables: dict[str, TableDoc] = field(default_factory=dict)
    metrics: dict[str, Metric] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    source: str = "<defaults>"

    def table_doc(self, table: str) -> TableDoc:
        return self.tables.get(table.lower(), TableDoc())

    def column_doc(self, table: str, column: str) -> ColumnDoc:
        return self.table_doc(table).columns.get(column.lower(), ColumnDoc())

    def is_column_denied(self, table: str | None, column: str) -> bool:
        if table and self.column_doc(table, column).sensitive:
            return True
        return self.policy.is_column_denied(table, column)


def _policy_from_dict(d: dict[str, Any]) -> Policy:
    p = Policy()
    for key in (
        "denied_tables",
        "allowed_schemas",
        "denied_columns",
        "allowed_table_hints",
    ):
        if key in d:
            setattr(p, key, [str(x) for x in d[key] or []])
    if "allowed_tables" in d:
        p.allowed_tables = (
            None if d["allowed_tables"] is None else [str(x).lower() for x in d["allowed_tables"]]
        )
    if "denied_column_patterns" in d:
        p.denied_column_patterns = [str(x) for x in d["denied_column_patterns"] or []]
    if d.get("extra_denied_column_patterns"):
        p.denied_column_patterns += [str(x) for x in d["extra_denied_column_patterns"]]
    for key, cast in (
        ("max_rows", int),
        ("timeout_seconds", int),
        ("allow_comments", bool),
        ("max_sql_chars", int),
        ("allow_privileged_connection", bool),
    ):
        if key in d:
            setattr(p, key, cast(d[key]))
    if p.max_rows < 1:
        raise ValueError("policy.max_rows must be >= 1")
    cost = d.get("cost") or {}
    for key in vars(p.cost):
        if key in cost:
            setattr(p.cost, key, type(getattr(p.cost, key))(cost[key]))
    return p


def from_dict(d: dict[str, Any], source: str = "<dict>") -> SemanticLayer:
    if not isinstance(d, dict):
        raise ValueError(f"{source}: semantic layer must be a mapping")
    layer = SemanticLayer(policy=_policy_from_dict(d.get("policy") or {}), source=source)
    for tname, tdef in (d.get("tables") or {}).items():
        tdef = tdef or {}
        cols = {}
        for cname, cdef in (tdef.get("columns") or {}).items():
            if isinstance(cdef, str):
                cdef = {"description": cdef}
            cdef = cdef or {}
            cols[str(cname).lower()] = ColumnDoc(
                description=str(cdef.get("description", "")),
                sensitive=bool(cdef.get("sensitive", False)),
                synonyms=[str(s) for s in cdef.get("synonyms", [])],
            )
        layer.tables[str(tname).lower()] = TableDoc(
            description=str(tdef.get("description", "")),
            synonyms=[str(s) for s in tdef.get("synonyms", [])],
            columns=cols,
        )
    for mname, mdef in (d.get("metrics") or {}).items():
        mdef = mdef or {}
        layer.metrics[str(mname).lower()] = Metric(
            name=str(mname),
            description=str(mdef.get("description", "")),
            sql=str(mdef.get("sql", "")),
            tables=[str(t).lower() for t in mdef.get("tables", [])],
            filter=str(mdef.get("filter", "")),
            synonyms=[str(s) for s in mdef.get("synonyms", [])],
        )
    layer.notes = [str(n) for n in d.get("notes", []) or []]
    return layer


def load(path: str | Path) -> SemanticLayer:
    path = Path(path)
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return from_dict(data, source=str(path))


def bundled_path(name: str) -> Path:
    """Path to a file shipped in ``nl2sql/data``."""
    return Path(str(resources.files("nl2sql").joinpath("data", name)))


def load_sample() -> SemanticLayer:
    return load(bundled_path("semantic.yaml"))
