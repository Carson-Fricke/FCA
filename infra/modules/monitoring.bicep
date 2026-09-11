targetScope = 'resourceGroup'

@description('Composed deployment name prefix, such as fca-ml-dev. Alert resource names and the trader Container App name pattern derive from it.')
@minLength(2)
param namePrefix string

@description('Name of the existing Log Analytics workspace that receives Container Apps logs through the environment diagnostic setting. The module references the workspace; it never creates one.')
@minLength(1)
param logAnalyticsWorkspaceName string

@description('REQUIRED USER INPUT: valid email address for the trader alert action group. Leave unset to fail validation; the module never embeds a recipient value.')
@minLength(3)
param traderAlertContactEmail string

@description('Short display name of the trader alert action group. Azure limits this to 12 characters.')
@minLength(3)
@maxLength(12)
param actionGroupShortName string = 'traderalerts'

@description('ISO 8601 evaluation cadence for the trader log query alerts.')
@allowed([
  'PT1M'
  'PT5M'
  'PT15M'
])
param alertFrequencyIso string = 'PT5M'

@description('ISO 8601 look-back window for the trader log query alerts.')
@allowed([
  'PT5M'
  'PT15M'
  'PT30M'
  'PT1H'
])
param alertWindowIso string = 'PT15M'

@description('Tags applied to every monitoring resource.')
param tags object = {}

var actionGroupName = '${namePrefix}-trader-alerts'
var faultAlertName = '${namePrefix}-trader-faults'
var haltAlertName = '${namePrefix}-trader-halts'
var restartAlertName = '${namePrefix}-trader-restarts'

// The supervision strings are the exact detail prefixes the trader supervision
// kernel is expected to write once the supervision runner is ported into the
// Rust `live` crate (the Python supervision runtime was deleted with the
// engine; see .github/architecture/rust-interface-migration.md). Console
// matches fire only for text the trader process prints today; the remaining
// strings arm the same rules for the supervision console emit path without a
// rule change. See .github/runbooks/go-live.md for the per-alert drill steps and
// the automatable-today versus manual boundary.
var traderAppFilter = 'ContainerAppName_s startswith \'${namePrefix}-\' and ContainerAppName_s endswith \'-trader\''

var faultQuery = 'ContainerAppConsoleLogs_CL | where ${traderAppFilter} | where Log_s has_any (\'restartable fault\', \'market-wait exceeded\', \'trader feed boundary fault\', \'Traceback\') | project TimeGenerated, ContainerAppName_s, RevisionName_s, Log_s'
var haltQuery = 'ContainerAppConsoleLogs_CL | where ${traderAppFilter} | where Log_s has_any (\'kill-switch engaged\', \'reconciliation aborted\', \'daily_loss_halt limit reached\', \'restart budget exhausted\', \'supervision ledger write failed\') | project TimeGenerated, ContainerAppName_s, RevisionName_s, Log_s'

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource traderActionGroup 'Microsoft.Insights/actionGroups@2022-06-01' = {
  name: actionGroupName
  location: 'global'
  tags: tags
  properties: {
    enabled: true
    groupShortName: actionGroupShortName
    emailReceivers: [
      {
        name: 'trader-operator'
        emailAddress: traderAlertContactEmail
      }
    ]
  }
}

resource traderFaultAlert 'Microsoft.Insights/scheduledQueryRules@2021-08-01' = {
  name: faultAlertName
  location: resourceGroup().location
  tags: tags
  kind: 'LogAlert'
  properties: {
    description: 'Trader console faults: restartable faults, market-wait exceeded, feed boundary faults, and process tracebacks on the trader Container Apps. Some matched strings require the supervision console emit path before they can appear; see the go-live runbook.'
    enabled: true
    severity: 2
    evaluationFrequency: alertFrequencyIso
    windowSize: alertWindowIso
    scopes: [
      logAnalyticsWorkspace.id
    ]
    autoMitigate: true
    criteria: {
      allOf: [
        {
          query: faultQuery
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
        }
      ]
    }
    actions: {
      actionGroups: [
        traderActionGroup.id
      ]
    }
  }
}

resource traderHaltAlert 'Microsoft.Insights/scheduledQueryRules@2021-08-01' = {
  name: haltAlertName
  location: resourceGroup().location
  tags: tags
  kind: 'LogAlert'
  properties: {
    description: 'Trader halt-class events: kill-switch engagement, reconciliation aborts, daily-loss halts, crash-loop budget exhaustion, and supervision ledger write failures on the trader Container Apps. Matched strings are armed for the supervision console emit path; see the go-live runbook for the manual checks that cover them today.'
    enabled: true
    severity: 1
    evaluationFrequency: alertFrequencyIso
    windowSize: alertWindowIso
    scopes: [
      logAnalyticsWorkspace.id
    ]
    autoMitigate: true
    criteria: {
      allOf: [
        {
          query: haltQuery
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
        }
      ]
    }
    actions: {
      actionGroups: [
        traderActionGroup.id
      ]
    }
  }
}

resource traderRestartAlert 'Microsoft.Insights/activityLogAlerts@2020-10-01' = {
  name: restartAlertName
  location: 'global'
  tags: tags
  properties: {
    description: 'Operator and deployment management operations on the trader Container Apps: revision restarts, min-replica scale changes, and configuration updates. Platform-internal replica reboots do not appear in the activity log; query alerts and supervision documents cover those.'
    enabled: true
    scopes: [
      resourceGroup().id
    ]
    condition: {
      allOf: [
        {
          field: 'resourceType'
          equals: 'Microsoft.App/containerApps'
        }
        {
          field: 'resourceUri'
          containsAny: [
            'trader'
          ]
        }
        {
          anyOf: [
            {
              field: 'operationName'
              equals: 'Microsoft.App/containerApps/write'
            }
            {
              field: 'operationName'
              equals: 'Microsoft.App/containerApps/revisions/restart/action'
            }
          ]
        }
      ]
    }
    actions: {
      actionGroups: [
        traderActionGroup
      ]
    }
  }
}

output actionGroupId string = traderActionGroup.id
output alertResourceIds array = [
  traderFaultAlert.id
  traderHaltAlert.id
  traderRestartAlert.id
]

@description('Non-secret summary of the trader monitoring composition for deployment verification.')
output monitoringSummary object = {
  actionGroup: actionGroupName
  emailReceiverCount: 1
  queryAlerts: {
    faultClass: faultAlertName
    haltClass: haltAlertName
    frequency: alertFrequencyIso
    window: alertWindowIso
    scope: logAnalyticsWorkspaceName
    appNamePattern: '${namePrefix}-*-trader'
  }
  activityLogAlert: {
    name: restartAlertName
    resourceType: 'Microsoft.App/containerApps'
    operations: [
      'Microsoft.App/containerApps/write'
      'Microsoft.App/containerApps/revisions/restart/action'
    ]
  }
  consoleMatchNote: 'Supervision-detail strings beyond feed faults and tracebacks require the trader console emit path; supervision documents remain manual checks until then.'
}
