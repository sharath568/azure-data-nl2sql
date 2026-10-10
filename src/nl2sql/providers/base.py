"""Provider interface: question + schema card in, T-SQL + explanation out."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from nl2sql.schema import Schema
from nl2sql.semantic import SemanticLayer


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: True when the numbers are an estimate (characters / 4), not reported by an API
    estimated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class GenerationContext:
    question: str
    card: str
    schema: Schema  # the *visible* schema: denied tables/columns already removed
    layer: SemanticLayer
    tables: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    max_rows: int = 100


@dataclass
class Generation:
    sql: str | None
    explanation: str = ""
    tables_used: list[str] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    usage: Usage | None = None
    latency_ms: float = 0.0
    raw: str | None = None

    @property
    def answered(self) -> bool:
        return bool(self.sql and self.sql.strip())

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "sql": self.sql,
            "explanation": self.explanation,
            "tables_used": self.tables_used,
            "provider": self.provider,
            "model": self.model,
            "latency_ms": round(self.latency_ms, 2),
        }
        if self.usage:
            d["usage"] = {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "estimated": self.usage.estimated,
            }
        return d


class ProviderError(RuntimeError):
    pass


class Provider(ABC):
    name: str = "provider"
    model: str = ""
    #: whether the engine should introspect low-cardinality sample values for this provider
    wants_sample_values: bool = False

    @abstractmethod
    def generate(self, ctx: GenerationContext) -> Generation: ...
