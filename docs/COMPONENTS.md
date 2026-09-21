# Components

Every component in the Hermes platform — what it does, how it scales, and what it depends on.

## Azure Resources

| Component | Azure Resource | Port / Trigger | Scales On | Depends On |
|-----------|---------------|----------------|-----------|------------|
| API Management | `Microsoft.ApiManagement/service` | HTTPS (443) | Capacity units | control-plane FQDN |
| control-plane | Container App | HTTP (3978) | HTTP requests (concurrentRequests=20) | Service Bus, Slack API, Jira API |
| hermes-service | Container App | Service Bus queue | Queue depth (1 replica per 5 messages) | Service Bus, OpenRouter, Jira API, Confluence API, Slack API |
| Service Bus | `Microsoft.ServiceBus/namespaces` | AMQP | N/A (managed) | — |
| Container Registry | `Microsoft.ContainerRegistry/registries` | HTTPS | N/A (managed) | — |
| Application Insights | `Microsoft.Insights/components` | SDK push | N/A (managed) | Log Analytics |
| Log Analytics | `Microsoft.OperationalInsights/workspaces` | SDK push | N/A (managed) | — |
| Azure Files (Storage) | `Microsoft.Storage/storageAccounts` | NFS mount | N/A (managed) | — |
| Container Apps Environment | `Microsoft.App/managedEnvironments` | — | N/A (shared env) | Log Analytics |

## Application Services

| Service | Language | Entry Point | Config Files | Key Libraries |
|---------|----------|-------------|--------------|---------------|
| control-plane | Python 3.11 | `uvicorn main:app` | `.env` | FastAPI, botbuilder-core, httpx, azure-servicebus |
| hermes-service | Python 3.11 | `python main.py` | `.env`, `hermes.yaml`, `~/.hermes/SOUL.md` | hermes-agent, azure-servicebus, slack-sdk, opencensus-ext-azure |

## Hermes Agent Components

| Component | File | Loaded By | Purpose |
|-----------|------|-----------|---------|
| Agent config | `hermes.yaml` | AIAgent constructor | Plugins, toolsets, model, fallback chain |
| Identity | `~/.hermes/SOUL.md` | Hermes automatically | Agent persona loaded every turn |
| Conversation DB | `~/.hermes/state.db` | `SessionDB()` | SQLite — persists messages across all sessions |
| Plugin: Jira | `plugins/jira_plugin.py` | hermes.yaml | list_tickets, get_ticket, create_epic, create_story |
| Plugin: Story | `plugins/story_plugin.py` | hermes.yaml | break_brd_into_stories |
| Plugin: Confluence | `plugins/confluence_plugin.py` | hermes.yaml | get_page, find_relevant |
| Plugin: Search | `plugins/search_plugin.py` | hermes.yaml | confluence_find_relevant (vector similarity) |
| Skill: BRD breakdown | `~/.hermes/skills/brd-breakdown/SKILL.md` | `skill_manage` tool | 6-step Confluence → Jira hierarchy procedure |
| Skill: Jira assistant | `~/.hermes/skills/jira-assistant/SKILL.md` | `skill_manage` tool | Quick-reference for Jira ops |
| Vector store | `vector_store.py` | `main.py` startup | numpy cosine similarity for Confluence page search |
| Index builder | `scripts/build_index.py` | Manual / offline | Builds vector index from Confluence pages |
| Telemetry | `telemetry.py` | `route_message()` | FinOps + App Insights traces |

## Platform Toolsets (always-on)

These toolsets are always enabled regardless of user capabilities:

| Toolset | Tool(s) | Description |
|---------|---------|-------------|
| `delegation` | `delegate_task` | Spawn up to 3 parallel child agents |
| `web` | `web_search`, `web_extract` | DuckDuckGo search — no API key needed |
| `skills` | `skill_manage` | Load/create procedural skills from ~/.hermes/skills/ |
| `session_search` | `session_search` | FTS5 full-text search across past conversations in state.db |

## RBAC-Gated Toolsets

| Capability | Roles | Hermes Toolset(s) |
|-----------|-------|-------------------|
| `jira_lister` | all | `jira_read` (list, get) |
| `jira_getter` | all | `jira_read` (list, get) |
| `brd_story_creator` | admin, engineer, ba | `jira_read`, `jira_write`, `story`, `confluence` |
| `deploy_pipeline` | admin, engineer | (no tools yet — placeholder) |

## Network Flow

```
Internet → APIM (443) → control-plane (3978, internal) → Service Bus → hermes-service (internal)
                                                                               │
                                            Slack API ←────────────────────────┘
```

All inter-service communication inside the Container Apps Environment is over private Azure network.
Only APIM is exposed to the internet.

## Secrets and Where They Live

| Secret | DEV | PROD |
|--------|-----|------|
| All secrets | `.env` file | Azure Container App secrets (set via Bicep or Portal) |
| `OPENROUTER_API_KEY` | `.env` | Container App secret `openrouter-api-key` |
| `SLACK_BOT_TOKEN` | `.env` | Container App secret `slack-bot-token` |
| `JIRA_API_TOKEN` | `.env` | Container App secret `jira-api-token` |
| `AZURE_SERVICEBUS_CONNECTION_STRING` | `.env` | Container App secret `sb-connection-string` |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | `.env` (optional) | Container App secret `ai-connection-string` |

**Never commit `.env` or any secret to git.**
The `.gitignore` excludes `.env` and all `*.env` files.

## Scaling Behaviour

| Service | Min Replicas (prod) | Max Replicas (prod) | Scale Rule |
|---------|--------------------|--------------------|------------|
| control-plane | 1 | 3 | HTTP concurrentRequests=20 |
| hermes-service | 0 | 5 | Service Bus messageCount=5 |

hermes-service scales to 0 when the queue is empty — no messages = no cost.
It cold-starts in ~10s when a new message arrives.
