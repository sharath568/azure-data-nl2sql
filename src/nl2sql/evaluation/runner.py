"""Run a provider over the gold and adversarial sets and score it."""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from nl2sql import __version__, guardrails, pricing
from nl2sql.db.base import BackendError, QueryTimeout, jsonable
from nl2sql.engine import Engine
from nl2sql.evaluation.compare import exact_match, has_top_level_order, results_match
from nl2sql.evaluation.goldset import AdversarialItem, GoldItem


@dataclass
class GoldResult:
    id: str
    question: str
    tags: list[str]
    status: str  # correct | wrong | no_sql | blocked | error | timeout
    correct: bool
    exact: bool
    gold_sql: str
    pred_sql: str | None
    executed_sql: str | None
    blocked_by: str | None = None
    error: str | None = None
    gold_rows: int = 0
    pred_rows: int = 0
    total_ms: float = 0.0
    generation_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class AdversarialResult:
    id: str
    question: str
    attack: str
    outcome: str  # blocked | refused | executed | error
    blocked_by: str | None
    expected: str | None
    sql: str | None
    reason: str = ""

    @property
    def stopped(self) -> bool:
        return self.outcome in ("blocked", "refused")


@dataclass
class EvalReport:
    provider: str
    model: str
    backend: str
    started_at: str
    gold: list[GoldResult] = field(default_factory=list)
    adversarial: list[AdversarialResult] = field(default_factory=list)
    gold_sql_blocked: list[dict[str, str]] = field(default_factory=list)
    tokens_estimated: bool = False
    version: str = __version__

    # ---- metrics ---------------------------------------------------------

    @property
    def is_stub(self) -> bool:
        return self.provider == "stub"

    def summary(self) -> dict[str, Any]:
        n = len(self.gold)
        correct = sum(r.correct for r in self.gold)
        exact = sum(r.exact for r in self.gold)
        statuses: dict[str, int] = {}
        for r in self.gold:
            statuses[r.status] = statuses.get(r.status, 0) + 1
        by_tag: dict[str, list[int]] = {}
        for r in self.gold:
            for t in r.tags:
                by_tag.setdefault(t, [0, 0])
                by_tag[t][0] += int(r.correct)
                by_tag[t][1] += 1
        totals = [r.total_ms for r in self.gold if r.total_ms]
        gens = [r.generation_ms for r in self.gold if r.generation_ms]
        prompt = sum(r.prompt_tokens for r in self.gold)
        completion = sum(r.completion_tokens for r in self.gold)
        model_for_cost = pricing.DEFAULT_MODEL if self.is_stub else self.model
        cost = pricing.cost_usd(prompt, completion, model_for_cost)
        adv_n = len(self.adversarial)
        adv_outcomes: dict[str, int] = {}
        adv_by_guard: dict[str, int] = {}
        for a in self.adversarial:
            adv_outcomes[a.outcome] = adv_outcomes.get(a.outcome, 0) + 1
            if a.blocked_by:
                adv_by_guard[a.blocked_by] = adv_by_guard.get(a.blocked_by, 0) + 1
        stopped = sum(a.stopped for a in self.adversarial)
        expected_ok = sum(
            1
            for a in self.adversarial
            if a.expected and a.outcome == "blocked" and a.blocked_by == a.expected
        )
        return {
            "gold": {
                "questions": n,
                "execution_accuracy": _ratio(correct, n),
                "correct": correct,
                "exact_match_rate": _ratio(exact, n),
                "exact_matches": exact,
                "statuses": statuses,
                "by_tag": {
                    t: {"correct": c, "total": tot, "accuracy": _ratio(c, tot)}
                    for t, (c, tot) in sorted(by_tag.items())
                },
                "gold_sql_blocked_by_guardrails": len(self.gold_sql_blocked),
            },
            "latency_ms": {
                "total_p50": _pct(totals, 50),
                "total_p95": _pct(totals, 95),
                "generation_p50": _pct(gens, 50),
                "generation_p95": _pct(gens, 95),
            },
            "tokens": {
                "prompt_total": prompt,
                "completion_total": completion,
                "prompt_per_question": round(prompt / n, 1) if n else 0,
                "completion_per_question": round(completion / n, 1) if n else 0,
                "estimated": self.tokens_estimated,
                "cost_model": model_for_cost,
                "cost_usd_total": round(cost, 6) if cost is not None else None,
                "cost_usd_per_question": round(cost / n, 6) if cost is not None and n else None,
                "prices_checked": pricing.PRICES_CHECKED,
            },
            "adversarial": {
                "questions": adv_n,
                "block_rate": _ratio(stopped, adv_n),
                "stopped": stopped,
                "outcomes": adv_outcomes,
                "blocked_by": dict(sorted(adv_by_guard.items())),
                "caught_by_expected_guardrail": expected_ok,
                "executed": [a.id for a in self.adversarial if a.outcome == "executed"],
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "nl2sql_version": self.version,
            "provider": self.provider,
            "model": self.model,
            "backend": self.backend,
            "started_at": self.started_at,
            "is_stub": self.is_stub,
            "summary": self.summary(),
            "gold_sql_blocked": self.gold_sql_blocked,
            "gold": [asdict(r) for r in self.gold],
            "adversarial": [asdict(a) for a in self.adversarial],
        }


def _ratio(a: int, b: int) -> float:
    return round(a / b, 4) if b else 0.0


def _pct(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return round(values[0], 2)
    q = statistics.quantiles(values, n=100, method="inclusive")
    return round(q[p - 1], 2)


def run_gold_item(engine: Engine, item: GoldItem, max_rows: int = 10_000) -> GoldResult:
    backend = engine.backend
    policy = engine.layer.policy
    gold = backend.execute(item.sql, max_rows=max_rows, timeout_s=max(policy.timeout_seconds, 30))
    res = engine.ask(item.question)
    gen = res.generation
    usage = gen.usage if gen else None
    out = GoldResult(
        id=item.id,
        question=item.question,
        tags=item.tags,
        status=res.status,
        correct=False,
        exact=exact_match(item.sql, gen.sql if gen else None),
        gold_sql=item.sql,
        pred_sql=gen.sql if gen else None,
        executed_sql=res.sql,
        blocked_by=res.guard.blocked_by if res.guard else None,
        error=res.error or (res.guard.reason if res.guard and not res.guard.allowed else None),
        gold_rows=len(gold.rows),
        total_ms=res.timings_ms.get("total", 0.0),
        generation_ms=res.timings_ms.get("generation", 0.0),
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
    )
    if res.status == "ok" and res.result is not None:
        out.pred_rows = len(res.result.rows)
        out.correct = results_match(
            gold.rows, res.result.rows, ordered=has_top_level_order(item.sql)
        )
        out.status = "correct" if out.correct else "wrong"
    return out


def run_adversarial_item(engine: Engine, item: AdversarialItem) -> AdversarialResult:
    res = engine.ask(item.question)
    gen = res.generation
    if res.status == "blocked":
        outcome = "blocked"
    elif res.status == "no_sql":
        outcome = "refused"
    elif res.status == "ok":
        outcome = "executed"
    else:
        outcome = "error"
    reason = ""
    if res.guard and not res.guard.allowed:
        reason = res.guard.reason
    elif gen and not gen.answered:
        reason = gen.explanation
    elif res.error:
        reason = res.error
    return AdversarialResult(
        id=item.id,
        question=item.question,
        attack=item.attack,
        outcome=outcome,
        blocked_by=res.guard.blocked_by if res.guard else None,
        expected=item.expect_blocked_by,
        sql=gen.sql if gen else None,
        reason=reason,
    )


def run_eval(
    engine: Engine,
    gold: list[GoldItem],
    adversarial: list[AdversarialItem],
    *,
    progress: Callable[[str], None] | None = None,
) -> EvalReport:
    report = EvalReport(
        provider=engine.provider.name,
        model=engine.provider.model,
        backend=engine.backend.name,
        started_at=dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
    )
    # The gold SQL itself must get through the guardrails, or the guardrails
    # are blocking legitimate questions (false positives).
    for item in gold:
        check = guardrails.check(item.sql, engine.layer, schema=engine.schema)
        if not check.allowed:
            report.gold_sql_blocked.append(
                {"id": item.id, "blocked_by": check.blocked_by or "", "reason": check.reason}
            )
    for item in gold:
        try:
            r = run_gold_item(engine, item)
        except (BackendError, QueryTimeout) as e:
            r = GoldResult(item.id, item.question, item.tags, "error", False, False, item.sql,
                           None, None, error=f"gold SQL failed: {e}")  # fmt: skip
        report.gold.append(r)
        if r.prompt_tokens and engine.provider.name == "stub":
            report.tokens_estimated = True
        if progress:
            progress(f"{r.id} {r.status}")
    for item in adversarial:
        a = run_adversarial_item(engine, item)
        report.adversarial.append(a)
        if progress:
            progress(f"{a.id} {a.outcome}")
    return report


def jsonify(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonify(v) for v in obj]
    return jsonable(obj)
