// Production parameters
// Usage:
//   az deployment group create \
//     --resource-group hermes-rg \
//     --template-file infra/main.bicep \
//     --parameters infra/params/prod.bicepparam

using '../main.bicep'

param environment = 'prod'
param prefix = 'hermes'

// ── Existing Azure resources (already provisioned) ────────────────────────────
// Service Bus → Azure Portal → your namespace → Shared access policies → RootManageSharedAccessKey → Primary connection string
param serviceBusConnectionString  = readEnvironmentVariable('AZURE_SERVICEBUS_CONNECTION_STRING')
param serviceBusQueueName         = readEnvironmentVariable('AZURE_SERVICEBUS_QUEUE_NAME', 'control-plane-queue')

// App Insights → Azure Portal → your App Insights → Overview → Connection String
param appInsightsConnectionString = readEnvironmentVariable('APPLICATIONINSIGHTS_CONNECTION_STRING')

// Log Analytics → Azure Portal → your Log Analytics workspace → Agents → Workspace ID + Primary key
param logWorkspaceCustomerId      = readEnvironmentVariable('LOG_WORKSPACE_CUSTOMER_ID')
param logWorkspaceKey             = readEnvironmentVariable('LOG_WORKSPACE_KEY')

// ── Application secrets ───────────────────────────────────────────────────────
param openrouterApiKey    = readEnvironmentVariable('OPENROUTER_API_KEY')
param slackBotToken       = readEnvironmentVariable('SLACK_BOT_TOKEN')
param slackSigningSecret  = readEnvironmentVariable('SLACK_SIGNING_SECRET')
param jiraApiToken        = readEnvironmentVariable('JIRA_API_TOKEN')
param jiraBaseUrl         = readEnvironmentVariable('JIRA_BASE_URL')
param jiraEmail           = readEnvironmentVariable('JIRA_EMAIL')
param jiraProjectKey      = readEnvironmentVariable('JIRA_PROJECT_KEY')
param confluenceSpaceKey  = readEnvironmentVariable('CONFLUENCE_SPACE_KEY')
param microsoftAppId      = readEnvironmentVariable('MICROSOFT_APP_ID')
param microsoftAppSecret  = readEnvironmentVariable('MICROSOFT_APP_SECRET')
param microsoftAppTenant  = readEnvironmentVariable('MICROSOFT_APP_TENANT')
