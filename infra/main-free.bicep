// The no-cost deployment.
//
//   az deployment group create -g <group> -f infra/main-free.bicep \
//      -p prefix=iragst githubOwner=Glory17805 dbAdminPassword=<generated>
//
// Container Apps for both halves, each scaling to zero between sessions and
// staying inside the monthly free grant. GitHub Container Registry for the
// images, so there is no registry bill. Azure Files for the workbooks and the
// PDFs.
//
// The frontend is a container rather than the obvious Static Web App because
// that service generates its hostname at random and will not let you change
// it: a site resource named `iragst-web` answered on `zealous-tree-06f203000`.
// A Container App's hostname is built from its own name, so the address can
// say what the thing is. It costs a cold start on the first visit and a share
// of the same free grant the API uses.
//
// The database is a parameter, because which one is free depends on the
// subscription rather than on anything technical:
//
//   postgres  On an Azure free account, Flexible Server B1ms is free for the
//             first 12 months, which makes the right answer also the free one.
//             This is the default.
//
//   sqlite    On a pay-as-you-go subscription there is no free managed
//             database, so reaching zero means SQLite - but NOT on the share.
//             Azure Files does not honour the byte-range locks SQLite takes,
//             and the result is not slowness but failure: creating the schema
//             on an empty file dies with "database is locked". That was
//             verified here with a single replica and a freshly cleaned share,
//             so it is neither contention between processes nor a damaged
//             file, and Container Apps exposes no mount options to work
//             around it.
//
//             So the database sits on the container's own disk, which is a
//             real filesystem, and `dbsync` mirrors it to the share: a
//             consistent snapshot on a timer, after every post, and on
//             shutdown. The filings themselves never depend on it - openpyxl
//             writes the workbooks straight to the share - so what the mirror
//             protects is the accounts, the queue and the audit trail, and
//             the exposure is the minutes since the last snapshot.
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

@description('''
Public name of the frontend, and therefore the first label of the address
people type. Container Apps builds the hostname from the app's own name, which
is why the frontend is a container at all: Static Web Apps generates its
hostname at random and will not let you change it, so a site resource named
`iragst-web` answered on `zealous-tree-06f203000`.
''')
@minLength(3)
@maxLength(32)
param webAppName string = 'ira-invoice-automation'

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

// Both apps need the other's address - the API to allow its origin, the
// frontend to call it - which as direct references would be a cycle Bicep
// refuses. A Container App's hostname is its own name plus the environment's
// domain, so deriving both from the environment breaks the cycle without
// hardcoding anything.
var envDomain = environment.properties.defaultDomain
var apiFqdn = '${apiName}.${envDomain}'
var webFqdn = '${webAppName}.${envDomain}'

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
          allowedOrigins: [ 'https://${webFqdn}' ]
          allowedMethods: [ 'GET', 'POST', 'PATCH', 'PUT', 'DELETE', 'OPTIONS' ]
          allowedHeaders: [ '*' ]
        }
      }
      // Every secret declared here must carry a value: Container Apps rejects
      // an empty one outright rather than treating it as unset. So a secret
      // exists only when there is something to put in it, and the matching
      // environment variable is added on the same condition below.
      secrets: concat(
        empty(anthropicApiKey) ? [] : [ { name: 'anthropic-key', value: anthropicApiKey } ],
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
            { name: 'GST_FRONTEND_ORIGINS', value: 'https://${webFqdn}' }
            { name: 'GST_APPROVAL_MODE', value: 'flagged_only' }
            { name: 'GST_EXTRACTION_PROVIDER', value: empty(anthropicApiKey) ? 'offline' : 'claude' }
          ],
          empty(anthropicApiKey)
            ? []
            : [ { name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-key' } ],
          usePostgres
            ? [ { name: 'DATABASE_URL', secretRef: 'database-url' } ]
            : [
                // SQLite cannot live on the share. Azure Files does not honour
                // the byte-range locks it takes, so even creating the schema on
                // an empty file fails with "database is locked" - verified here
                // with a single replica and a clean share, so it is neither
                // lock contention nor a damaged file. Container Apps exposes no
                // mount options, so nobrl is not available.
                //
                // The database therefore lives on the container's own disk and
                // is mirrored to the share: a consistent snapshot on a timer,
                // after every post, and on shutdown. The filings themselves are
                // never at risk - openpyxl writes the workbooks straight to the
                // share - so what the mirror protects is the accounts, the
                // queue and the audit trail.
                { name: 'GST_SQLITE_PATH', value: '/var/gstdb/store.db' }
                { name: 'GST_DB_MIRROR', value: '/data/store.db' }
                { name: 'GST_DB_MIRROR_INTERVAL', value: '120' }
                // WAL is fine now the file is on a real filesystem.
                { name: 'GST_SQLITE_JOURNAL', value: 'WAL' }
              ])
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

resource web 'Microsoft.App/containerApps@2024-03-01' = {
  name: webAppName
  location: location
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 3000
        transport: 'auto'
        allowInsecure: false
      }
      secrets: empty(ghcrToken) ? [] : [ { name: 'ghcr-token', value: ghcrToken } ]
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
          name: 'web'
          image: '${imageBase}/gst-web:latest'
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            // server.py bakes this into /config.js and into the page's
            // connect-src at container start, so pointing the frontend at a
            // different API is a restart rather than a rebuild. The browser
            // resolves it, so it is the API's public address.
            { name: 'GST_API_BASE', value: 'https://${apiFqdn}' }
          ]
        }
      ]
      scale: {
        // Same shape as the API: nothing running, nothing billed, and one
        // replica when someone is here. The frontend has no single-writer
        // constraint of its own - the ceiling is only to stay inside the
        // shared free grant.
        minReplicas: 0
        maxReplicas: 1
      }
    }
  }
}

output apiUrl string = 'https://${api.properties.configuration.ingress.fqdn}'
output webUrl string = 'https://${web.properties.configuration.ingress.fqdn}'
output webName string = web.name
output storageAccount string = storage.name
output shareName string = shareName
output imageBase string = imageBase
output apiName string = apiName
output engine string = database
output databaseHost string = usePostgres ? '${dbServerName}.postgres.database.azure.com' : 'sqlite on the file share'
