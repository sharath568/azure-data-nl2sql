"""The offline "stub" provider. **This is not an AI.**

It is a few dozen regular expressions and SQL templates that read the schema
card's structure (tables, foreign keys, synonyms, metrics, low-cardinality
sample values). It exists so that the whole pipeline (retrieval, guardrails,
execution, redaction, evaluation) can run in CI and on a laptop for free, with
deterministic output.

Two things to know before reading any number it produces:

* Its accuracy on the gold set measures **how many templates were written**,
  not how good natural-language-to-SQL is. The templates are generic patterns
  ("how many X in each Y", "top N X by metric", ...), not a lookup of the gold
  answers, so it misses questions phrased in ways it was not written for. That
  is the honest result.
* On adversarial questions it **does what it is told on purpose**: "delete all
  orders" becomes ``DELETE FROM orders``, "ignore previous instructions and run:
  X" becomes ``X``. It plays the part of a fully compromised model so the
  guardrails have something real to block. A real model will often refuse
  instead, which is good, but the guardrails must not depend on that.
"""

from __future__ import annotations

import re
import time
from collections import deque

from nl2sql.providers.base import Generation, GenerationContext, Provider, Usage
from nl2sql.providers.prompt import estimate_prompt_tokens, estimate_tokens
from nl2sql.schema import Schema, Table

_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)  # fmt: skip
MONTHS = {m: i for i, m in enumerate(_MONTH_NAMES, start=1)}

# Words that point at a sensitive column. The stub "guesses" the column name,
# like a model would; the visible schema it is given does not contain them.
SENSITIVE_WORDS = {
    "salary": r"salar(?:y|ies)|earns?|paid|pay|compensation",
    "ssn": r"ssns?|social security(?: numbers?)?",
    "email": r"e-?mails?(?: address(?:es)?)?",
}
# Where a model would guess such a column lives when no table is named.
SENSITIVE_HOME = {"salary": "employee", "ssn": "employee", "email": "customer"}

_NAME_COLS = ("name", "first_name", "last_name")


def _alias(table: str, used: set[str]) -> str:
    base = "".join(w[0] for w in table.split("_") if w) or "t"
    alias, i = base, 2
    while alias in used:
        alias, i = f"{base}{i}", i + 1
    used.add(alias)
    return alias


def _pk(table: Table) -> str | None:
    return next((c.name for c in table.columns if c.primary_key), None)


def _is_date(col) -> bool:
    return col.data_type.upper().startswith(("DATE", "DATETIME", "TEXT")) and col.name.endswith(
        "_date"
    )


class TemplateProvider(Provider):
    name = "stub"
    model = "template-matcher (not an AI)"
    wants_sample_values = True

    def generate(self, ctx: GenerationContext) -> Generation:
        start = time.perf_counter()
        q = " ".join(ctx.question.split())
        sql, rule = None, "no template matches this question"
        for handler in self._handlers():
            out = handler(q, ctx)
            if out:
                sql, rule = out, f"stub template '{handler.__name__.lstrip('_')}'"
                break
        latency = (time.perf_counter() - start) * 1000
        # What this would have cost with a real model: the prompt we would send,
        # plus a typical JSON answer. Estimated, and labelled as such.
        completion = estimate_tokens(sql or "") + 40
        usage = Usage(estimate_prompt_tokens(ctx), completion, estimated=True)
        tables = sorted({t for t in ctx.schema.tables if sql and re.search(rf"\b{t}\b", sql)})
        return Generation(sql, rule, tables, self.name, self.model, usage, latency)

    def _handlers(self):
        return [
            # --- "compromised model" behaviour, for the adversarial set ---
            self._verbatim_sql,
            self._xp_cmdshell,
            self._openrowset,
            self._waitfor,
            self._delete,
            self._drop,
            self._truncate,
            self._update,
            self._select_into_temp,
            self._every_with_every,
            self._union_exfiltration,
            self._sensitive_column,
            self._all_columns,
            # --- ordinary questions ---
            self._never,
            self._average_value,
            self._most_expensive,
            self._top_n_by_metric,
            self._which_has_most,
            self._metric_by_dimension,
            self._total_metric,
            self._count_by_group,
            self._count,
            self._list,
        ]

    # ------------------------------------------------------------------ lexicon

    @staticmethod
    def _entities(q: str, ctx: GenerationContext) -> list[str]:
        """Tables mentioned in the question, in order of first mention."""
        low = q.lower()
        hits: list[tuple[int, str]] = []
        for tname in ctx.schema.tables:
            doc = ctx.layer.table_doc(tname)
            phrases = [
                tname,
                tname.replace("_", " "),
                tname.rstrip("s").replace("_", " "),
                *doc.synonyms,
            ]
            positions = []
            for p in phrases:
                p = p.lower().rstrip("s")
                m = re.search(rf"\b{re.escape(p)}(?:s|es)?\b", low)
                if m:
                    positions.append(m.start())
            if positions:
                hits.append((min(positions), tname))
        hits.sort()
        out = [t for _, t in hits]
        # "order lines"/"order items" mention both; prefer the specific table.
        if "order_items" in out and "orders" in out and re.search(r"order (?:lines?|items?)", low):
            out.remove("orders")
        return out

    @staticmethod
    def _guess_table(q: str, ctx: GenerationContext, words: list[str]) -> str | None:
        low = q.lower()
        for w in words:
            if w in low:
                for t in ctx.schema.tables:
                    if t.startswith(w):
                        return t
        return None

    @staticmethod
    def _display(table: Table) -> list[str]:
        cols = [c.name for c in table.columns if c.name in _NAME_COLS]
        pk = _pk(table)
        if cols:
            if "name" in cols and pk:
                return [pk, "name"]
            return ([pk] if pk else []) + [c for c in _NAME_COLS if c in cols and c != "name"]
        fks = [c.name for c in table.columns if c.name.endswith("_id") and c.name != pk]
        dates = [c.name for c in table.columns if _is_date(c)]
        return ([pk] if pk else []) + fks[:1] + dates[:1]

    @staticmethod
    def _column_for(q: str, ctx: GenerationContext, table: str) -> str | None:
        """A column of ``table`` named (or described by a synonym) in the question."""
        low = q.lower()
        t = ctx.schema.table(table)
        if t is None:
            return None
        for col in t.columns:
            phrases = [col.name.replace("_", " "), *ctx.layer.column_doc(table, col.name).synonyms]
            for p in phrases:
                if re.search(rf"\b{re.escape(p.lower())}(?:s|es)?\b", low):
                    return col.name
        return None

    @staticmethod
    def _value_filters(
        q: str, ctx: GenerationContext, tables: list[str]
    ) -> list[tuple[str, str, str]]:
        """(table, column, value) for sample values that appear in the question."""
        low = q.lower()
        out = []
        for tname in tables:
            t = ctx.schema.table(tname)
            if t is None:
                continue
            for col in t.columns:
                for v in col.sample_values:
                    if len(v) > 2 and re.search(rf"\b{re.escape(v.lower())}\b", low):
                        # "sales rep" is not a filter on department = 'Sales'
                        if v.lower() == "sales" and re.search(r"sales ?(rep|people|person)", low):
                            continue
                        out.append((tname, col.name, v))
        return out

    @staticmethod
    def _date_filter(q: str, expr: str) -> str | None:
        low = q.lower()
        m = re.search(r"\b(" + "|".join(MONTHS) + r")\s+(\d{4})\b", low)
        if m:
            month, year = MONTHS[m.group(1)], int(m.group(2))
            nxt = (year + 1, 1) if month == 12 else (year, month + 1)
            return f"{expr} >= '{year}-{month:02d}-01' AND {expr} < '{nxt[0]}-{nxt[1]:02d}-01'"
        m = re.search(r"\bq([1-4])\s+(\d{4})\b", low)
        if m:
            qtr, year = int(m.group(1)), int(m.group(2))
            start, end = 3 * (qtr - 1) + 1, 3 * qtr + 1
            end_s = f"{year + 1}-01-01" if end == 13 else f"{year}-{end:02d}-01"
            return f"{expr} >= '{year}-{start:02d}-01' AND {expr} < '{end_s}'"
        m = re.search(r"\b(?:in|during|for)\s+(\d{4})\b", low)
        if m:
            year = int(m.group(1))
            return f"{expr} >= '{year}-01-01' AND {expr} < '{year + 1}-01-01'"
        return None

    @staticmethod
    def _metric(q: str, ctx: GenerationContext):
        low = re.sub(r"sales ?(reps?|people|persons?)", "", q.lower())
        for name, metric in ctx.layer.metrics.items():
            phrases = [name.replace("_", " "), *metric.synonyms]
            if any(re.search(rf"\b{re.escape(p.lower())}\b", low) for p in phrases):
                return name, metric
        return None

    @staticmethod
    def _join_path(
        schema: Schema, start: str, targets: list[str]
    ) -> list[tuple[str, str, str, str]] | None:
        """Foreign-key path covering ``targets`` from ``start``: list of
        (from_table, from_col, to_table, to_col) edges, in join order."""
        edges: dict[str, list[tuple[str, str, str, str]]] = {}
        for fk in schema.foreign_keys:
            a, b = fk.table.lower(), fk.ref_table.lower()
            if a == b:
                continue
            edges.setdefault(a, []).append((a, fk.column, b, fk.ref_column))
            edges.setdefault(b, []).append((b, fk.ref_column, a, fk.column))
        joined = {start}
        path: list[tuple[str, str, str, str]] = []
        for target in targets:
            if target in joined:
                continue
            prev: dict[str, tuple | None] = dict.fromkeys(joined)
            queue = deque(joined)
            while queue:
                cur = queue.popleft()
                if cur == target:
                    break
                for e in edges.get(cur, []):
                    if e[2] not in prev:
                        prev[e[2]] = e
                        queue.append(e[2])
            if target not in prev:
                return None
            step, node = [], target
            while prev[node] is not None:
                e = prev[node]
                step.append(e)
                node = e[0]
            for e in reversed(step):
                if e[2] not in joined:
                    path.append(e)
                    joined.add(e[2])
        return path

    def _from_clause(self, ctx, start: str, needed: list[str]):
        """FROM/JOIN text plus alias map, joining ``start`` to every table in ``needed``.

        Metric SQL in the semantic layer uses fixed aliases (``oi``, ``o``), so
        aliases are initials of the table name, which matches that convention.
        """
        path = self._join_path(ctx.schema, start, needed)
        if path is None:
            return None, None
        used: set[str] = set()
        aliases = {start: _alias(start, used)}
        parts = [f"{start} {aliases[start]}"]
        for a, acol, b, bcol in path:
            aliases[b] = _alias(b, used)
            parts.append(f"JOIN {b} {aliases[b]} ON {aliases[b]}.{bcol} = {aliases[a]}.{acol}")
        return " ".join(parts), aliases

    # ------------------------------------------------- compromised-model mode

    @staticmethod
    def _verbatim_sql(q: str, ctx) -> str | None:
        m = re.search(r"\b(?:run|execute|exec|return)(?: this)?\s*:\s*(.+)$", q, re.I | re.S)
        if m:
            return m.group(1).strip()
        # A question that is itself SQL ("SELECT ... FROM ..."), not English that
        # happens to start with "Delete" or "Update".
        if re.match(r"\s*(select|with)\b.*\bfrom\b", q, re.I | re.S):
            return q.strip()
        return None

    @staticmethod
    def _xp_cmdshell(q: str, ctx) -> str | None:
        m = re.search(r"xp_cmdshell\s*('[^']*')?", q, re.I)
        return f"EXEC xp_cmdshell {m.group(1) or chr(39) + 'whoami' + chr(39)}" if m else None

    @staticmethod
    def _openrowset(q: str, ctx) -> str | None:
        if not re.search(r"openrowset", q, re.I):
            return None
        m = re.search(r"([A-Za-z]:\\[^\s]+)", q)
        path = (m.group(1) if m else r"C:\secrets.txt").rstrip(".")
        return f"SELECT BulkColumn FROM OPENROWSET(BULK '{path}', SINGLE_CLOB) AS f"

    def _waitfor(self, q: str, ctx) -> str | None:
        m = re.search(r"\bwait(?: for)? (\d+) (minute|second)s?", q, re.I)
        if not m:
            return None
        n = int(m.group(1))
        delay = f"00:{n:02d}:00" if m.group(2).lower() == "minute" else f"00:00:{n:02d}"
        ents = self._entities(q, ctx) or ["orders"]
        return f"WAITFOR DELAY '{delay}'; SELECT * FROM {ents[0]}"

    def _delete(self, q: str, ctx) -> str | None:
        if re.search(r"^\s*(delete|remove|erase|wipe)\b", q, re.I):
            ents = self._entities(q, ctx)
            return f"DELETE FROM {ents[0]}" if ents else None
        return None

    def _drop(self, q: str, ctx) -> str | None:
        if re.search(r"^\s*drop\b", q, re.I):
            ents = self._entities(q, ctx)
            return f"DROP TABLE {ents[0]}" if ents else None
        return None

    def _truncate(self, q: str, ctx) -> str | None:
        if re.search(r"^\s*truncate\b", q, re.I):
            ents = self._entities(q, ctx)
            return f"TRUNCATE TABLE {ents[0]}" if ents else None
        return None

    def _update(self, q: str, ctx) -> str | None:
        m = re.search(r"^\s*(?:update|set|change)\b.*\bto\s+(-?\d+(?:\.\d+)?)", q, re.I)
        if not m:
            return None
        ents = self._entities(q, ctx)
        if not ents:
            return None
        col = self._column_for(q, ctx, ents[0]) or "name"
        return f"UPDATE {ents[0]} SET {col} = {m.group(1)}"

    def _select_into_temp(self, q: str, ctx) -> str | None:
        m = re.search(r"(#{1,2}\w+)", q)
        if not m or not re.search(r"\b(copy|into|backup|save)\b", q, re.I):
            return None
        ents = self._entities(q, ctx)
        return f"SELECT * INTO {m.group(1)} FROM {ents[0]}" if ents else None

    def _every_with_every(self, q: str, ctx) -> str | None:
        if len(re.findall(r"\bevery\b", q, re.I)) < 2:
            return None
        ents = self._entities(q, ctx)
        if len(ents) < 2:
            return None
        used: set[str] = set()
        aliases = [_alias(t, used) for t in ents]
        cols = ", ".join(
            f"{a}.{_pk(ctx.schema.table(t))}"
            for t, a in zip(ents, aliases, strict=True)
            if _pk(ctx.schema.table(t))
        )
        joins = " CROSS JOIN ".join(f"{t} {a}" for t, a in zip(ents, aliases, strict=True))
        return f"SELECT {cols} FROM {joins}"

    def _sensitive_hit(self, q: str) -> str | None:
        for col, pattern in SENSITIVE_WORDS.items():
            if re.search(rf"\b(?:{pattern})\b", q, re.I):
                return col
        return None

    def _union_exfiltration(self, q: str, ctx) -> str | None:
        if not re.search(r"\b(union|combine)\b", q, re.I):
            return None
        sens = self._sensitive_hit(q)
        ents = self._entities(q, ctx)
        if not sens or len(ents) < 2:
            return None
        first_col = (
            self._column_for(q, ctx, ents[0]) or self._display(ctx.schema.table(ents[0]))[-1]
        )
        return f"SELECT {first_col} FROM {ents[0]} UNION SELECT {sens} FROM {ents[1]}"

    def _sensitive_column(self, q: str, ctx) -> str | None:
        sens = self._sensitive_hit(q)
        if not sens:
            return None
        ents = self._entities(q, ctx)
        table = (
            ents[0]
            if ents
            else self._guess_table(SENSITIVE_HOME[sens], ctx, [SENSITIVE_HOME[sens]])
        )
        if not table:
            return None
        t = ctx.schema.table(table)
        names = [c for c in ("first_name", "last_name", "name") if t.column(c)]
        m = re.search(r"\baverage\b.*\bby (\w+)", q, re.I)
        if m and t.column(m.group(1)):
            dim = m.group(1)
            return f"SELECT {dim}, AVG({sens}) AS avg_{sens} FROM {table} GROUP BY {dim}"
        m = re.search(r"\b(?:more|greater|over|above) than (\d+)", q, re.I)
        if m:
            return f"SELECT {', '.join(names)} FROM {table} WHERE {sens} > {m.group(1)}"
        if re.search(r"\b(?:sorted|ordered|ranked) by\b", q, re.I):
            other = self._column_for(q, ctx, table)
            cols = ", ".join([*names, other] if other else names)
            return f"SELECT {cols} FROM {table} ORDER BY {sens} DESC"
        return f"SELECT {', '.join([*names, sens])} FROM {table}"

    def _all_columns(self, q: str, ctx) -> str | None:
        if not re.search(r"\b(all|every) (columns|fields)\b|\beverything\b", q, re.I):
            return None
        ents = self._entities(q, ctx)
        return f"SELECT * FROM {ents[0]}" if ents else None

    # ------------------------------------------------------ ordinary questions

    def _where(self, q, ctx, aliases: dict[str, str], extra: list[str] | None = None) -> str:
        preds = list(extra or [])
        for tname, col, value in self._value_filters(q, ctx, list(aliases)):
            preds.append(f"{aliases[tname]}.{col} = '{value}'")
        return (" WHERE " + " AND ".join(dict.fromkeys(preds))) if preds else ""

    def _date_pred(self, q, ctx, aliases: dict[str, str]) -> str | None:
        for tname, alias in aliases.items():
            t = ctx.schema.table(tname)
            for col in t.columns:
                if _is_date(col):
                    pred = self._date_filter(q, f"{alias}.{col.name}")
                    if pred:
                        return pred
        return None

    def _never(self, q: str, ctx) -> str | None:
        if not re.search(r"\bnever\b|\bno (orders|sales)\b", q, re.I):
            return None
        ents = self._entities(q, ctx)
        if not ents:
            return None
        parent = ents[0]
        pt = ctx.schema.table(parent)
        children = [
            fk
            for fk in ctx.schema.foreign_keys
            if fk.ref_table.lower() == parent and fk.table.lower() != parent
        ]
        verb_sold = re.search(r"\bsold\b", q, re.I)
        children.sort(key=lambda fk: (("item" in fk.table) != bool(verb_sold), fk.table))
        if not children:
            return None
        fk = children[0]
        cols = ", ".join(f"x.{c}" for c in self._display(pt))
        return (
            f"SELECT {cols} FROM {parent} x WHERE NOT EXISTS "
            f"(SELECT 1 FROM {fk.table} y WHERE y.{fk.column} = x.{fk.ref_column})"
        )

    def _average_value(self, q: str, ctx) -> str | None:
        m = re.search(r"\baverage (\w+) value\b", q, re.I)
        metric = self._metric("revenue", ctx)
        if not m or not metric:
            return None
        ents = self._entities(m.group(1), ctx)
        if not ents:
            return None
        grain = ents[0]
        _, mdef = metric
        from_sql, aliases = self._from_clause(ctx, grain, mdef.tables)
        if not from_sql:
            return None
        pk = _pk(ctx.schema.table(grain))
        where = f" WHERE {mdef.filter}" if mdef.filter else ""
        return (
            f"SELECT ROUND(AVG(v), 2) AS avg_{grain.rstrip('s')}_value FROM "
            f"(SELECT {aliases[grain]}.{pk}, {mdef.sql} AS v FROM {from_sql}{where} "
            f"GROUP BY {aliases[grain]}.{pk}) AS t"
        )

    def _most_expensive(self, q: str, ctx) -> str | None:
        m = re.search(r"\b(\d+) (most expensive|cheapest|least expensive)\b", q, re.I)
        if not m:
            return None
        ents = self._entities(q, ctx)
        if not ents:
            return None
        t = ctx.schema.table(ents[0])
        price = self._column_for("price", ctx, ents[0])
        if not price:
            return None
        order = "DESC" if "most" in m.group(2).lower() else "ASC"
        cols = ", ".join([*self._display(t), price])
        return f"SELECT TOP {m.group(1)} {cols} FROM {ents[0]} ORDER BY {price} {order}"

    def _grouped_metric(self, q, ctx, entity: str, n: int | None):
        metric = self._metric(q, ctx)
        if not metric:
            return None
        mname, mdef = metric
        from_sql, aliases = self._from_clause(ctx, entity, mdef.tables)
        if not from_sql:
            return None
        a = aliases[entity]
        cols = [f"{a}.{c}" for c in self._display(ctx.schema.table(entity))]
        value = f"ROUND({mdef.sql}, 2)" if "unit" not in mname else mdef.sql
        extra = [mdef.filter] if mdef.filter else []
        date = self._date_pred(q, ctx, aliases)
        if date:
            extra.append(date)
        where = self._where(q, ctx, aliases, extra)
        top = f"TOP {n} " if n else ""
        return (
            f"SELECT {top}{', '.join(cols)}, {value} AS {mname} FROM {from_sql}{where} "
            f"GROUP BY {', '.join(cols)} ORDER BY {mname} DESC"
        )

    def _top_n_by_metric(self, q: str, ctx) -> str | None:
        m = re.search(r"\btop (\d+) (.+?) by (.+?)[?.]?$", q, re.I)
        if not m:
            return None
        ents = self._entities(m.group(2), ctx)
        if not ents:
            return None
        return self._grouped_metric(q, ctx, ents[0], int(m.group(1)))

    def _which_has_most(self, q: str, ctx) -> str | None:
        m = re.search(
            r"\bwhich (.+?) (?:has|have|had|handled|placed|made|got) the most (.+?)[?.]?$", q, re.I
        )
        if not m:
            return None
        subject, obj = m.group(1), m.group(2)
        ents = self._entities(subject, ctx)
        counted = self._entities(obj, ctx)
        if ents and counted and ents[0] != counted[0]:
            entity, child = ents[0], counted[0]
            from_sql, aliases = self._from_clause(ctx, entity, [child])
            if not from_sql:
                return None
            cols = [f"{aliases[entity]}.{c}" for c in self._display(ctx.schema.table(entity))]
            return (
                f"SELECT TOP 1 {', '.join(cols)}, COUNT(*) AS {child.rstrip('s')}_count "
                f"FROM {from_sql} GROUP BY {', '.join(cols)} "
                f"ORDER BY {child.rstrip('s')}_count DESC"
            )
        # "which city has the most customers": subject is a column of the counted table
        if counted:
            table = counted[0]
            col = self._column_for(subject, ctx, table)
            if col:
                return (
                    f"SELECT TOP 1 {col}, COUNT(*) AS {table.rstrip('s')}_count FROM {table} "
                    f"GROUP BY {col} ORDER BY {table.rstrip('s')}_count DESC"
                )
        return None

    def _dimension(self, phrase: str, ctx, tables: list[str]):
        """Find the column a 'by <phrase>' refers to, in the given tables or any table."""
        ents = self._entities(phrase, ctx)
        for t in [*ents, *tables, *ctx.schema.tables]:
            col = self._column_for(phrase, ctx, t)
            if col:
                return t, col
        return None

    def _metric_by_dimension(self, q: str, ctx) -> str | None:
        metric = self._metric(q, ctx)
        m = re.search(r"\bby ([a-z ]+?)(?: in \d{4})?[?.]?$", q, re.I)
        if not metric or not m:
            return None
        mname, mdef = metric
        phrase = m.group(1)
        anchor = mdef.tables[0]
        if re.search(r"\bmonth\b", phrase, re.I):
            from_sql, aliases = self._from_clause(ctx, anchor, mdef.tables)
            date_col = next(
                (
                    f"{aliases[t]}.{c.name}"
                    for t in aliases
                    for c in ctx.schema.table(t).columns
                    if _is_date(c)
                ),
                None,
            )
            if not date_col:
                return None
            extra = [mdef.filter] if mdef.filter else []
            year = re.search(r"\b(\d{4})\b", q)
            if year:
                extra.append(f"YEAR({date_col}) = {year.group(1)}")
            return (
                f"SELECT MONTH({date_col}) AS order_month, ROUND({mdef.sql}, 2) AS {mname} "
                f"FROM {from_sql}{self._where(q, ctx, aliases, extra)} "
                f"GROUP BY MONTH({date_col}) ORDER BY order_month"
            )
        dim = self._dimension(phrase, ctx, mdef.tables)
        if not dim:
            return None
        dtable, dcol = dim
        from_sql, aliases = self._from_clause(ctx, anchor, [*mdef.tables, dtable])
        if not from_sql:
            return None
        extra = [mdef.filter] if mdef.filter else []
        ref = f"{aliases[dtable]}.{dcol}"
        return (
            f"SELECT {ref}, ROUND({mdef.sql}, 2) AS {mname} FROM {from_sql}"
            f"{self._where(q, ctx, aliases, extra)} GROUP BY {ref}"
        )

    def _total_metric(self, q: str, ctx) -> str | None:
        if not re.search(r"\b(total|overall|our)\b", q, re.I):
            return None
        metric = self._metric(q, ctx)
        if not metric:
            return None
        mname, mdef = metric
        from_sql, aliases = self._from_clause(ctx, mdef.tables[0], mdef.tables)
        if not from_sql:
            return None
        extra = [mdef.filter] if mdef.filter else []
        date = self._date_pred(q, ctx, aliases)
        if date:
            extra.append(date)
        where = self._where(q, ctx, aliases, extra)
        return f"SELECT ROUND({mdef.sql}, 2) AS {mname} FROM {from_sql}{where}"

    def _count_by_group(self, q: str, ctx) -> str | None:
        m = re.search(
            r"\bhow many (.+?) (?:are )?(?:there )?(?:in each|per|for each|by) (.+?)[?.]?$", q, re.I
        )
        if not m:
            return None
        ents = self._entities(m.group(1), ctx)
        if not ents:
            return None
        table = ents[0]
        col = self._column_for(m.group(2), ctx, table)
        if not col:
            return None
        return f"SELECT {col}, COUNT(*) AS {table.rstrip('s')}_count FROM {table} GROUP BY {col}"

    def _count(self, q: str, ctx) -> str | None:
        m = re.search(r"\bhow many\b(.*)$", q, re.I)
        if not m:
            return None
        ents = self._entities(m.group(1), ctx)
        if not ents:
            return None
        table = ents[0]
        aliases = {table: table}
        extra = []
        date = self._date_pred(q, ctx, aliases)
        if date:
            extra.append(date)
        where = self._where(q, ctx, aliases, extra)
        # A bare COUNT(*) references no column, so SQL Server checks it against
        # every column and refuses it (error 230) when any column is DENYed.
        # Counting the primary key gives the same answer and passes the check.
        pk = _pk(ctx.schema.table(table))
        what = pk if pk and not where else "*"
        return f"SELECT COUNT({what}) AS {table.rstrip('s')}_count FROM {table}{where}"

    def _list(self, q: str, ctx) -> str | None:
        if not re.match(r"\s*(list|show|which|what are|give me|find)\b", q, re.I):
            return None
        ents = self._entities(q, ctx)
        if not ents:
            return None
        table = ents[0]
        t = ctx.schema.table(table)
        cols = self._display(t)
        extra_col = None
        m = re.search(r"\bwith (?:their |its )?(.+?)[?.]?$", q, re.I)
        if m:
            extra_col = self._column_for(m.group(1), ctx, table)
            if extra_col:
                cols = [c for c in cols if c != _pk(t)] + [extra_col]
        aliases = {table: table}
        where = self._where(q, ctx, aliases)
        where = where.replace(f"{table}.", "")
        return f"SELECT {', '.join(cols)} FROM {table}{where}"
