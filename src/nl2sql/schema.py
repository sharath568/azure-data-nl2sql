"""Schema grounding: what the database looks like, and the part of it a question needs.

``Schema`` is filled in by a backend's ``introspect()``. ``SchemaCard`` is the
compact text the model sees. Two things are deliberate:

* **Denied columns are left out of the card.** A model cannot ask for a column
  it was never shown (it can still guess, which is why the allow-list guardrail
  exists too).
* **Retrieval is boring on purpose.** Keyword overlap with table names,
  synonyms, column names and descriptions, then one hop along foreign keys so
  joins have both ends. That is enough for schemas of tens of tables and is
  easy to debug. Embeddings can come later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from nl2sql.semantic import SemanticLayer


@dataclass
class Column:
    name: str
    data_type: str
    nullable: bool = True
    primary_key: bool = False
    sample_values: list[str] = field(default_factory=list)


@dataclass
class ForeignKey:
    table: str
    column: str
    ref_table: str
    ref_column: str


@dataclass
class Table:
    name: str
    schema: str = "dbo"
    columns: list[Column] = field(default_factory=list)

    def column(self, name: str) -> Column | None:
        name = name.lower()
        return next((c for c in self.columns if c.name.lower() == name), None)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


@dataclass
class Schema:
    tables: dict[str, Table] = field(default_factory=dict)  # keyed by lower-case name
    foreign_keys: list[ForeignKey] = field(default_factory=list)

    def table(self, name: str) -> Table | None:
        return self.tables.get(name.lower())

    def neighbors(self, name: str) -> set[str]:
        name = name.lower()
        out = set()
        for fk in self.foreign_keys:
            if fk.table.lower() == name:
                out.add(fk.ref_table.lower())
            if fk.ref_table.lower() == name:
                out.add(fk.table.lower())
        out.discard(name)
        return out

    def denied_columns(self, layer: SemanticLayer) -> dict[str, set[str]]:
        """Map of table -> denied column names (lower case), for tables that have any."""
        out: dict[str, set[str]] = {}
        for tname, table in self.tables.items():
            denied = {
                c.name.lower() for c in table.columns if layer.is_column_denied(tname, c.name)
            }
            if denied:
                out[tname] = denied
        return out

    def visible(self, layer: SemanticLayer) -> Schema:
        """A copy with disallowed tables and denied columns removed."""
        policy = layer.policy
        allowed = set(policy.allowed_tables) if policy.allowed_tables is not None else None
        tables = {}
        for tname, t in self.tables.items():
            if allowed is not None and tname not in allowed:
                continue
            if policy.is_table_denied(tname):
                continue
            cols = [c for c in t.columns if not layer.is_column_denied(tname, c.name)]
            tables[tname] = Table(t.name, t.schema, cols)
        fks = [
            fk
            for fk in self.foreign_keys
            if fk.table.lower() in tables
            and fk.ref_table.lower() in tables
            and tables[fk.table.lower()].column(fk.column)
            and tables[fk.ref_table.lower()].column(fk.ref_column)
        ]
        return Schema(tables, fks)


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "a",
    "an",
    "the",
    "of",
    "in",
    "on",
    "for",
    "by",
    "to",
    "and",
    "or",
    "is",
    "are",
    "was",
    "were",
    "what",
    "which",
    "who",
    "how",
    "many",
    "much",
    "show",
    "list",
    "me",
    "give",
    "all",
    "each",
    "per",
    "our",
    "we",
    "do",
    "did",
    "does",
    "have",
    "has",
    "with",
    "that",
    "this",
    "from",
    "top",
    "most",
    "than",
    "more",
    "less",
    "their",
    "them",
    "there",
    "it",
}


def _stem(word: str) -> str:
    for suffix in ("ies", "es", "s"):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def words(text: str) -> set[str]:
    """Lower-cased, lightly stemmed content words. ``order_items`` -> {order, item}."""
    raw = _WORD.findall(text.lower().replace("_", " "))
    return {_stem(w) for w in raw if w not in _STOP}


@dataclass
class Retrieval:
    tables: list[str]
    scores: dict[str, float]
    metrics: list[str]
    reason: str


def retrieve(
    question: str,
    schema: Schema,
    layer: SemanticLayer,
    *,
    max_tables: int = 6,
    min_tables_for_fallback: int = 8,
) -> Retrieval:
    """Pick the tables (and metrics) a question is likely to need."""
    q = words(question)
    scores: dict[str, float] = {}
    for tname, table in schema.tables.items():
        doc = layer.table_doc(tname)
        score = 0.0
        name_words = words(tname)
        if name_words and name_words <= q:
            score += 5
        score += 3 * len(q & words(" ".join(doc.synonyms)))
        for col in table.columns:
            cw = words(col.name) - {"id"}
            cdoc = layer.column_doc(tname, col.name)
            if cw and cw <= q:
                score += 2
            score += 1.5 * len(q & words(" ".join(cdoc.synonyms)))
            score += 0.5 * len(q & {w.lower() for v in col.sample_values for w in words(v)})
        score += 0.25 * len(q & words(doc.description))
        if score:
            scores[tname] = score

    metrics = []
    for mname, metric in layer.metrics.items():
        if q & (words(mname) | words(" ".join(metric.synonyms))):
            metrics.append(mname)
            for t in metric.tables:
                if t in schema.tables:
                    scores[t] = scores.get(t, 0) + 4

    if not scores:
        # Nothing matched. For a small schema just send it all; otherwise
        # send nothing and let the model say it cannot answer.
        if len(schema.tables) <= min_tables_for_fallback:
            names = sorted(schema.tables)
            return Retrieval(names, {}, metrics, "no keyword match; small schema, sent all tables")
        return Retrieval([], {}, metrics, "no keyword match")

    picked = sorted(scores, key=lambda t: (-scores[t], t))[:max_tables]
    # One hop along foreign keys so a join has both ends, but only to tables
    # that sit *between* picked tables or that the question hinted at.
    extra = []
    for t in list(picked):
        for n in sorted(schema.neighbors(t)):
            if n in picked or n in extra:
                continue
            links_two = sum(1 for p in picked if n in schema.neighbors(p)) >= 2
            if links_two:
                extra.append(n)
    # Bridge tables: if two picked tables are not adjacent, add a table adjacent to both.
    for i, a in enumerate(picked):
        for b in picked[i + 1 :]:
            if b in schema.neighbors(a):
                continue
            bridges = sorted(schema.neighbors(a) & schema.neighbors(b))
            if bridges and not any(x in picked or x in extra for x in bridges):
                extra.append(bridges[0])
    tables = (picked + extra)[:max_tables]
    return Retrieval(tables, scores, metrics, "keyword + foreign-key neighbourhood")


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------


def _clean(text: str, limit: int = 160) -> str:
    """Descriptions and sample values are data. Keep them on one short line."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def render_card(
    schema: Schema,
    layer: SemanticLayer,
    tables: list[str] | None = None,
    metrics: list[str] | None = None,
) -> str:
    """Compact, model-friendly description of (part of) the schema.

    ``schema`` should already be the *visible* schema (``Schema.visible``).
    """
    names = [t.lower() for t in tables] if tables is not None else sorted(schema.tables)
    lines: list[str] = []
    for tname in names:
        table = schema.table(tname)
        if table is None:
            continue
        doc = layer.table_doc(tname)
        head = f"TABLE {table.schema}.{table.name}" if table.schema else f"TABLE {table.name}"
        if doc.description:
            head += f"  -- {_clean(doc.description)}"
        lines.append(head)
        if doc.synonyms:
            lines.append(f"  (also called: {', '.join(_clean(s, 40) for s in doc.synonyms)})")
        for col in table.columns:
            line = f"  {col.name} {col.data_type}"
            if col.primary_key:
                line += " PK"
            cdoc = layer.column_doc(tname, col.name)
            notes = []
            if cdoc.description:
                notes.append(_clean(cdoc.description))
            if col.sample_values:
                vals = ", ".join(_clean(v, 30) for v in col.sample_values[:12])
                notes.append(f"values: {vals}")
            if notes:
                line += "  -- " + "; ".join(notes)
            lines.append(line)
        for fk in schema.foreign_keys:
            if fk.table.lower() == tname and fk.ref_table.lower() in names:
                lines.append(f"  FK {fk.column} -> {fk.ref_table}.{fk.ref_column}")
        lines.append("")
    chosen_metrics = metrics if metrics is not None else sorted(layer.metrics)
    if chosen_metrics:
        lines.append("METRICS (use these definitions when the question mentions them)")
        for m in chosen_metrics:
            metric = layer.metrics.get(m)
            if not metric:
                continue
            line = f"  {metric.name}: {metric.sql}"
            if metric.filter:
                line += f"  WHERE {metric.filter}"
            if metric.description:
                line += f"  -- {_clean(metric.description)}"
            lines.append(line)
        lines.append("")
    if layer.notes:
        lines.append("NOTES")
        lines.extend(f"  - {_clean(n, 200)}" for n in layer.notes)
    return "\n".join(lines).rstrip() + "\n"
