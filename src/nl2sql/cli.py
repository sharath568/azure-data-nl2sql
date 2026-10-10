"""Command line interface.

    nl2sql init-sample                 build the offline sample database (+ semantic.yaml)
    nl2sql schema                      print the schema card the model would see
    nl2sql check "SELECT ..."          run the guardrails on raw SQL
    nl2sql ask "question"              question -> SQL -> guardrails -> rows
    nl2sql eval                        gold + adversarial evaluation, Markdown + JSON

Exit codes: 0 ok, 1 error, 2 blocked by a guardrail, 3 no SQL produced.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import click

from nl2sql import __version__, config, guardrails
from nl2sql.semantic import SemanticLayer, bundled_path, load, load_sample

EXIT_OK, EXIT_ERROR, EXIT_BLOCKED, EXIT_NO_SQL = 0, 1, 2, 3


# ---------------------------------------------------------------- helpers


def _layer(semantic: str | None) -> SemanticLayer:
    path = semantic or config.default_semantic()
    if path is None:
        return SemanticLayer()
    if path == "sample":
        return load_sample()
    try:
        return load(path)
    except FileNotFoundError as e:
        raise click.ClickException(f"semantic layer not found: {path}") from e
    except (ValueError, TypeError) as e:
        raise click.ClickException(f"bad semantic layer {path}: {e}") from e


def _target(db: str | None, conn: str | None) -> str:
    if conn:
        return f"mssql:{conn}"
    return db or config.default_db()


def _backend(target: str):
    from nl2sql.db import open_backend

    try:
        return open_backend(target)
    except FileNotFoundError as e:
        raise click.ClickException(str(e)) from e
    except (ValueError, RuntimeError) as e:
        raise click.ClickException(f"cannot open {target.split(';')[0]}: {e}") from e


def _provider(name: str):
    from nl2sql.providers import ProviderError, get_provider

    try:
        return get_provider(name)
    except ProviderError as e:
        raise click.ClickException(str(e)) from e


def format_table(columns: list[str], rows: list[tuple], max_width: int = 40) -> str:
    def cell(v) -> str:
        s = "NULL" if v is None else str(v)
        s = " ".join(s.split())
        return s if len(s) <= max_width else s[: max_width - 3] + "..."

    body = [[cell(v) for v in r] for r in rows]
    widths = [len(c) for c in columns]
    for r in body:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], len(v))
    line = "  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True))
    sep = "  ".join("-" * w for w in widths)
    out = [line, sep]
    out += ["  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)) for r in body]
    return "\n".join(out)


_STATUS_MARK = {
    "pass": "ok",
    "rewrite": "rewrite",
    "warn": "warn",
    "skip": "skip",
    "block": "BLOCK",
}


def format_checks(report: guardrails.GuardReport) -> str:
    lines = []
    for c in report.checks:
        mark = _STATUS_MARK.get(c.status.value, c.status.value)
        lines.append(f"  [{mark:>7}] {c.name:<17} {c.message}")
        for d in c.details[1:] if c.blocked else c.details:
            lines.append(f"  {'':>9} {'':<17} - {d}")
    for name in report.not_run:
        lines.append(f"  [{'not run':>7}] {name}")
    return "\n".join(lines)


# ---------------------------------------------------------------- commands


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="nl2sql")
def main() -> None:
    """Guarded natural language to SQL for Azure SQL (and an offline SQLite sample)."""
    config.load_dotenv()


@main.command("init-sample")
@click.option(
    "--path",
    "path",
    default=config.DEFAULT_SAMPLE_PATH,
    show_default=True,
    help="Where to write the SQLite sample database.",
)
@click.option(
    "--db",
    "db",
    default=None,
    help="Load the sample into SQL Server / Azure SQL instead (mssql:<conn string>). "
    "Use an admin login; the tables are dropped and recreated.",
)
@click.option("--force", is_flag=True, help="Overwrite an existing SQLite file.")
@click.option("--no-semantic", is_flag=True, help="Do not write semantic.yaml next to it.")
def init_sample(path: str, db: str | None, force: bool, no_semantic: bool) -> None:
    """Create the bundled retail sample database."""
    from nl2sql import sample

    if db:
        from nl2sql.db import parse_target
        from nl2sql.db.mssql import MSSQLBackend

        kind, conn = parse_target(db)
        if kind != "mssql":
            raise click.ClickException("--db must be an mssql: target; use --path for SQLite")
        backend = MSSQLBackend(conn)
        try:
            n = backend.run_script(sample.tsql_schema()) + backend.run_script(sample.tsql_seed())
        finally:
            backend.close()
        click.echo(f"Loaded the sample schema and data into SQL Server ({n} batches).")
        click.echo(
            "Next: create the read-only login, see sql/mssql/03_*.sql and 04_permissions.sql."
        )
        return
    try:
        data = sample.build_sqlite(path, force=force)
    except FileExistsError as e:
        raise click.ClickException(f"{e}. Use --force to overwrite.") from e
    counts = ", ".join(f"{t} {data.count(t)}" for t in sample.LOAD_ORDER)
    click.echo(f"Wrote {path} ({counts}).")
    if not no_semantic:
        target = Path(config.DEFAULT_SEMANTIC_FILE)
        if target.exists():
            click.echo(f"Kept existing {target}.")
        else:
            shutil.copyfile(bundled_path("semantic.yaml"), target)
            click.echo(f"Wrote {target} (the semantic layer: descriptions + policy). Review it.")
    click.echo('Try: nl2sql ask "What are the top 5 products by revenue?" --explain')


@main.command()
@click.option("--db", default=None, help="sqlite:<path> or mssql:<conn string>. Env: NL2SQL_DB.")
@click.option("--conn", default=None, help="SQL Server / Azure SQL connection string.")
@click.option(
    "--semantic", default=None, help="Semantic layer YAML ('sample' for the bundled one)."
)
@click.option(
    "-q", "--question", default=None, help="Only show tables retrieved for this question."
)
@click.option("--sample-values", is_flag=True, help="Include low-cardinality sample values.")
@click.option(
    "--all", "show_all", is_flag=True, help="Show denied columns too (what the model does NOT see)."
)
def schema(db, conn, semantic, question, sample_values, show_all) -> None:
    """Print the schema card the model would see."""
    from nl2sql.schema import render_card, retrieve

    layer = _layer(semantic)
    with _backend(_target(db, conn)) as backend:
        full = backend.introspect(
            sample_values=sample_values,
            schemas=layer.policy.allowed_schemas,
            sample_filter=lambda t, c: not layer.is_column_denied(t, c),
        )
    visible = full if show_all else full.visible(layer)
    if question:
        r = retrieve(question, visible, layer)
        click.echo(f"-- retrieved: {', '.join(r.tables) or '(none)'} ({r.reason})")
        click.echo(render_card(visible, layer, r.tables, r.metrics))
    else:
        click.echo(render_card(visible, layer))
    hidden = {t: sorted(c) for t, c in full.denied_columns(layer).items()}
    if hidden and not show_all:
        listed = "; ".join(f"{t}: {', '.join(c)}" for t, c in sorted(hidden.items()))
        click.echo(f"-- hidden by policy: {listed}")


@main.command()
@click.argument("sql")
@click.option("--db", default=None, help="Use this database's schema (and check its privileges).")
@click.option("--conn", default=None, help="SQL Server / Azure SQL connection string.")
@click.option(
    "--semantic", default=None, help="Semantic layer YAML ('sample' for the bundled one)."
)
@click.option("--offline", is_flag=True, help="Do not connect to any database.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def check(sql, db, conn, semantic, offline, as_json) -> None:
    """Run the guardrails on raw SQL (no model involved). Exit 2 if blocked."""
    layer = _layer(semantic)
    backend = None
    schema_obj = None
    target = _target(db, conn)
    have_db = bool(db or conn) or config.default_db() != config.DEFAULT_DB
    have_db = have_db or Path(config.DEFAULT_SAMPLE_PATH).exists()
    if have_db and not offline:
        backend = _backend(target)
        schema_obj = backend.introspect(schemas=layer.policy.allowed_schemas)
    try:
        report = guardrails.check(sql, layer, schema=schema_obj, backend=backend)
    finally:
        if backend:
            backend.close()
    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2))
    else:
        click.echo(format_checks(report))
        click.echo("")
        if report.allowed:
            click.echo("ALLOWED. SQL that would run:")
            click.echo(f"  {report.sql}")
        else:
            click.echo(f"BLOCKED by {report.blocked_by}: {report.reason}")
    sys.exit(EXIT_OK if report.allowed else EXIT_BLOCKED)


@main.command()
@click.argument("question")
@click.option("--db", default=None, help="sqlite:<path> or mssql:<conn string>. Env: NL2SQL_DB.")
@click.option(
    "--conn", default=None, help="SQL Server / Azure SQL connection string (implies mssql)."
)
@click.option(
    "--provider",
    "provider_name",
    default=None,
    help="stub (offline, not an AI) or azure-openai. Env: NL2SQL_PROVIDER.",
)
@click.option(
    "--semantic", default=None, help="Semantic layer YAML ('sample' for the bundled one)."
)
@click.option(
    "--explain", is_flag=True, help="Show retrieval, the guardrail report and the explanation."
)
@click.option("--dry-run", is_flag=True, help="Generate and check, but do not execute.")
@click.option("--show-prompt", is_flag=True, help="Print the schema card sent to the model.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def ask(
    question, db, conn, provider_name, semantic, explain, dry_run, show_prompt, as_json
) -> None:
    """Ask a question in plain English."""
    from nl2sql.engine import Engine

    layer = _layer(semantic)
    provider = _provider(provider_name or config.default_provider())
    with _backend(_target(db, conn)) as backend:
        engine = Engine(backend, provider, layer)
        res = engine.ask(question, execute=not dry_run)
    if as_json:
        click.echo(json.dumps(res.to_dict(include_card=show_prompt), indent=2, default=str))
    else:
        _print_ask(res, explain=explain, show_prompt=show_prompt)
    code = {"ok": EXIT_OK, "blocked": EXIT_BLOCKED, "no_sql": EXIT_NO_SQL}.get(
        res.status, EXIT_ERROR
    )
    sys.exit(code)


def _print_ask(res, *, explain: bool, show_prompt: bool) -> None:
    gen = res.generation
    if provider_is_stub := (gen and gen.provider == "stub"):
        click.echo(
            "(stub provider: regex templates, not an AI. Use --provider azure-openai for a model.)"
        )
    if explain and res.retrieval:
        click.echo(
            f"Tables retrieved: {', '.join(res.retrieval.tables) or '(none)'}"
            f"  [{res.retrieval.reason}]"
        )
    if show_prompt:
        click.echo("\nSchema card sent to the model:\n" + res.card)
    if gen is None:
        click.echo(f"Error: {res.error}")
        return
    if not gen.answered:
        click.echo(f"No SQL produced: {gen.explanation}")
        return
    click.echo(f"\nGenerated SQL:\n  {gen.sql}")
    if explain and gen.explanation and not provider_is_stub:
        click.echo(f"\nExplanation: {gen.explanation}")
    if res.guard and (explain or not res.guard.allowed):
        click.echo("\nGuardrails:")
        click.echo(format_checks(res.guard))
    if res.guard and not res.guard.allowed:
        click.echo(f"\nBLOCKED by {res.guard.blocked_by}: {res.guard.reason}")
        return
    if res.guard and res.guard.sql != gen.sql:
        click.echo(f"\nSQL after guardrails:\n  {res.guard.sql}")
    if res.status in ("error", "timeout"):
        click.echo(f"\n{res.status.upper()}: {res.error}")
        return
    if res.result is None:
        click.echo("\n(dry run: not executed)")
        return
    click.echo("")
    click.echo(format_table(res.result.columns, res.result.rows))
    more = " (truncated by the row limit)" if res.result.truncated else ""
    click.echo(f"\n{len(res.result.rows)} row(s) in {res.result.elapsed_ms:.1f} ms{more}")
    if res.redaction and res.redaction.status.value != "pass":
        click.echo(f"Redaction: {res.redaction.message}")
    if explain and gen.usage:
        est = " (estimated)" if gen.usage.estimated else ""
        click.echo(f"Tokens{est}: {gen.usage.prompt_tokens} in / {gen.usage.completion_tokens} out")


@main.command("eval")
@click.option("--db", default=None, help="sqlite:<path> or mssql:<conn string>. Env: NL2SQL_DB.")
@click.option("--conn", default=None, help="SQL Server / Azure SQL connection string.")
@click.option("--provider", "provider_name", default=None, help="stub or azure-openai.")
@click.option(
    "--semantic", default=None, help="Semantic layer YAML (default: the bundled sample one)."
)
@click.option("--gold", "gold_path", default=None, help="Gold set YAML (default: bundled).")
@click.option(
    "--adversarial", "adv_path", default=None, help="Adversarial set YAML (default: bundled)."
)
@click.option("--out-dir", default="eval-results", show_default=True)
@click.option(
    "--name", "stem", default=None, help="File name stem (default: <provider>-<backend>)."
)
@click.option(
    "--limit", type=int, default=None, help="Only the first N gold questions (for a cheap trial)."
)
@click.option("-q", "--quiet", is_flag=True)
def eval_cmd(
    db, conn, provider_name, semantic, gold_path, adv_path, out_dir, stem, limit, quiet
) -> None:
    """Score a provider on the gold and adversarial sets."""
    from nl2sql.engine import Engine
    from nl2sql.evaluation import load_adversarial, load_gold, run_eval, write

    layer = _layer(semantic or "sample")
    provider = _provider(provider_name or config.default_provider())
    gold = load_gold(gold_path)
    adversarial = load_adversarial(adv_path)
    if limit:
        gold = gold[:limit]
    progress = None if quiet else (lambda m: click.echo(f"  {m}", err=True))
    with _backend(_target(db, conn)) as backend:
        engine = Engine(backend, provider, layer)
        report = run_eval(engine, gold, adversarial, progress=progress)
    md, js = write(report, out_dir, stem or f"{provider.name}-{backend.name}")
    s = report.summary()
    g, adv = s["gold"], s["adversarial"]
    if report.is_stub:
        click.echo(
            "NOTE: stub provider (templates, not an AI). Accuracy measures template coverage."
        )
    click.echo(
        f"execution accuracy {100 * g['execution_accuracy']:.1f}% "
        f"({g['correct']}/{g['questions']}), "
        f"exact match {100 * g['exact_match_rate']:.1f}%, "
        f"adversarial block rate {100 * adv['block_rate']:.1f}% "
        f"({adv['stopped']}/{adv['questions']})"
    )
    click.echo(f"wrote {md} and {js}")
    if adv["executed"]:
        click.echo(f"WARNING: adversarial questions executed: {', '.join(adv['executed'])}")
        sys.exit(EXIT_BLOCKED)


if __name__ == "__main__":  # pragma: no cover
    main()
