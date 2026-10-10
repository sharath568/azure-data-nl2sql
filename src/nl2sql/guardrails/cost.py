"""(f) Cost guard.

Two parts, and it is worth being clear about what each one can and cannot do.

**Static checks (always on, work offline):**

* too many joins (``policy.cost.max_joins``),
* subqueries nested too deep (``policy.cost.max_subquery_depth``),
* cartesian products: ``CROSS JOIN`` between base tables, or comma joins with
  no WHERE clause. A cross join against a derived table or CTE (the usual
  "share of total" pattern) is allowed.

These are heuristics. They catch the obvious runaway query; they do not know
how big your tables are.

**Plan estimate (SQL Server / Azure SQL only):** ``SET SHOWPLAN_XML ON`` asks
the optimizer for ``StatementEstRows`` and ``StatementSubTreeCost`` *without
running the query*, and the guard blocks if either is over the limit. This
needs the SHOWPLAN permission and is only as good as the statistics.

SQLite has no cost model (``EXPLAIN QUERY PLAN`` gives a plan shape, not
numbers), so offline this part is reported as SKIP rather than pretending.
TODO: a row-count-based estimate from table sizes would be a cheap improvement.

The statement timeout in the executor is the backstop for everything this
misses.
"""

from __future__ import annotations

from sqlglot import exp

from nl2sql.db.base import BackendError
from nl2sql.guardrails.base import CheckResult, Guardrail, GuardState, Status, cte_names


def _select_depth(node: exp.Expression) -> int:
    depth = 0
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.Select):
            depth += 1
        parent = parent.parent
    return depth


class CostGuard(Guardrail):
    name = "cost"
    layer = "f"
    description = "Static join/nesting/cartesian checks; optimizer estimate on SQL Server."

    def run(self, state: GuardState) -> CheckResult:
        tree = state.tree
        assert tree is not None
        cfg = state.policy.cost
        problems: list[str] = []
        notes: list[str] = []

        joins = list(tree.find_all(exp.Join))
        if len(joins) > cfg.max_joins:
            problems.append(f"{len(joins)} joins; the limit is {cfg.max_joins}")

        depth = max((_select_depth(s) for s in tree.find_all(exp.Select)), default=0)
        if depth > cfg.max_subquery_depth:
            problems.append(
                f"subqueries nested {depth} deep; the limit is {cfg.max_subquery_depth}"
            )

        if cfg.block_cartesian:
            ctes = cte_names(tree)
            for join in joins:
                target = join.this
                if not isinstance(target, exp.Table) or not isinstance(target.this, exp.Identifier):
                    continue  # derived table, APPLY, function
                if target.name.lower() in ctes and not target.args.get("db"):
                    continue
                if join.args.get("on") is not None or join.args.get("using"):
                    continue
                kind = (join.args.get("kind") or "").upper()
                select = join.find_ancestor(exp.Select)
                has_where = select is not None and select.args.get("where") is not None
                if kind == "CROSS" or not has_where:
                    problems.append(f"cartesian product with '{target.name}' (no join condition)")

        if problems:
            return self.block(problems[0], problems)
        notes.append(f"static: {len(joins)} join(s), nesting depth {depth}")

        if not cfg.use_plan_estimates or state.backend is None:
            notes.append("plan estimate: skipped (no database connection)")
            return CheckResult(self.name, Status.PASS, "static checks passed", notes)
        try:
            est = state.backend.estimate_cost(state.sql)
        except BackendError as e:
            return self.warn("static checks passed; plan estimate failed", [*notes, str(e)])
        if est is None:
            notes.append(
                f"plan estimate: not available on {state.backend.name} "
                "(no cost model offline; TODO); relying on static checks and the timeout"
            )
            return CheckResult(self.name, Status.PASS, "static checks passed", notes)
        notes.append(
            f"plan estimate ({est.source}): {est.estimated_rows:,.0f} rows, "
            f"cost {est.estimated_cost:.3f}"
        )
        if est.estimated_rows > cfg.max_estimated_rows:
            return self.block(
                f"optimizer estimates {est.estimated_rows:,.0f} rows "
                f"(limit {cfg.max_estimated_rows:,.0f})",
                notes,
            )
        if est.estimated_cost > cfg.max_estimated_cost:
            return self.block(
                f"optimizer cost {est.estimated_cost:.2f} is over the limit "
                f"{cfg.max_estimated_cost:.2f}",
                notes,
            )
        return self.ok("within cost limits", notes)
