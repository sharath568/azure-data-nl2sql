# Contributing

Thanks for taking a look. This is a small reference project, so the bar is
"clear, tested, honest, cheap to run".

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

That's enough for everything except the SQL Server integration tests.

## Before you open a PR

```bash
ruff check . && ruff format --check .
pytest -m "not integration"
python scripts/gen_tsql.py --check
```

CI runs the same on Python 3.10 to 3.12, plus the integration tests against
`mcr.microsoft.com/mssql/server:2025-latest` and a `bicep build` of
`infra/main.bicep`.

To run the integration tests locally you need SQL Server (a container is
easiest) and the ODBC driver or `mssql-python`. Then follow the steps in
`.github/workflows/ci.yml`: create the database, `nl2sql init-sample --db
mssql:...`, run `sql/mssql/03_login_sqlserver.sql`, `04_permissions.sql` and
`05_row_level_security.sql` with `scripts/run_tsql.py`, set
`NL2SQL_TEST_CONN` (and optionally `NL2SQL_TEST_WEST_CONN` and
`NL2SQL_TEST_ADMIN_CONN`), and run `pytest -m integration`.

## Guidelines

- **Guardrails fail closed.** If a guard can't decide, it blocks. A guard that
  raises is treated as a block by the pipeline; keep it that way.
- **Every guardrail change comes with tests**, both an allow case and a deny
  case. Tricky inputs (comments, casing, nesting, CTEs, unions) are the most
  useful ones.
- **New attacks go in two places:** a unit test for the guardrail that should
  catch it, and an item in `src/nl2sql/data/adversarial.yaml`. If the database
  permissions also stop it, add an integration test that calls `execute_raw`
  and proves it.
- **Keep the default path free.** Anything that calls a paid API must be opt-in
  and fail with a clear message when it is not configured.
- **Don't tune the stub to the gold set.** The stub's templates are generic
  patterns. Adding a template that only exists to answer one gold question
  makes the committed numbers meaningless. If you want a better score, use a
  real model and commit that report with its provenance.
- **The sample data is generated.** Edit `src/nl2sql/sample.py`, then run
  `python scripts/gen_tsql.py` to regenerate `sql/mssql/01_schema.sql` and
  `02_seed.sql`.
- **Eval reports:** only real runs, labelled with provider, model, deployment
  and date. Never present the stub's accuracy as model accuracy.
- **Prices** in `src/nl2sql/pricing.py` are reference constants. If you update
  them, say where they came from and when.
