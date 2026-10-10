"""CLI and offline end-to-end tests (SQLite + stub; no network, no cost)."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nl2sql import config, guardrails
from nl2sql.cli import format_table, main
from nl2sql.engine import Engine
from nl2sql.providers.base import Generation, Provider


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for var in ("NL2SQL_DB", "NL2SQL_SEMANTIC", "NL2SQL_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    runner = CliRunner()
    result = runner.invoke(main, ["init-sample"])
    assert result.exit_code == 0, result.output
    return tmp_path, runner


def test_init_sample_writes_db_and_semantic_layer(workdir):
    tmp, runner = workdir
    assert (tmp / "nl2sql_sample.db").exists()
    assert (tmp / "semantic.yaml").exists()
    again = runner.invoke(main, ["init-sample"])
    assert again.exit_code != 0 and "--force" in again.output
    forced = runner.invoke(main, ["init-sample", "--force"])
    assert forced.exit_code == 0 and "Kept existing semantic.yaml" in forced.output


def test_ask_ok(workdir):
    _, runner = workdir
    r = runner.invoke(main, ["ask", "How many customers do we have?", "--explain"])
    assert r.exit_code == 0, r.output
    assert "stub provider" in r.output
    assert "customer_count" in r.output and "60" in r.output
    assert "least_privilege" in r.output


def test_ask_blocked_exit_code(workdir):
    _, runner = workdir
    r = runner.invoke(main, ["ask", "Show employee salaries."])
    assert r.exit_code == 2
    assert "BLOCKED by allow_list" in r.output


def test_ask_no_sql_exit_code(workdir):
    _, runner = workdir
    r = runner.invoke(main, ["ask", "What is the meaning of life?"])
    assert r.exit_code == 3


def test_ask_json(workdir):
    _, runner = workdir
    r = runner.invoke(main, ["ask", "Delete all orders.", "--json"])
    assert r.exit_code == 2
    data = json.loads(r.output)
    assert data["status"] == "blocked"
    assert data["guardrails"]["blocked_by"] == "select_only"
    assert data["guardrails"]["sql"] is None


def test_ask_dry_run(workdir):
    _, runner = workdir
    r = runner.invoke(main, ["ask", "How many orders were cancelled?", "--dry-run"])
    assert r.exit_code == 0
    assert "dry run" in r.output


def test_check_command(workdir):
    _, runner = workdir
    ok = runner.invoke(main, ["check", "SELECT name FROM products"])
    assert ok.exit_code == 0 and "SELECT TOP 100 name FROM products" in ok.output
    bad = runner.invoke(main, ["check", "SELECT 1; DROP TABLE orders"])
    assert bad.exit_code == 2 and "single_statement" in bad.output
    js = runner.invoke(main, ["check", "SELECT ssn FROM employees", "--json"])
    assert json.loads(js.output)["blocked_by"] == "allow_list"


def test_check_offline_needs_no_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NL2SQL_DB", raising=False)
    r = CliRunner().invoke(main, ["check", "DELETE FROM orders", "--offline"])
    assert r.exit_code == 2


def test_schema_command(workdir):
    _, runner = workdir
    r = runner.invoke(main, ["schema"])
    assert r.exit_code == 0
    assert "TABLE dbo.orders" in r.output
    assert "salary" not in r.output.split("-- hidden by policy")[0]
    assert "hidden by policy" in r.output
    r = runner.invoke(main, ["schema", "-q", "revenue by category", "--sample-values"])
    assert "retrieved:" in r.output and "values:" in r.output


def test_eval_command(workdir):
    tmp, runner = workdir
    r = runner.invoke(main, ["eval", "-q", "--out-dir", "out"])
    assert r.exit_code == 0, r.output
    assert "stub provider" in r.output
    assert (tmp / "out" / "stub-sqlite.md").exists()
    assert (tmp / "out" / "stub-sqlite.json").exists()


def test_missing_database_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NL2SQL_DB", raising=False)
    r = CliRunner().invoke(main, ["ask", "How many customers?"])
    assert r.exit_code == 1
    assert "init-sample" in r.output


def test_dotenv_loader(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# c\nNL2SQL_X1=abc\nexport NL2SQL_X2='q v'\nNL2SQL_X3=keep\nbad line\n")
    monkeypatch.setenv("NL2SQL_X3", "shell")
    monkeypatch.delenv("NL2SQL_X1", raising=False)
    monkeypatch.delenv("NL2SQL_X2", raising=False)
    loaded = config.load_dotenv(env)
    import os

    assert os.environ["NL2SQL_X1"] == "abc"
    assert os.environ["NL2SQL_X2"] == "q v"
    assert os.environ["NL2SQL_X3"] == "shell"  # never overrides the shell
    assert "NL2SQL_X3" not in loaded


def test_format_table():
    out = format_table(["a", "bb"], [(1, None), ("x" * 50, "y")], max_width=10)
    lines = out.splitlines()
    assert lines[0].startswith("a")
    assert "NULL" in out and "..." in out


# ---------------------------------------------------------------- end to end


class ScriptedProvider(Provider):
    """Returns whatever SQL it is given: a stand-in for a model under attack."""

    name = "scripted"

    def __init__(self, sql):
        self.sql = sql

    def generate(self, ctx):
        return Generation(self.sql, "scripted", [], self.name)


@pytest.mark.parametrize(
    ("sql", "status", "blocked_by"),
    [
        ("SELECT name FROM products WHERE category = 'Tea'", "ok", None),
        ("DELETE FROM orders", "blocked", "select_only"),
        ("SELECT first_name, salary FROM employees", "blocked", "allow_list"),
        ("SELECT * FROM employees", "blocked", "allow_list"),
        ("SELECT 1; DROP TABLE orders", "blocked", "single_statement"),
        ("SELECT o.order_id FROM orders o CROSS JOIN order_items i", "blocked", "cost"),
        ("SELECT nope FROM products", "error", None),
    ],
)
def test_end_to_end(backend, layer, sql, status, blocked_by):
    res = Engine(backend, ScriptedProvider(sql), layer).ask("whatever")
    assert res.status == status, res.to_dict()
    if blocked_by:
        assert res.guard.blocked_by == blocked_by
    if status == "ok":
        assert res.result.rows
        assert res.sql.startswith("SELECT TOP 100")
        assert set(res.timings_ms) >= {
            "retrieval",
            "generation",
            "guardrails",
            "execution",
            "total",
        }


def test_end_to_end_redaction_as_last_line_of_defence(backend, layer):
    """If a sensitive-looking column reaches the result (say, a computed alias the
    allow-list could not know about), redaction still masks it by name."""
    from nl2sql.semantic import from_dict

    loose = from_dict({"policy": {"extra_denied_column_patterns": ["*secret*"]}})
    res = Engine(backend, ScriptedProvider("SELECT name AS secret_name FROM products"), loose).ask(
        "x"
    )
    assert res.status == "ok"
    assert {r[0] for r in res.result.rows} == {guardrails.REDACTED}
    assert res.redaction.status.value == "rewrite"


def test_end_to_end_row_cap(backend, layer):
    res = Engine(backend, ScriptedProvider("SELECT order_id FROM orders"), layer).ask("x")
    assert len(res.result.rows) == 100


def test_provider_error_is_reported(backend, layer):
    from nl2sql.providers.base import ProviderError

    class Broken(Provider):
        name = "broken"

        def generate(self, ctx):
            raise ProviderError("endpoint down")

    res = Engine(backend, Broken(), layer).ask("x")
    assert res.status == "error" and "endpoint down" in res.error


def test_guard_crash_means_block():
    class Exploding(guardrails.Guardrail):
        name = "exploding"

        def run(self, state):
            raise RuntimeError("bug")

    report = guardrails.check("SELECT 1", guards=[guardrails.ParseGuard(), Exploding()])
    assert not report.allowed
    assert report.blocked_by == "exploding"
