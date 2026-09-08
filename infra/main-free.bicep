// The no-cost deployment.
//
//   az deployment group create -g <group> -f infra/main-free.bicep \
//      -p prefix=iragst githubOwner=Glory17805
//
// Everything here sits inside a free allowance, with one exception noted
// below. It trades a managed database for SQLite on the file share, which is
// the only way to reach zero on a pay-as-you-go subscription: Azure's free
// PostgreSQL offer applies to free accounts only, and this is a company
// subscription.
//
// What that trade costs, and how it is contained:
//
//   SQLite cannot use WAL on SMB - WAL coordinates through shared memory that
//   SMB does not implement - so the container runs with journal_mode=DELETE
//   and synchronous=FULL. That is safe for one writer, which is all this app
//   ever has, but a connection dropped mid-write can still damage the file.
//   The app takes a verified backup before every posting session, which turns
//   that from losing the filing history into losing an hour of it.
//
// The exception: Azure Files bills for what is stored. A few hundred MB of
// workbooks and PDFs is a few cents a month. There is no free persistent file
// storage on Azure, so this is as close to zero as the platform allows.

@description('Short name used as the prefix for every resource.')
@minLength(3)
@maxLength(11)
param prefix string = 'iragst'

@description('Region. Central India keeps Indian tax records in-country.')
param location string = 'centralindia'

@description('GitHub account or org owning the container images on ghcr.io.')
param githubOwner string

@description('Claude API key. Empty deploys the offline reader, which is fully functional.')
@secure()
param anthropicApiKey string = ''

@description('Only needed if the ghcr.io packages are private. Leave empty for public images.')
@secure()
param ghcrToken string = ''

var storageName = '${prefix}store'
var shareName = 'data'
var envName = '${prefix}-env'
var apiName = '${prefix}-api'
var webName = '${prefix}-web'
var imageBase = 'ghcr.io/${toLower(githubOwner)}'

// --------------------------------------------------------------------------
// Storage - the workbooks, the PDFs, and (on this tier) the database
// --------------------------------------------------------------------------

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    // The database lives on this share on this tier, so soft delete is not a
    // nicety. It is the second line behind the app's own backups.
    shareDeleteRetentionPolicy: {
      enabled: true
      days: 14
    }
  }
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: shareName
  properties: {
    accessTier: 'TransactionOptimized'
    shareQuota: 20
  }
}

// --------------------------------------------------------------------------
// Container Apps
// --------------------------------------------------------------------------

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
  location: location
  properties: {
    // No Log Analytics workspace attached, on purpose: a workspace bills for
    // ingestion beyond its free grant and is the usual way a "free" Container
    // Apps deployment quietly starts costing money. Logs are still readable
    // with `az containerapp logs show`.
    appLogsConfiguration: { destination: '' }
  }
}

resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: environment
  name: 'data'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: shareName
      accessMode: 'ReadWrite'
    }
  }
}

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: apiName
  location: location
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        corsPolicy: {
          allowedOrigins: [ 'https://${web.properties.defaultHostname}' ]
          allowedMethods: [ 'GET', 'POST', 'PATCH', 'PUT', 'DELETE', 'OPTIONS' ]
          allowedHeaders: [ '*' ]
        }
      }
      secrets: concat(
        [ { name: 'anthropic-key', value: anthropicApiKey } ],
        empty(ghcrToken) ? [] : [ { name: 'ghcr-token', value: ghcrToken } ]
      )
      registries: empty(ghcrToken) ? [] : [
        {
          server: 'ghcr.io'
          username: githubOwner
          passwordSecretRef: 'ghcr-token'
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: '${imageBase}/gst-api:latest'
          // The smallest combination Container Apps allows. At 0.25 vCPU the
          // monthly free grant covers roughly 200 hours of running time, so
          // scaling to zero between working sessions is what keeps this free.
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            { name: 'GST_DATA_DIR', value: '/data' }
            { name: 'GST_SOURCE_WORKBOOK', value: '/data/master/workbook.xlsx' }
            { name: 'GST_BACKUP_DIR', value: '/data/backups' }
            // Required, not optional: WAL does not work over SMB, and the
            // failure is a database that reads as corrupt rather than an error.
            { name: 'GST_SQLITE_JOURNAL', value: 'DELETE' }
            { name: 'GST_BACKUP_MAX_AGE_MINUTES', value: '60' }
            { name: 'GST_FRONTEND_ORIGINS', value: 'https://${web.properties.defaultHostname}' }
            { name: 'GST_APPROVAL_MODE', value: 'flagged_only' }
            { name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-key' }
            { name: 'GST_EXTRACTION_PROVIDER', value: empty(anthropicApiKey) ? 'offline' : 'claude' }
          ]
          volumeMounts: [
            { volumeName: 'data', mountPath: '/data' }
          ]
        }
      ]
      volumes: [
        {
          name: 'data'
          storageType: 'AzureFile'
          storageName: envStorage.name
        }
      ]
      scale: {
        // Zero when idle, which is what keeps this inside the free grant.
        // One at most, always: workbook.py's lock is in-process only, and
        // singleton.py takes a kernel lock on the shared volume, so a second
        // replica would not corrupt quietly - it would fail to start. Either
        // way the ceiling is 1 and it is a correctness bound, not a budget.
        minReplicas: 0
        maxReplicas: 1
      }
    }
  }
}

// --------------------------------------------------------------------------
// Frontend - Static Web Apps, whose Free tier genuinely is free
// --------------------------------------------------------------------------

resource web 'Microsoft.Web/staticSites@2023-12-01' = {
  name: webName
  location: location
  sku: {
    name: 'Free'
    tier: 'Free'
  }
  properties: {
    // Content is pushed from the deploy script with the SWA CLI rather than
    // built by Azure from the repository, because the only build step is
    // writing config.js and there is no reason to hand Azure a repo token.
    allowConfigFileUpdates: true
    stagingEnvironmentPolicy: 'Disabled'
  }
}

output apiUrl string = 'https://${api.properties.configuration.ingress.fqdn}'
output webUrl string = 'https://${web.properties.defaultHostname}'
output webName string = web.name
output storageAccount string = storage.name
output shareName string = shareName
output imageBase string = imageBase
