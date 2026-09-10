// The no-cost deployment.
//
//   az deployment group create -g <group> -f infra/main-free.bicep \
//      -p prefix=iragst githubOwner=Glory17805 dbAdminPassword=<generated>
//
// Container Apps for the API, scaling to zero between sessions and staying
// inside the monthly free grant. Static Web Apps for the frontend, whose Free
// tier genuinely is free. GitHub Container Registry for the image, so there is
// no registry bill. Azure Files for the workbooks and PDFs.
//
// The database is a parameter, because which one is free depends on the
// subscription rather than on anything technical:
//
//   postgres  On an Azure free account, Flexible Server B1ms is free for the
//             first 12 months, which makes the right answer also the free one.
//             This is the default.
//
//   sqlite    On a pay-as-you-go subscription there is no free managed
//             database, so reaching zero means SQLite on the file share. That
//             works, and needs care: WAL cannot be used on SMB - it
//             coordinates through shared memory SMB does not implement, and
//             fails as a database that reads as corrupt rather than an error -
//             so the container runs journal_mode=DELETE with synchronous=FULL,
//             and the app takes a verified backup before every posting
//             session. That turns a mid-write disconnection from losing the
//             filing history into losing an hour of it.
//
// Azure Files bills for what is stored either way. A few hundred MB is a few
// cents a month, and a free account's first 12 months include more than that.

@description('Short name used as the prefix for every resource.')
@minLength(3)
@maxLength(11)
param prefix string = 'iragst'

@description('Region. Central India keeps Indian tax records in-country.')
param location string = 'centralindia'

@description('GitHub account or org owning the container images on ghcr.io.')
param githubOwner string

@description('Managed PostgreSQL, or SQLite on the file share. See the note above.')
@allowed([ 'postgres', 'sqlite' ])
param database string = 'postgres'

@description('PostgreSQL administrator login. Ignored when database is sqlite.')
param dbAdminUser string = 'gstadmin'

@description('PostgreSQL administrator password. Required when database is postgres.')
@secure()
param dbAdminPassword string = ''

@description('Claude API key. Empty deploys the offline reader, which is fully functional.')
@secure()
param anthropicApiKey string = ''

@description('Only needed if the ghcr.io packages are private. Leave empty for public images.')
@secure()
param ghcrToken string = ''

var usePostgres = database == 'postgres'
var storageName = '${prefix}store'
var shareName = 'data'
var envName = '${prefix}-env'
var apiName = '${prefix}-api'
var webName = '${prefix}-web'
var dbServerName = '${prefix}-db'
var dbName = 'gst'
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
// PostgreSQL - only when asked for
// --------------------------------------------------------------------------

resource database_ 'Microsoft.DBforPostgreSQL/flexibleServers@2023-06-01-preview' = if (usePostgres) {
  name: dbServerName
  location: location
  sku: {
    // The exact SKU an Azure free account covers for 12 months. Changing it
    // is what turns this deployment from free into billed.
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: dbAdminUser
    administratorLoginPassword: dbAdminPassword
    storage: { storageSizeGB: 32 }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: { mode: 'Disabled' }
    network: { publicNetworkAccess: 'Enabled' }
  }
}

resource gstDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-06-01-preview' = if (usePostgres) {
  parent: database_
  name: dbName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// citext is unavailable until allowlisted here. users.email is declared CITEXT
// so case-insensitive login behaves identically to SQLite; without this the
// very first request fails with 'type "citext" does not exist'.
resource allowCitext 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2023-06-01-preview' = if (usePostgres) {
  parent: database_
  name: 'azure.extensions'
  properties: {
    value: 'CITEXT'
    source: 'user-override'
  }
}

resource allowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-06-01-preview' = if (usePostgres) {
  parent: database_
  name: 'allow-azure-services'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

var databaseUrl = usePostgres
  ? 'postgresql://${dbAdminUser}:${uriComponent(dbAdminPassword)}@${dbServerName}.postgres.database.azure.com:5432/${dbName}?sslmode=require'
  : ''

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
        empty(ghcrToken) ? [] : [ { name: 'ghcr-token', value: ghcrToken } ],
        usePostgres ? [ { name: 'database-url', value: databaseUrl } ] : []
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
          env: concat([
            { name: 'GST_DATA_DIR', value: '/data' }
            { name: 'GST_SOURCE_WORKBOOK', value: '/data/master/workbook.xlsx' }
            { name: 'GST_BACKUP_DIR', value: '/data/backups' }
            // Backups matter on both engines - they also cover the workbooks
            // and the archived PDFs, which no database backup touches.
            { name: 'GST_BACKUP_MAX_AGE_MINUTES', value: '60' }
            { name: 'GST_FRONTEND_ORIGINS', value: 'https://${web.properties.defaultHostname}' }
            { name: 'GST_APPROVAL_MODE', value: 'flagged_only' }
            { name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-key' }
            { name: 'GST_EXTRACTION_PROVIDER', value: empty(anthropicApiKey) ? 'offline' : 'claude' }
          ],
          usePostgres
            ? [ { name: 'DATABASE_URL', secretRef: 'database-url' } ]
            // Required, not optional, when the database is a file on the
            // share: WAL does not work over SMB, and the failure is a
            // database that reads as corrupt rather than an error.
            : [ { name: 'GST_SQLITE_JOURNAL', value: 'DELETE' } ])
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
output apiName string = apiName
output engine string = database
output databaseHost string = usePostgres ? '${dbServerName}.postgres.database.azure.com' : 'sqlite on the file share'
