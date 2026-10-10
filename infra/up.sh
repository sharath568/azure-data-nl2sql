#!/usr/bin/env bash
# Deploy Azure SQL Database (free offer) + Azure OpenAI gpt-4.1-mini, load the
# sample, create the read-only users and write .env.azure.
#
# Usage:
#   pip install -e ".[mssql]"            # the SQL Server driver, needed for setup
#   BUDGET_EMAIL=you@example.com ./infra/up.sh
#
# Optional environment variables:
#   RG               resource group name            (default: nl2sql-rg)
#   LOCATION         region for Azure SQL           (default: eastus2)
#   OPENAI_LOCATION  region for Azure OpenAI        (default: same as LOCATION)
#   CLIENT_IP        IP allowed through firewall    (default: auto-detect)
#   MONTHLY_BUDGET   budget amount                  (default: 10)
#   DEPLOY_OPENAI    true/false                     (default: true)
#   CHAT_SKU         GlobalStandard or Standard     (default: GlobalStandard)
#   USE_FREE_LIMIT   true/false                     (default: true)
#
# The free offer database pauses when its monthly allowance runs out, so it
# cannot run up a bill. Azure OpenAI is pay per token. Delete everything with
# ./infra/down.sh when you are done.
set -euo pipefail

RG="${RG:-nl2sql-rg}"
LOCATION="${LOCATION:-eastus2}"
OPENAI_LOCATION="${OPENAI_LOCATION:-$LOCATION}"
MONTHLY_BUDGET="${MONTHLY_BUDGET:-10}"
DEPLOY_OPENAI="${DEPLOY_OPENAI:-true}"
CHAT_SKU="${CHAT_SKU:-GlobalStandard}"
USE_FREE_LIMIT="${USE_FREE_LIMIT:-true}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_ENV="$ROOT/.env.azure"

command -v az >/dev/null || { echo "Azure CLI (az) is required: https://aka.ms/azcli" >&2; exit 1; }
command -v nl2sql >/dev/null || { echo "install nl2sql first: pip install -e \".[mssql]\"" >&2; exit 1; }
: "${BUDGET_EMAIL:?set BUDGET_EMAIL to the address that should get budget alerts}"

if [[ -z "${CLIENT_IP:-}" ]]; then
  CLIENT_IP="$(curl -fsS https://api.ipify.org || true)"
  [[ -n "$CLIENT_IP" ]] || { echo "could not detect your IP; set CLIENT_IP" >&2; exit 1; }
fi

# Letters and digits plus a fixed suffix: no quotes or semicolons, which would
# break T-SQL literals and connection strings.
gen_password() { echo "$(openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 20)Aa1-"; }
ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(gen_password)}"
READER_PASSWORD="$(gen_password)"
WEST_PASSWORD="$(gen_password)"

echo "Resource group : $RG ($LOCATION)"
echo "Azure SQL      : serverless GP_S_Gen5_2, free offer = $USE_FREE_LIMIT (AutoPause when exhausted)"
echo "Client IP      : $CLIENT_IP"
echo "Azure OpenAI   : $DEPLOY_OPENAI (gpt-4.1-mini, $CHAT_SKU, $OPENAI_LOCATION)"
echo "Budget         : $MONTHLY_BUDGET per month, alerts to $BUDGET_EMAIL"

az group create --name "$RG" --location "$LOCATION" --tags project=nl2sql --output none

OUTPUTS="$(az deployment group create \
  --resource-group "$RG" \
  --name nl2sql \
  --template-file "$ROOT/infra/main.bicep" \
  --parameters \
      clientIp="$CLIENT_IP" \
      adminPassword="$ADMIN_PASSWORD" \
      budgetEmail="$BUDGET_EMAIL" \
      monthlyBudget="$MONTHLY_BUDGET" \
      openAiLocation="$OPENAI_LOCATION" \
      deployOpenAi="$DEPLOY_OPENAI" \
      chatDeploymentSku="$CHAT_SKU" \
      useFreeLimit="$USE_FREE_LIMIT" \
  --query properties.outputs --output json)"

out() { echo "$OUTPUTS" | python3 -c "import json,sys; print(json.load(sys.stdin)['$1']['value'])"; }

FQDN="$(out serverFqdn)"
DB="$(out databaseName)"
base="Server=tcp:${FQDN},1433;Database=${DB};Encrypt=yes;TrustServerCertificate=no"
ADMIN_CONN="${base};UID=nl2sqladmin;PWD=${ADMIN_PASSWORD}"
READER_CONN="${base};UID=nl2sql_reader;PWD=${READER_PASSWORD}"
WEST_CONN="${base};UID=nl2sql_reader_west;PWD=${WEST_PASSWORD}"

echo "Loading the sample schema and data (a paused database takes about a minute to resume)..."
for attempt in 1 2 3; do
  if nl2sql init-sample --db "mssql:${ADMIN_CONN}"; then break; fi
  [[ $attempt == 3 ]] && { echo "could not load the sample" >&2; exit 1; }
  sleep 30
done

echo "Creating the read-only users, permissions and row-level security..."
python3 "$ROOT/scripts/run_tsql.py" "$ADMIN_CONN" \
  "$ROOT/sql/mssql/03_users_azure_sql.sql" \
  "$ROOT/sql/mssql/04_permissions.sql" \
  "$ROOT/sql/mssql/05_row_level_security.sql" \
  -v READER_PASSWORD="$READER_PASSWORD" -v WEST_PASSWORD="$WEST_PASSWORD"

{
  echo "# Written by infra/up.sh. Contains secrets; do not commit."
  echo "# nl2sql connects as the read-only user:"
  echo "NL2SQL_DB=mssql:${READER_CONN}"
  echo "# Only for re-running setup scripts. nl2sql never reads these:"
  echo "NL2SQL_ADMIN_CONN=${ADMIN_CONN}"
  echo "NL2SQL_WEST_CONN=${WEST_CONN}"
} > "$OUT_ENV"

if [[ "$DEPLOY_OPENAI" == "true" ]]; then
  AOAI_NAME="$(out openAiName)"
  AOAI_ENDPOINT="$(out openAiEndpoint)"
  AOAI_KEY="$(az cognitiveservices account keys list -g "$RG" -n "$AOAI_NAME" --query key1 -o tsv)"
  {
    echo "NL2SQL_PROVIDER=azure-openai"
    echo "AZURE_OPENAI_ENDPOINT=$AOAI_ENDPOINT"
    echo "AZURE_OPENAI_API_KEY=$AOAI_KEY"
    echo "AZURE_OPENAI_CHAT_DEPLOYMENT=$(out chatDeploymentName)"
  } >> "$OUT_ENV"
fi
chmod 600 "$OUT_ENV"

cat <<MSG

Done. Connection details are in .env.azure (mode 600, git-ignored).

Next:
  cp .env.azure .env
  nl2sql schema --semantic sample
  nl2sql ask "What are the top 5 products by revenue?" --semantic sample --explain
  nl2sql eval --limit 5                 # a cheap trial: 5 gold + the adversarial set
  nl2sql eval --name azure-openai-mssql --out-dir docs/eval

When you are finished: ./infra/down.sh
MSG
