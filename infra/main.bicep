// ─────────────────────────────────────────────────────────────────────────────
// Hermes Agent Platform — Root Bicep Orchestrator
// Deploys all Azure resources in dependency order.
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

// ── Secrets (pass via parameters file or Key Vault reference) ────────────────
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

// ── Derived names ─────────────────────────────────────────────────────────────
var acrName = '${prefix}acr${environment}'
var sbNamespace = '${prefix}-sb-${environment}'
var logWorkspaceName = '${prefix}-logs-${environment}'
var appInsightsName = '${prefix}-ai-${environment}'
var containerEnvName = '${prefix}-env-${environment}'
var controlPlaneName = '${prefix}-control-plane-${environment}'
var hermesServiceName = '${prefix}-hermes-service-${environment}'
var apimName = '${prefix}-apim-${environment}'
var storageAccountName = '${replace(prefix, '-', '')}state${environment}'
var fileShareName = 'hermes-state'

// ── 1. Container Registry ─────────────────────────────────────────────────────
module acr 'container-registry.bicep' = {
  name: 'acr'
  params: {
    acrName: acrName
    location: location
  }
}

// ── 2. Service Bus ────────────────────────────────────────────────────────────
module serviceBus 'service-bus.bicep' = {
  name: 'serviceBus'
  params: {
    namespace: sbNamespace
    location: location
    tier: environment == 'prod' ? 'Standard' : 'Basic'
  }
}

// ── 3. Log Analytics + App Insights ──────────────────────────────────────────
module monitoring 'app-insights.bicep' = {
  name: 'monitoring'
  params: {
    logWorkspaceName: logWorkspaceName
    appInsightsName: appInsightsName
    location: location
    retentionDays: environment == 'prod' ? 90 : 30
  }
}

// ── 4. Azure Files (hermes-service SessionDB persistence) ────────────────────
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: environment == 'prod' ? 'Premium_LRS' : 'Standard_LRS'
  }
  kind: 'FileStorage'
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

// ── 5. Container Apps Environment ─────────────────────────────────────────────
resource containerEnv 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: containerEnvName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: monitoring.outputs.logWorkspaceId
        sharedKey: monitoring.outputs.logWorkspaceKey
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

// ── 6. control-plane Container App ───────────────────────────────────────────
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
      { name: 'MICROSOFT_APP_ID',                value: microsoftAppId }
      { name: 'MICROSOFT_APP_SECRET',            secretRef: 'microsoft-app-secret' }
      { name: 'MICROSOFT_APP_TENANT',            value: microsoftAppTenant }
      { name: 'SLACK_BOT_TOKEN',                 secretRef: 'slack-bot-token' }
      { name: 'SLACK_SIGNING_SECRET',            secretRef: 'slack-signing-secret' }
      { name: 'JIRA_BASE_URL',                   value: jiraBaseUrl }
      { name: 'JIRA_EMAIL',                      value: jiraEmail }
      { name: 'JIRA_API_TOKEN',                  secretRef: 'jira-api-token' }
      { name: 'JIRA_PROJECT_KEY',                value: jiraProjectKey }
      { name: 'AZURE_SERVICEBUS_CONNECTION_STRING', secretRef: 'sb-connection-string' }
      { name: 'AZURE_SERVICEBUS_QUEUE_NAME',     value: 'control-plane-queue' }
      { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'ai-connection-string' }
    ]
    secrets: [
      { name: 'microsoft-app-secret',  value: microsoftAppSecret }
      { name: 'slack-bot-token',        value: slackBotToken }
      { name: 'slack-signing-secret',   value: slackSigningSecret }
      { name: 'jira-api-token',         value: jiraApiToken }
      { name: 'sb-connection-string',   value: serviceBus.outputs.connectionString }
      { name: 'ai-connection-string',   value: monitoring.outputs.appInsightsConnectionString }
    ]
  }
}

// ── 7. hermes-service Container App ──────────────────────────────────────────
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
    sbNamespaceName: sbNamespace
    sbQueueName: 'control-plane-queue'
    stateVolumeName: 'hermes-state'
    env: [
      { name: 'OPENROUTER_API_KEY',              secretRef: 'openrouter-api-key' }
      { name: 'OPENROUTER_MODEL',                value: 'anthropic/claude-haiku-4-5' }
      { name: 'JIRA_BASE_URL',                   value: jiraBaseUrl }
      { name: 'JIRA_EMAIL',                      value: jiraEmail }
      { name: 'JIRA_API_TOKEN',                  secretRef: 'jira-api-token' }
      { name: 'JIRA_PROJECT_KEY',                value: jiraProjectKey }
      { name: 'CONFLUENCE_SPACE_KEY',            value: confluenceSpaceKey }
      { name: 'SLACK_BOT_TOKEN',                 secretRef: 'slack-bot-token' }
      { name: 'AZURE_SERVICEBUS_CONNECTION_STRING', secretRef: 'sb-connection-string' }
      { name: 'AZURE_SERVICEBUS_QUEUE_NAME',     value: 'control-plane-queue' }
      { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'ai-connection-string' }
      { name: 'HERMES_HOME',                     value: '/mnt/hermes-state' }
    ]
    secrets: [
      { name: 'openrouter-api-key',  value: openrouterApiKey }
      { name: 'slack-bot-token',      value: slackBotToken }
      { name: 'jira-api-token',       value: jiraApiToken }
      { name: 'sb-connection-string', value: serviceBus.outputs.connectionString }
      { name: 'ai-connection-string', value: monitoring.outputs.appInsightsConnectionString }
    ]
  }
}

// ── 8. API Management (Application Gateway for Slack + Teams) ────────────────
module apim 'api-management.bicep' = {
  name: 'apim'
  params: {
    apimName: apimName
    location: location
    tier: environment == 'prod' ? 'Standard' : 'Developer'
    controlPlaneUrl: controlPlane.outputs.fqdn
    appInsightsId: monitoring.outputs.appInsightsId
    appInsightsInstrumentationKey: monitoring.outputs.appInsightsInstrumentationKey
  }
}

// ── Outputs ───────────────────────────────────────────────────────────────────
output apimGatewayUrl string = apim.outputs.gatewayUrl
output controlPlaneFqdn string = controlPlane.outputs.fqdn
output acrLoginServer string = acr.outputs.loginServer
