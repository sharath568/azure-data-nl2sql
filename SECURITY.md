# Security

nl2sql is a reference project about running model-generated SQL safely. It
has not had an independent security review. Read the "Honest limits" section
of the README before you put it in front of real users.

## Reporting a problem

If you find a way to get SQL past the guardrails, please tell me. Use
GitHub's private vulnerability reporting on this repository ("Security" tab,
"Report a vulnerability") rather than a public issue. Include:

- the question or SQL,
- the guardrail you expected to catch it,
- whether the database permissions (layer g) still stopped it.

A bypass of the text guardrails that the read-only login still stops is a
bug, and it makes a good test case. A bypass that reaches data the
read-only principal should not see, or changes data, is a security issue.
Please report those privately.

I'll acknowledge reports within a week. This is a personal project, so
there is no bounty.

## Deployment advice

- Connect as a dedicated read-only principal. Use `sql/mssql/04_permissions.sql`
  as a starting point. Never use `sa`, `dbo`, `db_owner` or `db_datawriter`;
  nl2sql refuses by default and you should not override that
  (`policy.allow_privileged_connection`).
- Prefer Microsoft Entra ID (managed identity) over SQL passwords.
- Keep `semantic.yaml` under review like code. It decides which columns are
  denied.
- Log the generated SQL and the guardrail report for every question.
- Point it at a readable secondary or a reporting copy, not the primary that
  serves your application, if the database matters.
