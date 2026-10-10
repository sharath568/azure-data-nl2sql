"""(e) Row limit. Every query gets a cap of ``policy.max_rows``.

* no limit           -> ``TOP (n)`` is injected
* ``TOP (m)``, m > n -> clamped to n
* ``TOP (m) PERCENT``-> replaced by ``TOP (n)``
* ``OFFSET ... FETCH NEXT m ROWS`` -> FETCH clamped (or added) instead of TOP,
  because T-SQL does not allow TOP together with OFFSET
* ``UNION`` / ``INTERSECT`` / ``EXCEPT`` -> wrapped in
  ``SELECT TOP (n) * FROM (...) AS _q``, keeping a simple ORDER BY outside

The executor also only ever fetches ``n + 1`` rows, so a query that slips past
the rewrite still cannot pull a whole table into memory.
"""

from __future__ import annotations

from sqlglot import exp

from nl2sql.guardrails.base import CheckResult, Guardrail, GuardState


def _literal_int(node: exp.Expression | None) -> int | None:
    while isinstance(node, (exp.Paren, exp.Subquery)) and node.this is not None:
        node = node.this
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return int(float(node.this))
        except ValueError:
            return None
    return None


def _order_is_portable(order: exp.Order | None) -> bool:
    """An ORDER BY can move outside a derived table if it only uses unqualified
    names or ordinals."""
    if order is None:
        return True
    for ordered in order.expressions:
        target = ordered.this
        if isinstance(target, exp.Literal) and not target.is_string:
            continue
        if isinstance(target, exp.Column) and not target.table:
            continue
        return False
    return True


class RowLimitGuard(Guardrail):
    name = "row_limit"
    layer = "e"
    description = "Inject or clamp TOP / FETCH to policy.max_rows."

    def run(self, state: GuardState) -> CheckResult:
        cap = state.policy.max_rows
        tree = state.tree
        assert tree is not None
        root = tree
        while isinstance(root, exp.Subquery):
            root = root.this

        if isinstance(root, exp.SetOperation):
            return self._wrap_set_operation(state, root, cap)
        if not isinstance(root, exp.Select):  # select_only already blocked anything else
            return self.block("row limit can only be applied to a SELECT")

        limit = root.args.get("limit")
        if isinstance(limit, exp.Fetch):
            current = _literal_int(limit.args.get("count"))
            if current is not None and current <= cap:
                return self.ok(f"FETCH NEXT {current} ROWS is within the cap of {cap}")
            limit.set("count", exp.Literal.number(cap))
            state.tree, state.sql = tree, tree.sql(dialect="tsql")
            return self.rewrite(f"FETCH clamped to {cap} rows")
        if root.args.get("offset") is not None and limit is None:
            root.set(
                "limit",
                exp.Fetch(direction="NEXT", count=exp.Literal.number(cap)),
            )
            state.tree, state.sql = tree, tree.sql(dialect="tsql")
            return self.rewrite(f"added FETCH NEXT {cap} ROWS ONLY")
        if isinstance(limit, exp.Limit):
            opts = limit.args.get("limit_options")
            percent = bool(opts and opts.args.get("percent"))
            current = _literal_int(limit.args.get("expression"))
            if not percent and current is not None and current <= cap:
                return self.ok(f"TOP {current} is within the cap of {cap}")
            root.set("limit", None)
            root.limit(cap, copy=False)
            state.tree, state.sql = tree, tree.sql(dialect="tsql")
            why = "TOP PERCENT replaced" if percent else f"TOP {current} clamped"
            return self.rewrite(f"{why}: TOP {cap}")
        root.limit(cap, copy=False)
        state.tree, state.sql = tree, tree.sql(dialect="tsql")
        return self.rewrite(f"added TOP {cap}")

    def _wrap_set_operation(
        self, state: GuardState, root: exp.SetOperation, cap: int
    ) -> CheckResult:
        order = root.args.get("order")
        if not _order_is_portable(order):
            return self.warn(
                "set operation with a complex ORDER BY was not rewritten; "
                f"the executor still stops after {cap} rows"
            )
        inner = root.copy()
        inner.set("order", None)
        # A CTE cannot live inside a derived table in T-SQL; hoist it to the wrapper.
        with_key = "with_" if "with_" in inner.arg_types else "with"
        ctes = inner.args.get(with_key)
        inner.set(with_key, None)
        wrapped = (
            exp.select("*")
            .from_(exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier("_q"))))
            .limit(cap)
        )
        if order is not None:
            wrapped.set("order", order.copy())
        if ctes is not None:
            wrapped.set(with_key, ctes)
        state.tree, state.sql = wrapped, wrapped.sql(dialect="tsql")
        return self.rewrite(f"wrapped set operation in SELECT TOP {cap}")
