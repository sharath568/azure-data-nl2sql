// nl2sql: lowest-cost Azure setup for "natural language to SQL on Azure SQL".
//
// Creates, in the target resource group:
//   * an Azure SQL logical server (TLS 1.2, one firewall rule for your IP)
//   * one Azure SQL Database on the **free offer**: serverless General Purpose
//     (GP_S_Gen5_2), useFreeLimit = true, freeLimitExhaustionBehavior = AutoPause.
//     When the monthly free allowance runs out the database pauses until the
//     next month instead of billing you.
//   * optionally an Azure OpenAI account with one pay-per-token gpt-4.1-mini deployment
//   * a monthly budget on the resource group with e-mail alerts at 50/80/100%
//
// Deploy with infra/up.sh, or:
//   az deployment group create -g <rg> -f infra/main.bicep \
//     -p clientIp=<your ip> adminPassword=<secret> budgetEmail=<you@example.com>
//
// Offer terms, SKUs and model versions change. Check infra/README.md.

targetScope = 'resourceGroup'

@description('Region for the SQL server and database.')
param location string = resourceGroup().location

@description('Region for Azure OpenAI. Not every region offers every model; see infra/README.md.')
param openAiLocation string = location

@description('Globally unique name for the Azure SQL logical server.')
@minLength(3)
@maxLength(63)
param serverName string = 'nl2sql-${uniqueString(resourceGroup().id)}'

@description('SQL admin login. Used only to run the setup scripts; nl2sql itself connects as nl2sql_reader.')
param adminLogin string = 'nl2sqladmin'

@description('SQL admin password.')
@secure()
@minLength(12)
param adminPassword string

@description('Optional: a Microsoft Entra user or group to make server admin (display name). Leave empty to skip.')
param entraAdminLogin string = ''

@description('Object ID of entraAdminLogin. Required when entraAdminLogin is set.')
param entraAdminObjectId string = ''

@description('Your public IP address, allowed through the server firewall.')
param clientIp string

@description('Database name.')
param databaseName string = 'nl2sql_sample'

@description('Use the Azure SQL Database free offer (one per subscription region limits apply). Set false to get a normal serverless database that bills per use.')
param useFreeLimit bool = true

@description('Minutes of inactivity before the serverless database auto-pauses (minimum 15).')
@minValue(15)
param autoPauseDelayMinutes int = 60

@description('Globally unique name (and custom subdomain) for the Azure OpenAI account.')
param openAiName string = 'nl2sql-aoai-${uniqueString(resourceGroup().id)}'

@description('Deploy Azure OpenAI. The offline stub provider needs none of it.')
param deployOpenAi bool = true

@description('Chat deployment SKU. GlobalStandard is the most widely available pay-per-token option for gpt-4.1-mini; Standard keeps processing in-region where offered.')
@allowed(['Standard', 'GlobalStandard'])
param chatDeploymentSku string = 'GlobalStandard'

@description('Chat model version.')
param chatModelVersion string = '2025-04-14'

@description('Rate limit for the chat deployment, in thousands of tokens per minute. This caps throughput, not cost.')
@minValue(1)
param chatCapacityK int = 10

@description('Monthly budget for this resource group, in your billing currency.')
@minValue(1)
param monthlyBudget int = 10

@description('E-mail address that receives budget alerts.')
param budgetEmail string

@description('First day of the budget period (must be the first of a month). Defaults to this month.')
param budgetStartDate string = utcNow('yyyy-MM-01')

var tags = {
  project: 'nl2sql'
  purpose: 'community-demo'
}

// ---------------------------------------------------------------------------
// Azure SQL
// ---------------------------------------------------------------------------

resource sqlServer 'Microsoft.Sql/servers@2023-08-01' = {
  name: serverName
  location: location
  tags: tags
  properties: {
    administratorLogin: adminLogin
    administratorLoginPassword: adminPassword
    version: '12.0'
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    administrators: empty(entraAdminLogin)
      ? null
      : {
          administratorType: 'ActiveDirectory'
          login: entraAdminLogin
          sid: entraAdminObjectId
          tenantId: subscription().tenantId
          principalType: 'User'
          azureADOnlyAuthentication: false
        }
  }
}

resource firewall 'Microsoft.Sql/servers/firewallRules@2023-08-01' = {
  parent: sqlServer
  name: 'client-ip'
  properties: {
    startIpAddress: clientIp
    endIpAddress: clientIp
  }
}

resource database 'Microsoft.Sql/servers/databases@2023-08-01' = {
  parent: sqlServer
  name: databaseName
  location: location
  tags: tags
  sku: {
    name: 'GP_S_Gen5_2'
    tier: 'GeneralPurpose'
    family: 'Gen5'
    capacity: 2
  }
  properties: {
    // The free offer: a monthly allowance of vCore seconds and 32 GB of data.
    useFreeLimit: useFreeLimit
    // Pause (do not bill) when the allowance is used up. 'BillOverUsage' is the other option.
    freeLimitExhaustionBehavior: useFreeLimit ? 'AutoPause' : null
    autoPauseDelay: autoPauseDelayMinutes
    minCapacity: json('0.5')
    maxSizeBytes: 34359738368 // 32 GB, the free-offer limit
    zoneRedundant: false
    requestedBackupStorageRedundancy: 'Local'
  }
}

// ---------------------------------------------------------------------------
// Azure OpenAI (pay per token)
// ---------------------------------------------------------------------------

resource openAi 'Microsoft.CognitiveServices/accounts@2024-10-01' = if (deployOpenAi) {
  name: openAiName
  location: openAiLocation
  tags: tags
  kind: 'OpenAI'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: openAiName
    publicNetworkAccess: 'Enabled'
  }
}

resource chatDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = if (deployOpenAi) {
  parent: openAi
  name: 'gpt-4.1-mini'
  sku: {
    name: chatDeploymentSku
    capacity: chatCapacityK
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-4.1-mini'
      version: chatModelVersion
    }
  }
}

// ---------------------------------------------------------------------------
// Budget with alerts (an alert, not a cap)
// ---------------------------------------------------------------------------

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: 'nl2sql-monthly-budget'
  properties: {
    category: 'Cost'
    amount: monthlyBudget
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: budgetStartDate
    }
    notifications: {
      actual50: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 50
        thresholdType: 'Actual'
        contactEmails: [budgetEmail]
      }
      actual80: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: [budgetEmail]
      }
      actual100: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Actual'
        contactEmails: [budgetEmail]
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Outputs (no secrets)
// ---------------------------------------------------------------------------

output serverName string = sqlServer.name
output serverFqdn string = sqlServer.properties.fullyQualifiedDomainName
output databaseName string = database.name
output connTemplate string = 'Server=tcp:${sqlServer.properties.fullyQualifiedDomainName},1433;Database=${databaseName};UID=<user>;PWD=<password>;Encrypt=yes;TrustServerCertificate=no'
output openAiEndpoint string = deployOpenAi ? openAi!.properties.endpoint : ''
output openAiName string = deployOpenAi ? openAi.name : ''
output chatDeploymentName string = deployOpenAi ? chatDeployment.name : ''
