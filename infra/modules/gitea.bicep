targetScope = 'resourceGroup'

// Open-source Gitea only: no managed forge, database service, or public ingress.
// /data survives replica replacement in an isolated Standard_LRS Azure Files
// share. LRS is not a backup or regional disaster recovery; operators must back
// up the repositories, app.ini, and database together and test restoration.
// SQLite on SMB depends on network filesystem byte-range locking and is not
// equivalent to local disk. Use DELETE journaling (not WAL), retain SMB locks,
// and validate locking/recovery under the intended workload before production.
// One replica and Single revision mode prevent steady-state scale-out, but ACA
// can overlap old/new replicas during updates. Stop the old writer before an
// image/config update; never run a second app or bootstrap writer on this share.
// The operator creates the admin, organization, and scoped API token after
// startup (admin CLI via container exec, then API from inside the environment).
// Store the token in Key Vault under thesisGiteaTokenSecretName; this module
// generates no admin credentials, organization, or provider token.

@description('Name of the internal Gitea Container App.')
@minLength(2)
@maxLength(32)
param appName string

@description('Globally unique storage account name isolated from lake and Thesis storage.')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Resource ID of the existing shared Container Apps environment.')
@minLength(1)
param containerAppsEnvironmentResourceId string

@description('Official rootful image from docker.io/gitea/gitea (or gitea/gitea) or docker.gitea.com/gitea, followed by @sha256: and 64 lowercase hex characters. Invalid references fail closed on image pull.')
param giteaImageReference string

@description('Azure region inherited from the composition.')
param location string = resourceGroup().location

@description('Tags inherited from the composition.')
param tags object = {}

var shareName = 'gitea-data'
// App-specific registration names avoid collisions in the shared environment.
var environmentStorageName = '${appName}-data'
var imageDigest = last(split(giteaImageReference, '@sha256:'))
var imageValid = (startsWith(giteaImageReference, 'docker.io/gitea/gitea@sha256:') || startsWith(giteaImageReference, 'gitea/gitea@sha256:') || startsWith(giteaImageReference, 'docker.gitea.com/gitea@sha256:')) && length(imageDigest) == 64 && empty(filter(range(0, length(imageDigest)), i => !contains('0123456789abcdef', substring(imageDigest, i, 1))))

resource environment 'Microsoft.App/managedEnvironments@2025-07-01' existing = {
  name: last(split(containerAppsEnvironmentResourceId, '/'))
}

// Internal ACA ingress inserts .internal. before the environment DNS suffix.
// Derive the advertised root before app creation to avoid a self-reference.
var hostname = '${appName}.internal.${environment.properties.defaultDomain}'
var rootUrl = 'https://${hostname}'

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
    // ACA Azure Files SMB mounting requires shared-key authentication. The
    // key is used only in the environment mount, never container env or outputs.
    allowSharedKeyAccess: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
  properties: {}
}

resource dataShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: shareName
  properties: {
    enabledProtocols: 'SMB'
    shareQuota: 100
  }
}

resource environmentStorage 'Microsoft.App/managedEnvironments/storages@2025-07-01' = {
  parent: environment
  name: environmentStorageName
  properties: {
    azureFile: {
      accessMode: 'ReadWrite'
      accountName: storageAccount.name
      shareName: dataShare.name
      accountKey: storageAccount.listKeys().keys[0].value
    }
  }
}

resource giteaApp 'Microsoft.App/containerApps@2025-07-01' = {
  name: appName
  location: location
  tags: tags
  properties: {
    environmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        allowInsecure: false
        targetPort: 3000
        transport: 'http'
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
    }
    template: {
      containers: [
        {
          name: 'gitea'
          // Assertions are not enabled in this repo. The reserved .invalid
          // registry prevents mutable/empty/non-official images from running.
          image: imageValid ? giteaImageReference : 'invalid.invalid/gitea/requires-official-sha256-digest:invalid'
          // Preserve the official image's entrypoint and default command.
          // Gitea SSH is disabled below; ingress exposes only HTTP port 3000.
          env: [
            {
              name: 'GITEA____APP_NAME'
              value: 'FCA workspace Git'
            }
            {
              name: 'GITEA__server__DOMAIN'
              value: hostname
            }
            {
              name: 'GITEA__server__ROOT_URL'
              value: '${rootUrl}/'
            }
            {
              name: 'GITEA__server__PUBLIC_URL_DETECTION'
              value: 'never'
            }
            {
              name: 'GITEA__server__PROTOCOL'
              value: 'http'
            }
            {
              name: 'GITEA__server__HTTP_ADDR'
              value: '0.0.0.0'
            }
            {
              name: 'GITEA__server__HTTP_PORT'
              value: '3000'
            }
            {
              name: 'GITEA__server__APP_DATA_PATH'
              value: '/data/gitea'
            }
            {
              name: 'GITEA__server__DISABLE_SSH'
              value: 'true'
            }
            {
              name: 'GITEA__server__START_SSH_SERVER'
              value: 'false'
            }
            {
              name: 'GITEA__security__INSTALL_LOCK'
              value: 'true'
            }
            {
              name: 'GITEA__service__DISABLE_REGISTRATION'
              value: 'true'
            }
            {
              name: 'GITEA__service__REQUIRE_SIGNIN_VIEW'
              value: 'true'
            }
            {
              name: 'GITEA__database__DB_TYPE'
              value: 'sqlite3'
            }
            {
              name: 'GITEA__database__PATH'
              value: '/data/gitea/gitea.db'
            }
            {
              name: 'GITEA__database__SQLITE_JOURNAL_MODE'
              value: 'DELETE'
            }
            {
              name: 'GITEA__repository__ROOT'
              value: '/data/git/repositories'
            }
            {
              name: 'GITEA__repository__FORCE_PRIVATE'
              value: 'true'
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          volumeMounts: [
            {
              volumeName: 'data'
              mountPath: '/data'
            }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/api/healthz'
                port: 3000
                scheme: 'HTTP'
              }
              failureThreshold: 30
              periodSeconds: 10
              timeoutSeconds: 5
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/api/healthz'
                port: 3000
                scheme: 'HTTP'
              }
              periodSeconds: 10
              timeoutSeconds: 5
            }
          ]
        }
      ]
      volumes: [
        {
          name: 'data'
          storageType: 'AzureFile'
          storageName: environmentStorage.name
          // Match the official image's git UID/GID without disabling SMB locks.
          mountOptions: 'uid=1000,gid=1000,dir_mode=0755,file_mode=0644,cache=strict'
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

@description('Internal HTTPS base URL shared by Gitea ROOT_URL and the Thesis remote workspace provider. No credentials or account keys are output.')
output endpoint string = 'https://${giteaApp.properties.configuration.ingress.fqdn}'
