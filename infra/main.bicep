// ─────────────────────────────────────────────────────────────────────────────
// Hermes Agent Platform — Root Bicep Orchestrator
//
// Assumes the following already exist in your subscription:
//   - Azure Service Bus + queue
//   - Azure Application Insights
//   - Azure API Management
//
// Creates only what is missing:
//   - Azure Container Registry (ACR)
//   - Azure Storage Account + File Share  (SessionDB persistence)
//   - Container Apps Environment
//   - control-plane Container App
//   - hermes-service Container App
//
// Deploy:
//   az deployment group create \
//     --resource-group hermes-rg \
//     --template-file infra/main.bicep \
//     --parameters infra/params/prod.bicepparam
// ─────────────────────────────────────────────────────────────────────────────

@description('Environment name (dev | prod)')
param environment string = 'prod'

@description('Azure region for all resources')
param location string = resourceGroup().location

@description('Name prefix for all resources')
param prefix string = 'hermes'

// ── Existing resource connection strings ──────────────────────────────────────
@secure()
@description('Primary connection string of your existing Service Bus namespace')
param serviceBusConnectionString string

@description('Queue name in your existing Service Bus namespace')
param serviceBusQueueName string = 'control-plane-queue'

@secure()
@description('Connection string from your existing Application Insights resource')
param appInsightsConnectionString string

@description('Log Analytics workspace customer ID (from existing workspace linked to App Insights)')
param logWorkspaceCustomerId string

@secure()
@description('Log Analytics workspace shared key')
param logWorkspaceKey string

// ── Application secrets ───────────────────────────────────────────────────────
@secure()
param openrouterApiKey string

@secure()
param slackBotToken string

@secure()
param slackSigningSecret string

@secure()
param jiraApiToken string

param jiraBaseUrl string
param jiraEmail string
param jiraProjectKey string
param confluenceSpaceKey string

@secure()
param microsoftAppId string

@secure()
param microsoftAppSecret string

param microsoftAppTenant string

// ── Derived resource names ─────────────────────────────────────────────────────
var acrName            = '${replace(prefix, '-', '')}acr${environment}'
var storageAccountName = '${replace(prefix, '-', '')}state${environment}'
var fileShareName      = 'hermes-state'
var containerEnvName   = '${prefix}-env-${environment}'
var controlPlaneName   = '${prefix}-control-plane-${environment}'
var hermesServiceName  = '${prefix}-hermes-service-${environment}'

// ── 1. Container Registry ─────────────────────────────────────────────────────
module acr 'container-registry.bicep' = {
  name: 'acr'
  params: {
    acrName: acrName
    location: location
  }
}

// ── 2. Azure Files (hermes-service SessionDB persistence) ─────────────────────
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource fileShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-01-01' = {
  name: '${storageAccount.name}/default/${fileShareName}'
  properties: {
    shareQuota: 5
  }
}

// ── 3. Container Apps Environment ─────────────────────────────────────────────
resource containerEnv 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: containerEnvName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logWorkspaceCustomerId
        sharedKey: logWorkspaceKey
      }
    }
  }
}

// Azure Files storage link for SessionDB volume
resource storageLink 'Microsoft.App/managedEnvironments/storages@2023-05-01' = {
  name: 'hermes-state'
  parent: containerEnv
  properties: {
    azureFile: {
      accountName: storageAccount.name
      accountKey: storageAccount.listKeys().keys[0].value
      shareName: fileShareName
      accessMode: 'ReadWrite'
    }
  }
}

// ── 4. control-plane Container App ────────────────────────────────────────────
module controlPlane 'container-apps.bicep' = {
  name: 'controlPlane'
  params: {
    appName: controlPlaneName
    location: location
    containerEnvId: containerEnv.id
    acrLoginServer: acr.outputs.loginServer
    acrAdminUsername: acr.outputs.adminUsername
    acrAdminPassword: acr.outputs.adminPassword
    imageName: 'control-plane'
    imageTag: 'latest'
    minReplicas: environment == 'prod' ? 1 : 0
    maxReplicas: environment == 'prod' ? 3 : 1
    httpPort: 3978
    isQueueConsumer: false
    env: [
      { name: 'MICROSOFT_APP_ID',                   value: microsoftAppId }
      { name: 'MICROSOFT_APP_SECRET',               secretRef: 'microsoft-app-secret' }
      { name: 'MICROSOFT_APP_TENANT',               value: microsoftAppTenant }
      { name: 'SLACK_BOT_TOKEN',                    secretRef: 'slack-bot-token' }
      { name: 'SLACK_SIGNING_SECRET',               secretRef: 'slack-signing-secret' }
      { name: 'JIRA_BASE_URL',                      value: jiraBaseUrl }
      { name: 'JIRA_EMAIL',                         value: jiraEmail }
      { name: 'JIRA_API_TOKEN',                     secretRef: 'jira-api-token' }
      { name: 'JIRA_PROJECT_KEY',                   value: jiraProjectKey }
      { name: 'AZURE_SERVICEBUS_CONNECTION_STRING', secretRef: 'sb-connection-string' }
      { name: 'AZURE_SERVICEBUS_QUEUE_NAME',        value: serviceBusQueueName }
      { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'ai-connection-string' }
    ]
    secrets: [
      { name: 'microsoft-app-secret', value: microsoftAppSecret }
      { name: 'slack-bot-token',       value: slackBotToken }
      { name: 'slack-signing-secret',  value: slackSigningSecret }
      { name: 'jira-api-token',        value: jiraApiToken }
      { name: 'sb-connection-string',  value: serviceBusConnectionString }
      { name: 'ai-connection-string',  value: appInsightsConnectionString }
    ]
  }
}

// ── 5. hermes-service Container App ──────────────────────────────────────────
module hermesService 'container-apps.bicep' = {
  name: 'hermesService'
  params: {
    appName: hermesServiceName
    location: location
    containerEnvId: containerEnv.id
    acrLoginServer: acr.outputs.loginServer
    acrAdminUsername: acr.outputs.adminUsername
    acrAdminPassword: acr.outputs.adminPassword
    imageName: 'hermes-service'
    imageTag: 'latest'
    minReplicas: 0
    maxReplicas: environment == 'prod' ? 5 : 2
    httpPort: 0
    isQueueConsumer: true
    sbNamespaceName: split(split(serviceBusConnectionString, 'Endpoint=sb://')[1], '.servicebus.windows.net')[0]
    sbQueueName: serviceBusQueueName
    stateVolumeName: 'hermes-state'
    env: [
      { name: 'OPENROUTER_API_KEY',                 secretRef: 'openrouter-api-key' }
      { name: 'OPENROUTER_MODEL',                   value: 'anthropic/claude-haiku-4-5' }
      { name: 'JIRA_BASE_URL',                      value: jiraBaseUrl }
      { name: 'JIRA_EMAIL',                         value: jiraEmail }
      { name: 'JIRA_API_TOKEN',                     secretRef: 'jira-api-token' }
      { name: 'JIRA_PROJECT_KEY',                   value: jiraProjectKey }
      { name: 'CONFLUENCE_SPACE_KEY',               value: confluenceSpaceKey }
      { name: 'SLACK_BOT_TOKEN',                    secretRef: 'slack-bot-token' }
      { name: 'AZURE_SERVICEBUS_CONNECTION_STRING', secretRef: 'sb-connection-string' }
      { name: 'AZURE_SERVICEBUS_QUEUE_NAME',        value: serviceBusQueueName }
      { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'ai-connection-string' }
      { name: 'HERMES_HOME',                        value: '/mnt/hermes-state' }
    ]
    secrets: [
      { name: 'openrouter-api-key',  value: openrouterApiKey }
      { name: 'slack-bot-token',      value: slackBotToken }
      { name: 'jira-api-token',       value: jiraApiToken }
      { name: 'sb-connection-string', value: serviceBusConnectionString }
      { name: 'ai-connection-string', value: appInsightsConnectionString }
    ]
  }
}

// ── Outputs ───────────────────────────────────────────────────────────────────
output controlPlaneFqdn string = controlPlane.outputs.fqdn
output acrLoginServer string = acr.outputs.loginServer
