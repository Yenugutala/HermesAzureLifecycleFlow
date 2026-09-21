// Development parameter overrides
// Usage:
//   az deployment group create \
//     --resource-group hermes-rg-dev \
//     --template-file infra/main.bicep \
//     --parameters infra/params/dev.bicepparam

using '../main.bicep'

param environment = 'dev'
param prefix = 'hermes'

// Secrets — load from Azure Key Vault or CI/CD variable group in real deployments.
// For local testing you can supply these directly, but never commit real values.
param openrouterApiKey       = readEnvironmentVariable('OPENROUTER_API_KEY')
param slackBotToken          = readEnvironmentVariable('SLACK_BOT_TOKEN')
param slackSigningSecret     = readEnvironmentVariable('SLACK_SIGNING_SECRET')
param jiraApiToken           = readEnvironmentVariable('JIRA_API_TOKEN')
param jiraBaseUrl            = readEnvironmentVariable('JIRA_BASE_URL')
param jiraEmail              = readEnvironmentVariable('JIRA_EMAIL')
param jiraProjectKey         = readEnvironmentVariable('JIRA_PROJECT_KEY')
param confluenceSpaceKey     = readEnvironmentVariable('CONFLUENCE_SPACE_KEY')
param microsoftAppId         = readEnvironmentVariable('MICROSOFT_APP_ID')
param microsoftAppSecret     = readEnvironmentVariable('MICROSOFT_APP_SECRET')
param microsoftAppTenant     = readEnvironmentVariable('MICROSOFT_APP_TENANT')
