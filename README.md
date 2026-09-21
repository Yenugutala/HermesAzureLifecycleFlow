# Hermes Agent Platform

An AI-powered Slack and Teams assistant that automates Jira and Confluence workflows — listing tickets, fetching details, and breaking Business Requirements Documents (BRDs) into full Jira epic hierarchies.

Built on [Nous Research Hermes Agent](https://hermes-agent.nousresearch.com/) with Azure Container Apps, Service Bus, and API Management.

```
Slack / Teams
      │
      ▼
Azure API Management  ←── TLS, HMAC-SHA256, rate limiting, X-Correlation-ID
      │
      ▼
control-plane  (Azure Container App, FastAPI, port 3978)
  Resolves user → role → capabilities → enqueues to Service Bus
      │
      ▼
Azure Service Bus  (control-plane-queue)
      │
      ▼
hermes-service  (Azure Container App, queue consumer)
  Hermes AIAgent: Jira + Confluence + web search + delegation + skills
      │
      ▼
OpenRouter → Claude Haiku-4-5  +  App Insights telemetry
      │
      ▼
Slack / Teams reply
```

## Quick Links

| Doc | Purpose |
|-----|---------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Full production flow, RBAC, agent internals, telemetry |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | First-time setup, CI/CD, rollback |
| [docs/DEV-VS-PROD.md](docs/DEV-VS-PROD.md) | What differs between local dev and prod |
| [docs/ADD-NEW-AGENT.md](docs/ADD-NEW-AGENT.md) | 5-step guide to add a new capability |
| [docs/COMPONENTS.md](docs/COMPONENTS.md) | Every component, its role, and dependencies |

## Prerequisites

- Azure subscription with Owner or Contributor + User Access Administrator
- Azure CLI `>= 2.57`
- Docker Desktop (for local builds)
- Python 3.11+
- GitHub repository (for CI/CD)

## Local Development (5 steps)

```bash
# 1. Clone
git clone https://github.com/<your-org>/hermes-platform.git
cd hermes-platform

# 2. Create virtual environment
python3.11 -m venv .venv311 && source .venv311/bin/activate

# 3. Install dependencies (both services)
pip install -r control-plane/requirements.txt
pip install -r hermes-service/requirements.txt

# 4. Copy and fill in secrets
cp .env.example .env
# Edit .env with your credentials

# 5. Run locally
# Terminal 1 — control plane:
cd control-plane && uvicorn main:app --port 3978 --reload

# Terminal 2 — hermes service:
cd hermes-service && python main.py

# Expose control-plane to Slack/Teams (for local testing):
ngrok http 3978
# Set Slack Event Subscriptions URL to: https://<ngrok-id>.ngrok.io/slack/events
```

## First-time Azure Deployment

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for full details. Quick version:

```bash
# 1. Create resource group
az group create --name hermes-rg --location eastus

# 2. Deploy all infrastructure
az deployment group create \
  --resource-group hermes-rg \
  --template-file infra/main.bicep \
  --parameters infra/params/prod.bicepparam

# 3. Add GitHub secrets for CI/CD
#    AZURE_CREDENTIALS  (service principal JSON from: az ad sp create-for-rbac)

# 4. Push to main → GitHub Actions builds + deploys automatically
git push origin main
```

## RBAC Roles

| Role | Capabilities |
|------|-------------|
| admin | All capabilities |
| engineer | Jira read/write, BRD breakdown, deploy pipeline |
| ba (Business Analyst) | Jira read/write, BRD breakdown |
| product_owner | Jira read/write |
| viewer | Jira read only |

## Environment Variables

See `.env.example` for the full list. Key variables:

| Variable | Where it comes from |
|----------|-------------------|
| `OPENROUTER_API_KEY` | [openrouter.ai](https://openrouter.ai) |
| `SLACK_BOT_TOKEN` | Slack app settings |
| `SLACK_SIGNING_SECRET` | Slack app settings |
| `JIRA_API_TOKEN` | Atlassian account settings |
| `AZURE_SERVICEBUS_CONNECTION_STRING` | Azure Portal → Service Bus → Shared access policies |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Azure Portal → Application Insights |
