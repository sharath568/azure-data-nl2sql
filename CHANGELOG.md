# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

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
