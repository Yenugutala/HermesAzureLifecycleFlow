// Production parameter overrides
// Usage:
//   az deployment group create \
//     --resource-group hermes-rg \
//     --template-file infra/main.bicep \
//     --parameters infra/params/prod.bicepparam

using '../main.bicep'

param environment = 'prod'
param prefix = 'hermes'

// Secrets — load from environment variables injected by GitHub Actions secrets
// or Azure Key Vault references. Never hardcode real values here.
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
