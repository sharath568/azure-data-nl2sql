# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- First eval with a real model: Azure OpenAI gpt-5-mini on Azure SQL Database
  (free offer). Execution accuracy 45.2% with the original prompt, 81.0% after
  prompt rules 7 and 8; no harmful statement reached the database in either
  run. Reports in `docs/eval/azure-gpt5mini-*`.
- Reasoning models (gpt-5*, o1/o3/o4*) in the Azure OpenAI provider: they get
  `max_completion_tokens` and `reasoning_effort` (`AZURE_OPENAI_REASONING_EFFORT`,
  default `low`) instead of `temperature` and `max_tokens`, and API version
  2025-04-01-preview unless `AZURE_OPENAI_API_VERSION` is set. gpt-5 prices
  in `pricing.py`.
- `CHAT_MODEL` / `CHAT_MODEL_VERSION` in `infra/up.sh` (`chatModelName` in
  Bicep), for subscriptions without gpt-4.1-mini quota.
- `python -m nl2sql`.

### Changed

- Prompt rules 7 (select the columns asked for, keep stored values as they
  are) and 8 (count the primary key, not `*`). The prompt is ~100 tokens
  longer; the stub report is regenerated.
- An Azure OpenAI content-filter rejection (HTTP 400) is now a refusal (no
  SQL), not a provider error.
- Eval reports call executed adversarial questions "executed", not "leaks",
  and print their SQL: a real model may rewrite an attack into a harmless
  query.
- `infra/up.sh` saves the admin password to `.env.azure` before deploying.

### Fixed

- SQL Server refuses a bare `SELECT COUNT(*) FROM t` (error 230) when `t` has a
  column-level DENY, because a count that names no column is checked against
  every column. Gold question g01 and the stub's plain count now use
  `COUNT(<primary key>)`, and the integration tests document the engine
  behaviour.

## [0.1.0] - 2026-10-10

First release.

### Added

- Schema grounding: introspection for SQL Server / Azure SQL (`sys` views) and
  SQLite (`PRAGMA`), a compact schema card with denied columns left out, a YAML
  semantic layer (descriptions, synonyms, metrics, policy) and keyword plus
  foreign-key table retrieval. Low-cardinality sample values are opt-in.
- Providers: Azure OpenAI chat completions (gpt-4.1-mini by default, JSON schema
  output, retries honouring `Retry-After`, API key or Entra ID token) and an
  offline template "stub" provider, clearly labelled as not an AI.
- Guardrails, one module each, applied in order and reported: (a) parse with
  sqlglot T-SQL, (b) single statement, (c) SELECT only, (d) table/column
  allow-list including `SELECT *` expansion, (e) row limit via TOP/FETCH,
  (f) cost (static checks, `SET SHOWPLAN_XML` estimate on SQL Server),
  (g) least-privilege connection check with timeout and rollback,
  (h) result redaction.
- T-SQL scripts: sample schema and seed (generated), read-only logins (SQL
  Server) and contained users (Azure SQL), a `nl2sql_readers` role with column
  DENY and explicit write DENY, and a row-level security example.
- Evaluation: 42 gold questions and 21 adversarial questions on a bundled retail
  sample; execution accuracy, exact match, per-tag accuracy, guardrail block
  rate, latency, token usage and cost estimate; Markdown and JSON reports.
  Committed offline report in `docs/eval/` (stub provider, labelled).
- CLI: `init-sample`, `schema`, `check`, `ask`, `eval`.
- Bicep for the Azure SQL Database free offer (serverless, AutoPause on
  exhaustion) and Azure OpenAI gpt-4.1-mini, with `up.sh` / `down.sh` and a
  budget alert.
- GitHub Actions: ruff and offline tests on Python 3.10 to 3.12; integration
  tests against SQL Server 2025 showing the read-only login is enforced by the
  engine; Bicep build.
