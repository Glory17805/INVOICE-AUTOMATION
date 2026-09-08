// Everything the deployment needs, in one file.
//
//   az deployment group create -g <group> -f infra/main.bicep -p @infra/params.json
//
// Written as Bicep rather than clicked together in the portal so the
// environment can be recreated, reviewed, and diffed. Running it twice is
// safe: ARM reconciles to this description rather than adding a second copy.

@description('Short name used as the prefix for every resource. Lowercase letters and digits.')
@minLength(3)
@maxLength(11)
param prefix string = 'iragst'

@description('Region. Central India keeps Indian tax records in-country.')
param location string = 'centralindia'

@description('Administrator login for PostgreSQL.')
param dbAdminUser string = 'gstadmin'

@description('Administrator password for PostgreSQL. Supply at deploy time; never commit it.')
@secure()
param dbAdminPassword string

@description('Claude API key. Leave empty to deploy without one - the offline reader still works.')
@secure()
param anthropicApiKey string = ''

var registryName = '${prefix}registry'
var storageName = '${prefix}storage'
var shareName = 'data'
var dbServerName = '${prefix}-db'
var dbName = 'gst'
var planName = '${prefix}-plan'
var apiName = '${prefix}-api'
var webName = '${prefix}-web'

// --------------------------------------------------------------------------
// Container registry
// --------------------------------------------------------------------------

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  sku: { name: 'Basic' }
  properties: {
    // The web apps authenticate with the registry's admin user. A managed
    // identity would be better and needs an AcrPull role assignment, which
    // requires permissions on the subscription that a first deployment often
    // does not have. Revisit once the deployment is running.
    adminUserEnabled: true
  }
}

// --------------------------------------------------------------------------
// Storage: the workbooks, the incoming PDFs, the archive
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
    // Invoices and filed registers. Seven days of soft delete is the cheapest
    // insurance against someone clearing the wrong directory.
    shareDeleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: shareName
  properties: {
    accessTier: 'TransactionOptimized'
    shareQuota: 100
  }
}

// --------------------------------------------------------------------------
// PostgreSQL
// --------------------------------------------------------------------------

resource database 'Microsoft.DBforPostgreSQL/flexibleServers@2023-06-01-preview' = {
  name: dbServerName
  location: location
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: dbAdminUser
    administratorLoginPassword: dbAdminPassword
    storage: { storageSizeGB: 32 }
    backup: {
      // Seven days of point-in-time restore. This holds the audit trail an
      // auditor may ask about, so the default is kept rather than reduced.
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: { mode: 'Disabled' }
    network: { publicNetworkAccess: 'Enabled' }
  }
}

resource gstDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-06-01-preview' = {
  parent: database
  name: dbName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// citext is not available until it is allowlisted on the server. The app's
// schema declares users.email as CITEXT to keep case-insensitive login
// identical to SQLite, so without this the very first request fails with
// 'type "citext" does not exist'.
resource allowCitext 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2023-06-01-preview' = {
  parent: database
  name: 'azure.extensions'
  properties: {
    value: 'CITEXT'
    source: 'user-override'
  }
}

// The API reaches Postgres over the public endpoint, restricted to Azure
// services. Tightening this to the web apps' outbound addresses, or moving to
// a private endpoint, is the next hardening step once the deployment is up.
resource allowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-06-01-preview' = {
  parent: database
  name: 'allow-azure-services'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// --------------------------------------------------------------------------
// Compute
// --------------------------------------------------------------------------

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: planName
  location: location
  sku: {
    name: 'B1'
    tier: 'Basic'
  }
  kind: 'linux'
  properties: { reserved: true }
}

var databaseUrl = 'postgresql://${dbAdminUser}:${uriComponent(dbAdminPassword)}@${database.properties.fullyQualifiedDomainName}:5432/${dbName}?sslmode=require'
var webOrigin = 'https://${webName}.azurewebsites.net'
var apiOrigin = 'https://${apiName}.azurewebsites.net'

resource api 'Microsoft.Web/sites@2023-12-01' = {
  name: apiName
  location: location
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'DOCKER|${registry.properties.loginServer}/gst-api:latest'
      // Never more than one. workbook.py serialises writes with an in-process
      // lock, and singleton.py takes a kernel lock to make a second process
      // fail loudly rather than silently overwrite a posted tax row.
      alwaysOn: true
      numberOfWorkers: 1
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      healthCheckPath: '/api/health'
      appSettings: [
        { name: 'WEBSITES_PORT', value: '8000' }
        { name: 'DOCKER_REGISTRY_SERVER_URL', value: 'https://${registry.properties.loginServer}' }
        { name: 'DOCKER_REGISTRY_SERVER_USERNAME', value: registry.listCredentials().username }
        { name: 'DOCKER_REGISTRY_SERVER_PASSWORD', value: registry.listCredentials().passwords[0].value }
        { name: 'DATABASE_URL', value: databaseUrl }
        { name: 'GST_DATA_DIR', value: '/data' }
        { name: 'GST_SOURCE_WORKBOOK', value: '/data/master/workbook.xlsx' }
        { name: 'GST_FRONTEND_ORIGINS', value: webOrigin }
        { name: 'GST_APPROVAL_MODE', value: 'flagged_only' }
        { name: 'ANTHROPIC_API_KEY', value: anthropicApiKey }
        { name: 'GST_EXTRACTION_PROVIDER', value: empty(anthropicApiKey) ? 'offline' : 'claude' }
      ]
    }
  }
}

// The share, mounted where the app expects its data directory. Everything
// durable that is not in Postgres lives here: the period workbooks, the
// incoming PDFs, the archive, and the master workbook under master/.
resource apiStorage 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: api
  name: 'azurestorageaccounts'
  properties: {
    data: {
      type: 'AzureFiles'
      accountName: storage.name
      shareName: shareName
      mountPath: '/data'
      accessKey: storage.listKeys().keys[0].value
    }
  }
}

resource web 'Microsoft.Web/sites@2023-12-01' = {
  name: webName
  location: location
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'DOCKER|${registry.properties.loginServer}/gst-web:latest'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'WEBSITES_PORT', value: '3000' }
        { name: 'DOCKER_REGISTRY_SERVER_URL', value: 'https://${registry.properties.loginServer}' }
        { name: 'DOCKER_REGISTRY_SERVER_USERNAME', value: registry.listCredentials().username }
        { name: 'DOCKER_REGISTRY_SERVER_PASSWORD', value: registry.listCredentials().passwords[0].value }
        // The browser resolves this, so it is the API's public address rather
        // than anything internal to the plan.
        { name: 'GST_API_BASE', value: apiOrigin }
      ]
    }
  }
}

output registryLoginServer string = registry.properties.loginServer
output registryName string = registry.name
output storageAccount string = storage.name
output shareName string = shareName
output apiUrl string = apiOrigin
output webUrl string = webOrigin
output databaseHost string = database.properties.fullyQualifiedDomainName
