#!/usr/bin/env bash
# Delete the whole nl2sql resource group: the SQL server, the database, the
# Azure OpenAI account and the budget. There is no undo.
set -euo pipefail
RG="${RG:-nl2sql-rg}"
command -v az >/dev/null || { echo "Azure CLI (az) is required" >&2; exit 1; }
echo "Deleting resource group $RG (runs in the background)..."
az group delete --name "$RG" --yes --no-wait
echo "Requested. Check progress with: az group show --name $RG"
echo "Remember to delete .env.azure / .env, which hold the (now useless) credentials."
