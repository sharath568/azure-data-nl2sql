"""Markdown and JSON output for an eval run."""

from __future__ import annotations

import json
from pathlib import Path

from nl2sql.evaluation.runner import EvalReport, jsonify

STUB_BANNER = """\
> **This report was produced by the offline stub provider, which is not a
> language model.** It is a set of regex templates written for this sample
> schema. The execution accuracy below measures how many templates exist, not
> how well natural-language-to-SQL works. On adversarial questions the stub
> deliberately emits the malicious SQL (it plays a fully compromised model),
> so the block rate measures the **guardrails**, which is the useful part.
> Token counts are estimated from the prompt that *would* be sent
> (characters / 4) and priced as if it were gpt-4.1-mini.
"""


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def _cell(text: str | None, limit: int = 90) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def to_markdown(report: EvalReport) -> str:
    s = report.summary()
    g, lat, tok, adv = s["gold"], s["latency_ms"], s["tokens"], s["adversarial"]
    lines = [f"# nl2sql eval: {report.provider} on {report.backend}", ""]
    if report.is_stub:
        lines += [STUB_BANNER, ""]
    outcomes = ", ".join(f"{k}: {v}" for k, v in sorted(g["statuses"].items()))
    acc = f"**{_pct(g['execution_accuracy'])}** ({g['correct']}/{g['questions']})"
    em = f"{_pct(g['exact_match_rate'])} ({g['exact_matches']}/{g['questions']})"
    block = f"**{_pct(adv['block_rate'])}** ({adv['stopped']}/{adv['questions']})"
    total_lat = f"{lat['total_p50']:.1f} ms / {lat['total_p95']:.1f} ms"
    gen_lat = f"{lat['generation_p50']:.1f} ms / {lat['generation_p95']:.1f} ms"
    lines += [
        f"- provider: `{report.provider}`, model: {report.model}",
        f"- backend: `{report.backend}`",
        f"- run at: {report.started_at} (nl2sql {report.version})",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Gold questions | {g['questions']} |",
        f"| Execution accuracy | {acc} |",
        f"| Exact SQL match | {em} |",
        f"| Outcomes | {outcomes} |",
        f"| Gold SQL blocked by guardrails (false positives) | "
        f"{g['gold_sql_blocked_by_guardrails']} |",
        f"| Adversarial questions | {adv['questions']} |",
        f"| Adversarial block rate | {block} |",
        f"| Adversarial executed (leaks) | {len(adv['executed'])} |",
        f"| Latency p50 / p95 (end to end) | {total_lat} |",
        f"| Generation latency p50 / p95 | {gen_lat} |",
    ]
    est = " (estimated)" if tok["estimated"] else ""
    lines.append(
        f"| Tokens per question{est} | {tok['prompt_per_question']:.0f} in / "
        f"{tok['completion_per_question']:.0f} out |"
    )
    if tok["cost_usd_total"] is not None:
        lines.append(
            f"| Cost{est}, {tok['cost_model']} list price | "
            f"${tok['cost_usd_per_question']:.5f} per question, "
            f"${tok['cost_usd_total']:.4f} for the gold set |"
        )
    lines += [
        "",
        "## Accuracy by tag",
        "",
        "| Tag | Correct | Total | Accuracy |",
        "|---|---|---|---|",
    ]
    for tag, v in g["by_tag"].items():
        lines.append(f"| {tag} | {v['correct']} | {v['total']} | {_pct(v['accuracy'])} |")

    lines += [
        "",
        "## Adversarial set",
        "",
        "Blocked = a guardrail stopped the generated SQL. Refused = the provider "
        "produced no SQL. Executed = the query ran (a leak).",
        "",
        "| ID | Attack | Outcome | Caught by | Expected | Reason |",
        "|---|---|---|---|---|---|",
    ]
    for a in report.adversarial:
        mark = "" if not a.expected or a.blocked_by == a.expected else " (!)"
        lines.append(
            f"| {a.id} | {_cell(a.attack, 40)} | {a.outcome} | {a.blocked_by or ''}{mark} | "
            f"{a.expected or ''} | {_cell(a.reason, 70)} |"
        )
    if adv["blocked_by"]:
        lines += [
            "",
            "Caught by guardrail: " + ", ".join(f"{k} {v}" for k, v in adv["blocked_by"].items()),
        ]

    if report.gold_sql_blocked:
        lines += ["", "## Gold SQL blocked by guardrails", ""]
        for b in report.gold_sql_blocked:
            lines.append(f"- {b['id']}: {b['blocked_by']}: {b['reason']}")

    lines += [
        "",
        "## Gold questions",
        "",
        "| ID | Question | Tags | Result | Detail |",
        "|---|---|---|---|---|",
    ]
    for r in report.gold:
        detail = r.error or ("" if r.correct else f"{r.pred_rows} rows vs {r.gold_rows} expected")
        if r.status == "no_sql":
            detail = "no SQL produced"
        lines.append(
            f"| {r.id} | {_cell(r.question, 60)} | {', '.join(r.tags)} | {r.status} | "
            f"{_cell(detail, 60)} |"
        )
    lines += [
        "",
        "## How to read this",
        "",
        "- **Execution accuracy** runs the gold SQL and the generated SQL on the same "
        "database and compares the rows (order-insensitive unless the gold query has "
        "an ORDER BY; numbers within 0.01; column order may differ, column count may not).",
        "- **Exact match** compares normalised SQL text. It undercounts: different "
        "correct queries never match.",
        "- **Block rate** counts adversarial questions that never reached the database. "
        "The adversarial set was written alongside the guardrails, so 100% here is the "
        "expected result, not evidence of safety against attacks nobody has thought of yet. "
        "That is what least-privilege database permissions are for.",
        "- Latency here includes guardrails and execution against the local database.",
        "",
    ]
    return "\n".join(lines)


def write(report: EvalReport, out_dir: str | Path, stem: str = "eval") -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md, js = out / f"{stem}.md", out / f"{stem}.json"
    md.write_text(to_markdown(report), encoding="utf-8")
    js.write_text(json.dumps(jsonify(report.to_dict()), indent=2) + "\n", encoding="utf-8")
    return md, js
