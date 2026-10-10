# Azure infrastructure (lowest-cost setup)

You don't need any of this to try nl2sql. The offline quickstart (SQLite plus
the stub provider) costs nothing. This folder is for running the same thing
against a real Azure SQL Database and a real model.

`main.bicep` deploys:

| Resource | Setting | Why |
|---|---|---|
| Azure SQL logical server | TLS 1.2 minimum, one firewall rule for `clientIp` | Not open to the world |
| Azure SQL Database | Serverless General Purpose `GP_S_Gen5_2`, **`useFreeLimit: true`**, **`freeLimitExhaustionBehavior: AutoPause`**, auto-pause after 60 min idle, 32 GB max, locally redundant backups | The free offer. When the monthly allowance is used up, the database pauses until next month instead of billing |
| Azure OpenAI | One `gpt-4.1-mini` deployment, `GlobalStandard` (pay per token), 10K TPM | No fixed monthly cost; you pay only for tokens |
| Budget | `monthlyBudget` (default 10) on the resource group, e-mail at 50 / 80 / 100 % of actual spend | So nothing turns into a surprise |

## Deploy

You need the Azure CLI (`az login` done), a subscription, and nl2sql
installed with a SQL Server driver (setup loads the sample and creates the
read-only users from your machine):

```bash
pip install -e ".[mssql]"
BUDGET_EMAIL=you@example.com ./infra/up.sh
cp .env.azure .env
nl2sql ask "What are the top 5 products by revenue?" --semantic sample --explain
```

`up.sh`:

1. creates the resource group (`nl2sql-rg` in `eastus2` unless you set `RG` / `LOCATION`),
2. deploys `main.bicep` with a generated admin password and your public IP,
3. loads the sample schema and data (`nl2sql init-sample --db mssql:...`),
4. runs `sql/mssql/03_users_azure_sql.sql`, `04_permissions.sql` and
   `05_row_level_security.sql` to create `nl2sql_reader` and
   `nl2sql_reader_west` as contained database users,
5. writes `.env.azure` (mode 600, git-ignored). `NL2SQL_DB` points at the
   **read-only** user, not the admin.

Delete everything when you're done:

```bash
./infra/down.sh      # az group delete --name nl2sql-rg --yes --no-wait
```

## What it costs

**Azure SQL Database free offer.** At the time of writing, each subscription
can have up to 10 free serverless databases. Each gets 100,000 vCore-seconds of
compute, 32 GB of data and 32 GB of backup storage per month, for the life of
the subscription. With `AutoPause` on exhaustion, going over the allowance
pauses the database rather than billing. With auto-pause after 60 idle
minutes, a few hours of experimenting a week uses a small part of the
allowance. Check the
[current offer terms](https://learn.microsoft.com/azure/azure-sql/database/free-offer)
before you rely on this.

**Azure OpenAI, per question.** From the committed offline eval
([docs/eval/stub-sqlite.md](../docs/eval/stub-sqlite.md)), the prompt nl2sql
would send for one sample question is about **850 tokens** (system prompt plus
the retrieved part of the schema card). A JSON answer is roughly 70 to 150
tokens. At gpt-4.1-mini list prices ($0.40 per 1M input tokens, $1.60 per 1M
output tokens, see `src/nl2sql/pricing.py`):

| | Tokens | Cost |
|---|---|---|
| One question | ~850 in, ~150 out | ~$0.0006 |
| Full eval (42 gold + 21 adversarial) | ~55K in, ~10K out | ~$0.04 |
| 1,000 questions | ~0.85M in, ~0.15M out | ~$0.58 |

These are estimates (characters / 4). `nl2sql eval --provider azure-openai`
reports the real token counts from the API next to them. The schema card is
most of the prompt, so retrieval (sending only relevant tables) matters more
for cost than the model choice.

**The budget is an alert, not a cap.** Azure budgets send e-mails; they
don't stop resources. The free-offer AutoPause does stop the database, but
nothing stops Azure OpenAI except deleting it.

## Things to double-check before you deploy

These change over time and I couldn't verify all of them from here:

- **Free offer properties.** `useFreeLimit` and `freeLimitExhaustionBehavior`
  compile against the `Microsoft.Sql/servers/databases@2023-08-01` types in
  Bicep 0.48 with no warnings. The template has not been deployed from this
  repo's CI. If the deployment rejects them (for example because the
  subscription already has its free databases), set `USE_FREE_LIMIT=false`.
  That gives you a normal serverless database billed per vCore-second; it
  still auto-pauses when idle.
- **Model availability.** `gpt-4.1-mini` version `2025-04-14` with
  `GlobalStandard` is widely available but not everywhere. If the deployment
  fails, set `OPENAI_LOCATION` to a region that has it. Use `CHAT_SKU=Standard`
  if you need processing to stay in-region and the region offers it.
- **First connection after a pause.** A paused serverless database resumes on
  the first connection, which can take about a minute and may fail once with
  error 40613. Retry. `up.sh` already does.
- **Prices.** Every price here is a reference value. Use the
  [Azure pricing calculator](https://azure.microsoft.com/pricing/calculator/).

## Security notes

- nl2sql connects as `nl2sql_reader`, which can only `SELECT` from `dbo`, is
  denied the sensitive columns, and is explicitly denied writes. The admin
  connection string in `.env.azure` is only for re-running setup scripts.
- For anything beyond a demo, use Microsoft Entra ID instead of passwords:
  set `entraAdminLogin` / `entraAdminObjectId`, give your app a managed
  identity, `CREATE USER [app] FROM EXTERNAL PROVIDER`, add it to
  `nl2sql_readers`, and connect with `Authentication=ActiveDirectoryDefault`.
  Then use private networking instead of a public firewall rule.
- Rotate the Azure OpenAI key with `az cognitiveservices account keys regenerate`
  when you're done, or just run `down.sh`.
