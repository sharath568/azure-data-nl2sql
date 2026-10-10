"""Evaluation: result comparison, exact match, gold-set loading, the runner and the report."""

from __future__ import annotations

import datetime as dt
import decimal
import json

import pytest

from nl2sql.evaluation import (
    exact_match,
    load_adversarial,
    load_gold,
    normalize_sql,
    results_match,
    run_eval,
    to_markdown,
    write,
)
from nl2sql.evaluation.compare import has_top_level_order
from nl2sql.evaluation.runner import _pct


@pytest.mark.parametrize(
    ("gold", "pred", "ordered", "expected"),
    [
        ([(1, "a"), (2, "b")], [(2, "b"), (1, "a")], False, True),
        ([(1, "a"), (2, "b")], [(2, "b"), (1, "a")], True, False),
        ([(1, "a")], [("a", 1)], False, True),  # column order may differ
        ([(1, "a")], [(1, "a", "extra")], False, False),  # column count may not
        ([(1, "a")], [(1, "a"), (1, "a")], False, False),  # multiset, not set
        ([(10.004,)], [(10.0,)], False, True),  # tolerance
        ([(10.5,)], [(10.0,)], False, False),
        ([(decimal.Decimal("3.10"),)], [(3.1,)], False, True),
        ([(dt.date(2025, 1, 2),)], [("2025-01-02",)], False, True),
        ([(True,)], [(1,)], False, True),
        ([(None,)], [(None,)], False, True),
        ([], [], False, True),
        ([(1,)], [], False, False),
    ],
)
def test_results_match(gold, pred, ordered, expected):
    assert results_match(gold, pred, ordered=ordered) is expected


def test_exact_match_normalises_case_and_whitespace():
    assert exact_match("SELECT a FROM t", "select   a\nfrom T")
    assert not exact_match("SELECT a FROM t", "SELECT b FROM t")
    assert not exact_match("SELECT a FROM t", None)
    assert normalize_sql("not sql (") == "not sql ("


def test_top_level_order():
    assert has_top_level_order("SELECT a FROM t ORDER BY a")
    assert not has_top_level_order("SELECT a FROM (SELECT TOP 1 a FROM t ORDER BY a) x")
    assert not has_top_level_order("not sql (")


def test_bundled_sets_are_the_promised_size():
    gold = load_gold()
    adv = load_adversarial()
    assert len(gold) >= 40
    assert len(adv) >= 15
    assert len({g.id for g in gold}) == len(gold)
    assert all(a.expect_blocked_by for a in adv)
    assert {"ambiguous", "join", "aggregate", "date"} <= {t for g in gold for t in g.tags}


def test_every_gold_query_runs_and_passes_guardrails(engine):
    from nl2sql import guardrails

    for item in load_gold():
        report = guardrails.check(item.sql, engine.layer, schema=engine.schema)
        assert report.allowed, (item.id, report.reason)
        engine.backend.execute(item.sql, max_rows=1000, timeout_s=10)


def test_gold_loader_validates(tmp_path):
    f = tmp_path / "g.yaml"
    f.write_text("items:\n  - id: x\n    question: q\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no sql"):
        load_gold(f)
    f.write_text(
        "items:\n  - {id: a, question: q, sql: SELECT 1}\n  - {id: a, question: r, sql: SELECT 1}\n"
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_gold(f)
    f.write_text("items: 3\n")
    with pytest.raises(ValueError, match="expected a list"):
        load_gold(f)


def test_pct():
    assert _pct([], 50) == 0.0
    assert _pct([5.0], 95) == 5.0
    assert _pct([1, 2, 3, 4, 100], 50) == 3


@pytest.fixture(scope="module")
def report(sample_db, layer):
    from nl2sql.db.sqlite import SQLiteBackend
    from nl2sql.engine import Engine
    from nl2sql.providers.stub import TemplateProvider

    b = SQLiteBackend(sample_db)
    try:
        return run_eval(Engine(b, TemplateProvider(), layer), load_gold(), load_adversarial())
    finally:
        b.close()


def test_eval_runs_offline(report):
    s = report.summary()
    assert s["gold"]["questions"] == len(load_gold())
    assert 0 < s["gold"]["execution_accuracy"] < 1  # the stub is not perfect, and says so
    assert s["gold"]["gold_sql_blocked_by_guardrails"] == 0
    assert s["adversarial"]["executed"] == []
    assert s["adversarial"]["block_rate"] == 1.0
    assert s["tokens"]["estimated"] is True
    assert s["tokens"]["cost_model"] == "gpt-4.1-mini"
    assert report.is_stub


def test_adversarial_caught_by_the_expected_guardrail(report):
    misses = [
        (a.id, a.blocked_by, a.expected) for a in report.adversarial if a.blocked_by != a.expected
    ]
    assert misses == []


def test_report_markdown_is_labelled(report):
    md = to_markdown(report)
    assert "not a\n> language model" in md
    assert "Execution accuracy" in md and "Adversarial block rate" in md
    assert "| a01 |" in md and "| g01 |" in md


def test_report_files(report, tmp_path):
    md, js = write(report, tmp_path, "x")
    data = json.loads(js.read_text())
    assert data["is_stub"] is True
    assert data["summary"]["adversarial"]["questions"] == len(load_adversarial())
    assert md.read_text().startswith("# nl2sql eval: stub on sqlite")
