targetScope = 'resourceGroup'

@description('Globally unique Storage account name.')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Azure region for the Storage account.')
param location string = resourceGroup().location

@description('Tags applied to the Storage account.')
param tags object = {}

@description('Days after last modification before lake block blobs move from Hot to Cool.')
@minValue(1)
param coolAfterDays int = 14

@description('Whether lake block blobs also move to the Cold tier.')
param enableColdTier bool = false

@description('Days after last modification before lake block blobs move to Cold when enabled.')
@minValue(1)
param coldAfterDays int = 90

var lakeContainerName = 'lake'
var harnessContainerNames = {
  results: 'harness-results'
  state: 'harness-state'
}
var tableNames = {
  coverage: 'coverage'
  jobs: 'jobs'
  contracts: 'contracts'
  backtests: 'backtests'
  // Azure Table names allow alphanumeric characters only, so the kill-switch
  // control table cannot carry a hyphen.
  killSwitch: 'killswitch'
}
var queueNames = {
  backfill: 'backfill'
  deadLetter: 'backfill-poison'
}
var coldTierAction = enableColdTier ? {
  tierToCold: {
    daysAfterModificationGreaterThan: coldAfterDays
  }
} : {}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowCrossTenantReplication: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    isHnsEnabled: true
    isLocalUserEnabled: false
    isNfsV3Enabled: false
    isSftpEnabled: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  name: 'default'
  parent: storageAccount
  properties: {
    isVersioningEnabled: false
  }
}

resource lakeContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  name: lakeContainerName
  parent: blobService
  properties: {
    publicAccess: 'None'
  }
}

resource harnessContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for containerName in items(harnessContainerNames): {
  name: containerName.value
  parent: blobService
  properties: {
    publicAccess: 'None'
  }
}]

resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2023-05-01' = {
  name: 'default'
  parent: storageAccount
  properties: {}
}

resource queues 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = [for queueName in items(queueNames): {
  name: queueName.value
  parent: queueService
  properties: {}
}]

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  name: 'default'
  parent: storageAccount
  properties: {}
}

resource tables 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = [for tableName in items(tableNames): {
  name: tableName.value
  parent: tableService
  properties: {
    signedIdentifiers: []
  }
}]

resource lifecyclePolicy 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  name: 'default'
  parent: storageAccount
  properties: {
    policy: {
      rules: [
        {
          name: 'lake-hot-to-cool'
          enabled: true
          type: 'Lifecycle'
          definition: {
            actions: {
              baseBlob: union({
                tierToCool: {
                  daysAfterModificationGreaterThan: coolAfterDays
                }
              }, coldTierAction)
            }
            filters: {
              blobTypes: [
                'blockBlob'
              ]
              prefixMatch: [
                '${lakeContainerName}/'
              ]
            }
          }
        }
      ]
    }
  }
}

output storageAccountName string = storageAccount.name
output storageAccountId string = storageAccount.id
output endpoints object = {
  blob: storageAccount.properties.primaryEndpoints.blob
  dfs: storageAccount.properties.primaryEndpoints.dfs
  queue: storageAccount.properties.primaryEndpoints.queue
  table: storageAccount.properties.primaryEndpoints.table
}
output resourceNames object = {
  lakeContainer: lakeContainer.name
  harnessContainers: harnessContainerNames
  queues: queueNames
  tables: tableNames
}
