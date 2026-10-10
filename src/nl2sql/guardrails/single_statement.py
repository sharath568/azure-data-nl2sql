"""(b) Exactly one statement. ``SELECT 1; DROP TABLE orders`` is two statements,
and the second one is the attack. A trailing semicolon is fine."""

from __future__ import annotations

from nl2sql.guardrails.base import CheckResult, Guardrail, GuardState


class SingleStatementGuard(Guardrail):
    name = "single_statement"
    layer = "b"
    description = "Exactly one statement; no batches, no semicolon chaining."

    def run(self, state: GuardState) -> CheckResult:
        n = len(state.statements)
        if n != 1:
            kinds = ", ".join(type(s).__name__.upper() for s in state.statements)
            return self.block(f"expected 1 statement, found {n} ({kinds})")
        state.tree = state.statements[0]
        return self.ok("1 statement")
