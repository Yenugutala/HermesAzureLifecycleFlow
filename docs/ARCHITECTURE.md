# Architecture

## Production Request Flow

```
User (Slack DM or channel mention)
│
│  POST /slack/events  (HTTPS)
▼
Azure API Management
  ├── TLS termination
  ├── Slack HMAC-SHA256 signature pre-check
  ├── Rate limiting: 100 req/min per IP
  ├── Injects X-Correlation-ID header (new UUID if not already set)
  └── Routes /slack/* → control-plane, /api/messages → control-plane
│
│  HTTP (internal Azure network)
▼
Azure Container App: control-plane  (FastAPI, port 3978)
  ├── Reads X-Correlation-ID from header
  ├── Verifies Slack signature (second check, belt+suspenders)
  ├── Resolves Slack user_id → email via Slack users.info API
  ├── Resolves email → Jira account_id + role + capabilities
  │     (capabilities.py: admin/engineer/ba/product_owner/viewer)
  ├── Enqueues payload to Service Bus:
  │     { message, user_email, user_role, capabilities,
  │       correlation_id, channel_id, thread_ts, platform }
  └── Returns HTTP 200 to Slack in <3s (Slack requires fast ack)
│
│  Azure Service Bus — control-plane-queue
│    TTL: 1 hour  |  Max delivery: 5  |  Dead-letter on failure
│    Lock duration: 5 min (renewed every 30s by hermes-service)
▼
Azure Container App: hermes-service  (no HTTP port — queue consumer)
  ├── Reads correlation_id from message, logs on every line
  ├── Loads conversation history from SessionDB (SQLite on Azure Files)
  ├── Builds toolsets from user capabilities + always-on set:
  │     capabilities → jira_hermes/story_hermes/confluence_hermes
  │     always-on    → delegation + web + skills + session_search
  ├── Runs Hermes AIAgent:
  │     model: anthropic/claude-haiku-4-5 (OpenRouter)
  │     fallback: claude-haiku-4-5-20251001 → claude-3-5-haiku
  │     tools: jira_list_tickets, jira_get_ticket, jira_create_epic,
  │            jira_create_story, break_brd_into_stories,
  │            confluence_find_relevant, confluence_get_page,
  │            web_search, web_extract, delegate_task, session_search,
  │            skill_manage
  ├── Hermes auto-saves turn to state.db
  └── Posts reply to Slack thread via Slack WebClient
│
▼
Azure Application Insights
  Traces, costs, errors — queryable by correlation_id
```

## RBAC Flow

```
Slack user → email → capabilities.py:get_capabilities(email)
                        └── returns { role, capabilities[] }
                              │
                              ▼
                    _toolsets_for_capabilities(capabilities)
                        ├── jira_lister → jira_hermes
                        ├── jira_getter → jira_hermes
                        ├── brd_story_creator → jira_hermes + story_hermes + confluence_hermes
                        └── deploy_pipeline → []
                              + always append: delegation, web, skills, session_search
                              │
                              ▼
                    AIAgent(enabled_toolsets=[...])
```

## Hermes Agent Internals

| Component | Purpose |
|-----------|---------|
| `hermes.yaml` | Plugins, toolsets, fallback model, web backend, delegation config |
| `SOUL.md` (`~/.hermes/SOUL.md`) | Agent identity loaded every turn |
| `SessionDB` (`~/.hermes/state.db`) | SQLite conversation history across all threads |
| `skills/brd-breakdown/SKILL.md` | 6-step BRD → Jira hierarchy procedure |
| `skills/jira-assistant/SKILL.md` | Quick-reference for list/get/create ticket ops |
| `plugins/jira_plugin.py` | `jira_list_tickets`, `jira_get_ticket`, `jira_create_epic`, `jira_create_story` |
| `plugins/story_plugin.py` | `break_brd_into_stories` (Claude LLM call) |
| `plugins/confluence_plugin.py` | `confluence_get_page`, `confluence_find_relevant` |
| `plugins/search_plugin.py` | `confluence_find_relevant` — vector similarity via numpy |
| `vector_store.py` | In-memory numpy cosine similarity index (loaded at startup) |
| `scripts/build_index.py` | Builds the vector index from Confluence pages |

## End-to-End Tracing with correlation_id

Every request gets a UUID `correlation_id`:
- **APIM**: generates it as `X-Correlation-ID` header (new if not present)
- **control-plane**: reads from header, logs it, includes in Service Bus message
- **hermes-service**: reads from message body, logs on every line, passes to `RequestTracker`
- **telemetry.py**: emits as `customDimensions.correlation_id` to App Insights

### App Insights query — trace one full request

```kusto
union customEvents, traces, exceptions
| where customDimensions.correlation_id == "YOUR-UUID-HERE"
| project timestamp, itemType, message, customDimensions
| order by timestamp asc
```

## Azure Resources

```
hermes-rg  (Resource Group)
├── hermesacr             Azure Container Registry
├── hermes-sb             Service Bus Namespace
│   └── control-plane-queue
├── hermes-logs           Log Analytics Workspace
├── hermes-ai             Application Insights
├── hermesstate           Storage Account (Azure Files)
│   └── hermes-state      File Share → mounted at /mnt/hermes-state
├── hermes-env            Container Apps Environment
│   ├── hermes-control-plane   Container App (HTTP, scales on requests)
│   └── hermes-hermes-service  Container App (no HTTP, scales on queue depth)
└── hermes-apim           API Management (Application Gateway)
    └── hermes-api
        ├── /slack/events  → control-plane
        └── /api/messages  → control-plane
```

## Multi-Agent Delegation

When a request requires parallelism (e.g. a large BRD with 5+ epics):

```
hermes-service (parent agent)
      │
      │  delegate_task("Create epic for FR-1")
      │  delegate_task("Create epic for FR-2")
      │  delegate_task("Create epic for FR-3")
      ▼
  child agents (run concurrently, max 3)
      each with same toolset, isolated context
      parent sees only the summary result
```

Configured in `hermes.yaml`:
```yaml
delegation:
  max_concurrent_children: 3
  max_spawn_depth: 1   # flat: parent → children only
```
