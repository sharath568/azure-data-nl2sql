"""The ask pipeline: retrieve -> generate -> guard -> execute -> redact."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from nl2sql import guardrails
from nl2sql.db.base import Backend, BackendError, QueryTimeout, ResultSet
from nl2sql.guardrails import GuardReport
from nl2sql.guardrails.base import CheckResult
from nl2sql.providers.base import Generation, GenerationContext, Provider, ProviderError
from nl2sql.schema import Retrieval, Schema, render_card, retrieve
from nl2sql.semantic import SemanticLayer


@dataclass
class AskResult:
    question: str
    status: str  # ok | no_sql | blocked | timeout | error
    retrieval: Retrieval | None = None
    card: str = ""
    generation: Generation | None = None
    guard: GuardReport | None = None
    result: ResultSet | None = None
    redaction: CheckResult | None = None
    error: str | None = None
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def sql(self) -> str | None:
        return self.guard.sql if self.guard and self.guard.allowed else None

    def to_dict(self, *, include_card: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "question": self.question,
            "status": self.status,
            "tables": self.retrieval.tables if self.retrieval else [],
            "generation": self.generation.to_dict() if self.generation else None,
            "guardrails": self.guard.to_dict() if self.guard else None,
            "result": self.result.to_dict() if self.result else None,
            "redaction": self.redaction.to_dict() if self.redaction else None,
            "error": self.error,
            "timings_ms": {k: round(v, 2) for k, v in self.timings_ms.items()},
        }
        if include_card:
            d["schema_card"] = self.card
        return d


class Engine:
    def __init__(
        self,
        backend: Backend,
        provider: Provider,
        layer: SemanticLayer,
        *,
        sample_values: bool | None = None,
        max_tables: int = 6,
    ):
        self.backend = backend
        self.provider = provider
        self.layer = layer
        self.sample_values = (
            provider.wants_sample_values if sample_values is None else sample_values
        )
        self.max_tables = max_tables
        self._schema: Schema | None = None
        self._visible: Schema | None = None

    @property
    def schema(self) -> Schema:
        """The full introspected schema (denied columns included, for the guardrails)."""
        if self._schema is None:
            self._schema = self.backend.introspect(
                sample_values=self.sample_values,
                schemas=self.layer.policy.allowed_schemas,
                # never pull sample values out of a denied column
                sample_filter=lambda t, c: not self.layer.is_column_denied(t, c),
            )
        return self._schema

    @property
    def visible_schema(self) -> Schema:
        """What the model is allowed to see."""
        if self._visible is None:
            self._visible = self.schema.visible(self.layer)
        return self._visible

    def context(self, question: str) -> tuple[GenerationContext, Retrieval]:
        visible = self.visible_schema
        r = retrieve(question, visible, self.layer, max_tables=self.max_tables)
        card = render_card(visible, self.layer, r.tables, r.metrics)
        ctx = GenerationContext(
            question=question,
            card=card,
            schema=visible,
            layer=self.layer,
            tables=r.tables,
            metrics=r.metrics,
            max_rows=self.layer.policy.max_rows,
        )
        return ctx, r

    def check(self, sql: str) -> GuardReport:
        return guardrails.check(sql, self.layer, schema=self.schema, backend=self.backend)

    def ask(self, question: str, *, execute: bool = True) -> AskResult:
        t0 = time.perf_counter()
        timings: dict[str, float] = {}

        def lap(name: str, since: float) -> float:
            now = time.perf_counter()
            timings[name] = (now - since) * 1000
            return now

        ctx, r = self.context(question)
        t = lap("retrieval", t0)
        out = AskResult(question, "no_sql", retrieval=r, card=ctx.card, timings_ms=timings)
        try:
            gen = self.provider.generate(ctx)
        except ProviderError as e:
            out.status, out.error = "error", f"provider: {e}"
            lap("total", t0)
            return out
        out.generation = gen
        t = lap("generation", t)
        if not gen.answered:
            lap("total", t0)
            return out

        out.guard = self.check(gen.sql or "")
        t = lap("guardrails", t)
        if not out.guard.allowed:
            out.status = "blocked"
            lap("total", t0)
            return out
        if not execute:
            out.status = "ok"
            lap("total", t0)
            return out

        policy = self.layer.policy
        try:
            result = self.backend.execute(
                out.guard.sql, max_rows=policy.max_rows, timeout_s=policy.timeout_seconds
            )
        except QueryTimeout as e:
            out.status, out.error = "timeout", str(e)
            lap("execution", t)
            lap("total", t0)
            return out
        except BackendError as e:
            out.status, out.error = "error", f"database: {e}"
            lap("execution", t)
            lap("total", t0)
            return out
        t = lap("execution", t)
        out.result, out.redaction = guardrails.redact(result, self.layer, r.tables)
        out.status = "ok"
        lap("total", t0)
        return out
