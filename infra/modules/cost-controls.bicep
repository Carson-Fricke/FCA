targetScope = 'resourceGroup'

@description('Name of the Log Analytics workspace used by the Container Apps environment. This module keeps the workspace security posture aligned with the shared Container Apps module while applying the short retention policy.')
@minLength(4)
@maxLength(63)
param logAnalyticsWorkspaceName string

@description('Name prefix for the two resource-group monthly budgets. The module appends 40usd and 70usd to keep each exact-dollar alert independently addressable.')
@minLength(1)
@maxLength(50)
param budgetName string = 'market-lake'

@description('Azure region for the Log Analytics workspace.')
param location string = resourceGroup().location

@description('Tags applied to the Log Analytics workspace.')
param tags object = {}

@description('Default analytics retention for the Log Analytics workspace. The market-lake settings use seven days; Azure supports four days as the minimum for Analytics tables.')
@minValue(4)
@maxValue(730)
param logRetentionDays int = 7

@description('First exact monthly budget alert amount in USD. Each budget uses a 100 percent actual-cost notification so the alert amount is literal rather than a percentage approximation.')
@minValue(1)
param firstBudgetAlertUsd int = 40

@description('Second exact monthly budget alert amount in USD. Each budget uses a 100 percent actual-cost notification so the alert amount is literal rather than a percentage approximation.')
@minValue(1)
param secondBudgetAlertUsd int = 70

@description('First day of the month from which the monthly budgets evaluate cost, in UTC ISO 8601 format. Azure requires this value to be the first day of a month.')
@minLength(10)
param budgetStartDate string

@description('Email recipients for Cost Management notifications. Leave empty when an existing action group is supplied; no recipient values are embedded in this module.')
param alertContactEmails array = []

@description('Existing Action Group resource IDs for Cost Management notifications. Azure requires at least one email or action group recipient when the budgets are deployed.')
param alertContactGroups array = []

@description('Azure role names that may receive Cost Management notifications. The default is empty so callers choose the intended subscription or resource-group recipients.')
param alertContactRoles array = []

@description('Storage capacity ceiling in bytes. The runtime must pause enqueue and ingest before this ceiling is exceeded.')
@minValue(1)
param storageCapacityBytes int = 1099511627776

@description('Pause backfill enqueue when the configured storage capacity is reached.')
param pauseEnqueueAtCapacity bool = true

@description('Pause ingest when the configured storage capacity is reached.')
param pauseIngestAtCapacity bool = true

@description('Maximum concurrent replicas for newest-first backfill. The plan requires one replica.')
@minValue(1)
@maxValue(1)
param backfillMaxReplicas int = 1

@description('Maximum SQL bytes that a query may scan. The plan bounds this at one GiB.')
@minValue(1)
@maxValue(1073741824)
param sqlMaxScanBytes int = 1073741824

@description('Maximum SQL result bytes returned to a caller. The plan bounds this at 32 MiB.')
@minValue(1)
@maxValue(33554432)
param sqlMaxResultBytes int = 33554432

var monthlyBudgetAlertAmountsUsd = [
  firstBudgetAlertUsd
  secondBudgetAlertUsd
]
var monthlyBudgetIds = [for index in range(0, length(monthlyBudgetAlertAmountsUsd)): monthlyBudgets[index].id]

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsWorkspaceName
  location: location
  tags: tags
  properties: {
    features: {
      disableLocalAuth: true
      enableLogAccessUsingOnlyResourcePermissions: true
    }
    publicNetworkAccessForIngestion: 'Disabled'
    publicNetworkAccessForQuery: 'Disabled'
    retentionInDays: logRetentionDays
    sku: {
      name: 'PerGB2018'
    }
  }
}

resource monthlyBudgets 'Microsoft.Consumption/budgets@2024-08-01' = [for alertAmountUsd in monthlyBudgetAlertAmountsUsd: {
  name: '${budgetName}-${alertAmountUsd}usd'
  properties: {
    amount: alertAmountUsd
    category: 'Cost'
    notifications: {
      Actual_GreaterThanOrEqualTo_100_Percent: {
        contactEmails: alertContactEmails
        contactGroups: alertContactGroups
        contactRoles: alertContactRoles
        enabled: true
        locale: 'en-us'
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Actual'
      }
    }
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: budgetStartDate
    }
  }
}]

output logAnalyticsWorkspace object = {
  name: logAnalyticsWorkspace.name
  resourceId: logAnalyticsWorkspace.id
  retentionInDays: logRetentionDays
}

output monthlyBudgetAlerts object = {
  amountsUsd: monthlyBudgetAlertAmountsUsd
  budgetIds: monthlyBudgetIds
  notificationThresholdPercent: 100
  notificationType: 'Actual'
  timeGrain: 'Monthly'
}

@description('Non-secret guardrails for future composition into runtime workload settings. Azure does not provide a native storage-byte or SQL-scan quota on these resources, so the application must enforce these values.')
output runtimeGuardrails object = {
  storage: {
    capacityBytes: storageCapacityBytes
    pauseEnqueueAtCapacity: pauseEnqueueAtCapacity
    pauseIngestAtCapacity: pauseIngestAtCapacity
  }
  ingest: {
    backfillMaxReplicas: backfillMaxReplicas
  }
  sql: {
    maxScanBytes: sqlMaxScanBytes
    maxResultBytes: sqlMaxResultBytes
  }
  operations: {
    logRetentionDays: logRetentionDays
    monthlyBudgetAlertsUsd: monthlyBudgetAlertAmountsUsd
  }
}
