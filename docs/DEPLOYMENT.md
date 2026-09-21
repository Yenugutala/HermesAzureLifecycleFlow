# Deployment Guide

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Azure CLI | >= 2.57 | `brew install azure-cli` |
| Docker Desktop | latest | [docker.com](https://www.docker.com) |
| Python | 3.11+ | `brew install python@3.11` |
| GitHub account | — | [github.com](https://github.com) |

## First-Time Infrastructure Setup

### 1. Login to Azure

```bash
az login
az account set --subscription <YOUR_SUBSCRIPTION_ID>
```

### 2. Create Resource Group

```bash
az group create --name hermes-rg --location eastus
```

### 3. Deploy all Azure resources (Bicep)

Export your secrets as environment variables first — the param files read them:

```bash
export OPENROUTER_API_KEY=sk-or-...
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_SIGNING_SECRET=...
export JIRA_API_TOKEN=...
export JIRA_BASE_URL=https://yourorg.atlassian.net
export JIRA_EMAIL=your@email.com
export JIRA_PROJECT_KEY=SCRUM
export CONFLUENCE_SPACE_KEY=~YourSpace
export MICROSOFT_APP_ID=...
export MICROSOFT_APP_SECRET=...
export MICROSOFT_APP_TENANT=...

az deployment group create \
  --resource-group hermes-rg \
  --template-file infra/main.bicep \
  --parameters infra/params/prod.bicepparam
```

Note the outputs — you'll need:
- `apimGatewayUrl` → configure as Slack Event Subscriptions URL: `<apimGatewayUrl>/slack/events`
- `acrLoginServer` → used by CI/CD

### 4. Set up GitHub Actions secrets

```bash
# Create service principal for CI/CD
az ad sp create-for-rbac \
  --name hermes-github-actions \
  --role Contributor \
  --scopes /subscriptions/<SUB_ID>/resourceGroups/hermes-rg \
  --sdk-auth
```

Add the output JSON as GitHub secret `AZURE_CREDENTIALS` in your repository:
`Settings → Secrets and variables → Actions → New repository secret`

### 5. Configure Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → your app
2. **Event Subscriptions** → Request URL: `<apimGatewayUrl>/slack/events`
3. **OAuth & Permissions** → Bot Token Scopes: `chat:write`, `users:info`, `channels:read`
4. Reinstall the app to your workspace

### 6. Configure Teams (optional)

1. Azure Bot Service → Messaging endpoint: `<control-plane-fqdn>/api/messages`
2. Update `teams-manifest/manifest.json` with your production APIM URL
3. Upload manifest to Teams Admin Center

---

## CI/CD — How It Works

Every push to `main` triggers `.github/workflows/deploy.yml`:

```
push to main
      │
      ▼
build-and-push job
  ├── docker build control-plane → ACR
  └── docker build hermes-service → ACR
      │
      ▼
deploy job (requires: build-and-push)
  ├── az containerapp update control-plane → new image tag
  ├── az containerapp update hermes-service → new image tag
  └── verify active revisions
```

The image is tagged with `github.sha` (commit hash) for traceability.
Both `:latest` and `:<sha>` tags are pushed so rollback is easy.

---

## Manual Deploy (skip CI/CD)

```bash
# Login to ACR
az acr login --name hermesacr

# Build and push
docker build -t hermesacr.azurecr.io/control-plane:manual ./control-plane
docker push hermesacr.azurecr.io/control-plane:manual

docker build -t hermesacr.azurecr.io/hermes-service:manual ./hermes-service
docker push hermesacr.azurecr.io/hermes-service:manual

# Deploy
az containerapp update \
  --name hermes-control-plane \
  --resource-group hermes-rg \
  --image hermesacr.azurecr.io/control-plane:manual

az containerapp update \
  --name hermes-hermes-service \
  --resource-group hermes-rg \
  --image hermesacr.azurecr.io/hermes-service:manual
```

---

## Rollback

```bash
# List revisions for a Container App
az containerapp revision list \
  --name hermes-control-plane \
  --resource-group hermes-rg \
  --query "[].{name:name, active:properties.active, created:properties.createdTime}" \
  --output table

# Activate a previous revision
az containerapp revision activate \
  --name hermes-control-plane \
  --resource-group hermes-rg \
  --revision <REVISION_NAME>

# Deactivate the bad revision
az containerapp revision deactivate \
  --name hermes-control-plane \
  --resource-group hermes-rg \
  --revision <BAD_REVISION_NAME>
```

---

## Monitoring

### View live logs

```bash
# control-plane logs
az containerapp logs show \
  --name hermes-control-plane \
  --resource-group hermes-rg \
  --follow

# hermes-service logs
az containerapp logs show \
  --name hermes-hermes-service \
  --resource-group hermes-rg \
  --follow
```

### App Insights — trace a request end-to-end

```kusto
union customEvents, traces, exceptions
| where customDimensions.correlation_id == "YOUR-UUID"
| project timestamp, itemType, message, customDimensions
| order by timestamp asc
```

### App Insights — token costs by user

```kusto
customEvents
| where name == "HERMES_FINOPS"
| project timestamp,
    user = tostring(customDimensions.user_email),
    cost = todouble(customDimensions.llm_cost_usd),
    tokens_in = toint(customDimensions.input_tokens),
    tokens_out = toint(customDimensions.output_tokens)
| summarize total_cost=sum(cost), total_calls=count() by user
| order by total_cost desc
```

---

## Environment Variables Reference

| Variable | Service | Required | Description |
|----------|---------|----------|-------------|
| `OPENROUTER_API_KEY` | hermes-service | Yes | OpenRouter API key for LLM calls |
| `OPENROUTER_MODEL` | hermes-service | No | Default: `anthropic/claude-haiku-4-5` |
| `SLACK_BOT_TOKEN` | both | Yes | `xoxb-...` from Slack app OAuth |
| `SLACK_SIGNING_SECRET` | control-plane | Yes | Slack app signing secret |
| `JIRA_BASE_URL` | both | Yes | e.g. `https://yourorg.atlassian.net` |
| `JIRA_EMAIL` | both | Yes | Atlassian account email |
| `JIRA_API_TOKEN` | both | Yes | Atlassian API token |
| `JIRA_PROJECT_KEY` | both | Yes | e.g. `SCRUM` |
| `CONFLUENCE_SPACE_KEY` | hermes-service | Yes | Confluence space key |
| `MICROSOFT_APP_ID` | control-plane | Yes | Azure AD App ID for Teams |
| `MICROSOFT_APP_SECRET` | control-plane | Yes | Azure AD App secret for Teams |
| `MICROSOFT_APP_TENANT` | control-plane | Yes | Azure AD tenant ID |
| `AZURE_SERVICEBUS_CONNECTION_STRING` | both | Yes | Service Bus primary connection string |
| `AZURE_SERVICEBUS_QUEUE_NAME` | both | No | Default: `control-plane-queue` |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | both | No | App Insights (telemetry disabled if unset) |
| `HERMES_HOME` | hermes-service | No | Default: `~/.hermes`; set to `/mnt/hermes-state` in prod |
