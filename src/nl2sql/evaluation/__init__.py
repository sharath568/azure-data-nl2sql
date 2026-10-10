"""Evaluation harness: execution accuracy, exact match, guardrail block rate, latency, cost."""

from nl2sql.evaluation.compare import exact_match, normalize_sql, results_match
from nl2sql.evaluation.goldset import AdversarialItem, GoldItem, load_adversarial, load_gold
from nl2sql.evaluation.report import to_markdown, write
from nl2sql.evaluation.runner import EvalReport, run_eval

__all__ = [
    "AdversarialItem",
    "EvalReport",
    "GoldItem",
    "exact_match",
    "load_adversarial",
    "load_gold",
    "normalize_sql",
    "results_match",
    "run_eval",
    "to_markdown",
    "write",
]
