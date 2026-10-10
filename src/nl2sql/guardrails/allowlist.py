"""(d) Table and column allow-list, from the semantic layer and the live schema.

Tables:

* system catalogs (``sys.*``, ``INFORMATION_SCHEMA.*``, ``master``/``msdb``...)
  are always blocked,
* the schema prefix must be in ``policy.allowed_schemas`` (default ``dbo``),
* the table must be in ``policy.allowed_tables`` if that is set, otherwise in
  the introspected schema (when we have one),
* ``policy.denied_tables`` patterns win over everything.

Columns:

* a denied column (``policy.denied_columns``, ``sensitive: true`` in the
  semantic layer, or a name matching ``policy.denied_column_patterns`` such as
  ``*salary*``) is blocked **anywhere** in the query, not only in the SELECT
  list. ``WHERE salary > 150000`` and ``ORDER BY salary`` leak the value one
  bit at a time,
* ``SELECT *`` (and ``t.*``, and ``BINARY_CHECKSUM(*)``-style functions) over a
  table that has a denied column is blocked, because the star would expand to
  include it. ``COUNT(*)`` is fine,
* a CTE that reuses the name of a real table is blocked, because it makes the
  query mean something different from what it looks like.

Column checks are deliberately name-based and conservative: every reference to
a base column has to appear by its real name somewhere in the tree, so aliasing
(``SELECT x.s FROM (SELECT salary AS s FROM employees) x``) does not hide it.
A false positive (an unrelated CTE column called ``salary``) is the price.
"""

from __future__ import annotations

from sqlglot import exp

from nl2sql.guardrails.base import CheckResult, Guardrail, GuardState, cte_names

SYSTEM_SCHEMAS = {"sys", "information_schema"}
SYSTEM_DATABASES = {"master", "msdb", "tempdb", "model", "mssqlsystemresource"}
LEGACY_SYSTEM_TABLES = {
    "sysobjects", "syscolumns", "sysusers", "syslogins", "sysxlogins", "sysdatabases",
    "sysprocesses", "syscomments", "sysindexes", "sysmembers", "syspermissions",
    "sysservers", "sysfiles", "sysconfigures", "syscacheobjects", "syslockinfo",
    "sysperfinfo", "sysprotects", "sysreferences", "systypes", "sysforeignkeys",
}  # fmt: skip


class AllowListGuard(Guardrail):
    name = "allow_list"
    layer = "d"
    description = (
        "Only allowed tables; denied/sensitive columns blocked anywhere; no SELECT * over them."
    )

    def run(self, state: GuardState) -> CheckResult:
        tree = state.tree
        assert tree is not None
        layer = state.layer
        policy = layer.policy
        schema = state.schema
        problems: list[str] = []

        if policy.allowed_tables is not None:
            allowed_tables: set[str] | None = {t.lower() for t in policy.allowed_tables}
        elif schema is not None:
            allowed_tables = set(schema.tables)
        else:
            allowed_tables = None
        allowed_schemas = {s.lower() for s in policy.allowed_schemas}

        ctes = cte_names(tree)
        real_names = set(schema.tables) if schema is not None else set()
        if allowed_tables:
            real_names |= allowed_tables
        for name in sorted(ctes & real_names):
            problems.append(f"CTE name '{name}' shadows a real table")

        alias_to_table: dict[str, str] = {}
        referenced: set[str] = set()
        for table in tree.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                continue  # table-valued functions are handled by select_only
            name = table.name.lower()
            db = table.db.lower() if table.args.get("db") else ""
            catalog = table.catalog.lower() if table.args.get("catalog") else ""
            if not db and not catalog and name in ctes:
                continue
            if db in SYSTEM_SCHEMAS or catalog in SYSTEM_DATABASES or name in LEGACY_SYSTEM_TABLES:
                problems.append(f"system catalog access is not allowed ({table.sql('tsql')})")
                continue
            if db and db not in allowed_schemas:
                problems.append(
                    f"schema '{db}' is not allowed (allowed: {sorted(allowed_schemas)})"
                )
                continue
            if policy.is_table_denied(name):
                problems.append(f"table '{name}' is denied by policy")
                continue
            if allowed_tables is not None and name not in allowed_tables:
                problems.append(f"table '{name}' is not in the allow-list")
                continue
            referenced.add(name)
            alias_to_table[table.alias_or_name.lower()] = name

        denied_by_table: dict[str, set[str]] = {}
        for name in referenced:
            cols = set()
            if schema is not None and schema.table(name):
                cols = {
                    c.name.lower()
                    for c in schema.table(name).columns
                    if layer.is_column_denied(name, c.name)
                }
            cols |= {
                d.split(".", 1)[1].lower()
                for d in policy.denied_columns
                if "." in d and d.split(".", 1)[0].lower() == name
            }
            cols |= {c for c, doc in layer.table_doc(name).columns.items() if doc.sensitive}
            if cols:
                denied_by_table[name] = cols
        denied_names = set().union(*denied_by_table.values()) if denied_by_table else set()

        for col in tree.find_all(exp.Column):
            if isinstance(col.this, exp.Star):
                continue
            cname = col.name.lower()
            qualifier = col.table.lower() if col.table else ""
            owner = alias_to_table.get(qualifier, qualifier) or None
            if cname in denied_names or layer.is_column_denied(owner, cname):
                problems.append(f"column '{cname}' is denied by policy")

        for star in tree.find_all(exp.Star):
            problems.extend(self._star_problem(star, alias_to_table, denied_by_table, ctes))

        problems = list(dict.fromkeys(problems))
        if problems:
            return self.block(problems[0], problems)
        msg = f"tables: {', '.join(sorted(referenced)) or '(none)'}"
        if allowed_tables is None:
            return self.warn(msg + "; no schema or allow-list available, table check skipped")
        return self.ok(msg)

    @staticmethod
    def _star_problem(star, alias_to_table, denied_by_table, ctes) -> list[str]:
        parent = star.parent
        if isinstance(parent, exp.Count):
            return []
        qualifier = ""
        if isinstance(parent, exp.Column):
            qualifier = parent.table.lower() if parent.table else ""
        select = star.find_ancestor(exp.Select)
        if select is None:
            return []
        sources: list[exp.Table] = []
        from_ = select.args.get("from_") or select.args.get("from")
        if from_ is not None and isinstance(from_.this, exp.Table):
            sources.append(from_.this)
        for join in select.args.get("joins") or []:
            if isinstance(join.this, exp.Table):
                sources.append(join.this)
        out = []
        for src in sources:
            if not isinstance(src.this, exp.Identifier):
                continue
            name = src.name.lower()
            if not src.args.get("db") and name in ctes:
                continue
            if qualifier and qualifier not in (src.alias_or_name.lower(), name):
                continue
            base = alias_to_table.get(src.alias_or_name.lower(), name)
            if base in denied_by_table:
                cols = ", ".join(sorted(denied_by_table[base]))
                out.append(
                    f"SELECT * over '{base}' would expose denied column(s) {cols}; "
                    "list the columns explicitly"
                )
        return out
