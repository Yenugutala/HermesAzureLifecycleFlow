# Adding a New Agent to Hermes

This guide explains how to extend Hermes with a new external service integration
(e.g. GitHub, ServiceNow, PagerDuty). Read this before writing any code.

---

## Architecture

```
Slack message
  → control-plane/main.py      POST /slack/events
  → capabilities resolved       get_capabilities(user_email)  →  control-plane/capabilities.py
  → payload enqueued            queue_producer.enqueue_message → Azure Service Bus
  → hermes-service/main.py      picks up from queue
  → _toolsets_for_capabilities() maps capability → toolset names
  → AIAgent.run_conversation()  with enabled_toolsets
  → Hermes agent calls tool     (e.g. github_list_prs)
  → plugin handler invoked      plugins/github_plugin.py
  → handler calls agent fn      agents/github_agent.py (async)
  → HTTP call to external API
  → result JSON returned up chain
  → agent formats reply → sent back to Slack thread
```

### The two code layers

| Layer | Location | Responsibility |
|-------|----------|----------------|
| **Agent** | `agents/my_agent.py` | Pure async Python. Makes HTTP calls. Returns plain Python values (`str`, `dict`, `list`). Knows nothing about Hermes. |
| **Plugin** | `plugins/my_plugin.py` | Hermes glue. Defines JSON schemas. Converts async → sync (`_run`). Returns `json.dumps({...})` strings. Registered via `setup(context)`. |
| **Registration** | `hermes.yaml` | Tells Hermes which plugins to load and which tools belong to which named toolsets. |
| **RBAC gate** | `hermes-service/main.py` | `CAPABILITY_TOOLSET_MAP` maps a user's capability string to toolset names. Controls who can call which tools. |
| **Identity** | `control-plane/capabilities.py` | Maps email → role → capability list. Add new capabilities here if needed. |

---

## Step 1 — Create `agents/my_agent.py`

The agent layer is a plain Python module. Follow these rules exactly:

- **Read env vars inside `_cfg()`**, never at module level — `dotenv` loads after imports.
- Return plain Python values (`str`, `dict`, dataclass) — not JSON strings.
- Use `loop.run_in_executor(None, sync_fn, ...)` to wrap blocking `requests` calls.

**`agents/github_agent.py` (minimal example)**

```python
"""
GitHub Agent — lists open pull requests via GitHub REST API.
Config is read at call time so dotenv always wins.
"""
from __future__ import annotations

import asyncio
import logging
import os
import requests

log = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────

def _cfg() -> dict:
    """Read config from env at call time — never cached at module load."""
    return {
        "token": os.environ.get("GITHUB_TOKEN", ""),
        "owner": os.environ.get("GITHUB_OWNER", ""),
        "repo":  os.environ.get("GITHUB_REPO", ""),
    }


def _headers(cfg: dict) -> dict:
    return {
        "Authorization": f"Bearer {cfg['token']}",
        "Accept": "application/vnd.github+json",
    }


# ── Sync HTTP call ────────────────────────────────────────────────────────────

def _sync_list_open_prs() -> list[dict]:
    cfg = _cfg()
    url = f"https://api.github.com/repos/{cfg['owner']}/{cfg['repo']}/pulls"
    r = requests.get(url, headers=_headers(cfg), params={"state": "open"}, timeout=15)
    r.raise_for_status()
    return [
        {"number": pr["number"], "title": pr["title"], "author": pr["user"]["login"], "url": pr["html_url"]}
        for pr in r.json()
    ]


# ── Async wrapper (called by plugin) ─────────────────────────────────────────

async def list_open_prs() -> list[dict]:
    """List open pull requests (async wrapper around sync requests call)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_list_open_prs)
```

---

## Step 2 — Create `plugins/my_plugin.py`

The plugin layer is Hermes glue. Follow these rules exactly:

- **Copy `_run()` verbatim** from any existing plugin — it handles nested event loops correctly.
- **Import agent functions inside the handler body**, not at the top of the file (dotenv timing).
- **Every handler must return `json.dumps({...})`** — Hermes expects a string, always.
- **Never raise from a handler** — catch all exceptions and return `{"error": "..."}`.
- **`setup(context)`** is the entry point Hermes calls — register all tools here.

**`plugins/github_plugin.py` (minimal example)**

```python
"""
GitHub plugin for Hermes Agent.
Registers: github_list_prs
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import os

log = logging.getLogger(__name__)


# ── Tool schema ───────────────────────────────────────────────────────────────
# The description tells the agent WHEN to call this tool — make it precise.

LIST_PRS_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "description": (
        "List all open GitHub pull requests for the configured repository. "
        "Returns PR number, title, author, and URL."
    ),
}


# ── Async → sync bridge ───────────────────────────────────────────────────────
# Copy this verbatim. It handles the case where an event loop is already running
# (e.g. inside the async Service Bus queue consumer in main.py).

def _run(coro):
    """Run an async coroutine from a synchronous tool handler."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


# ── Handler ───────────────────────────────────────────────────────────────────

def _list_prs_handler(params: dict, **kwargs) -> str:
    try:
        # Import inside handler so dotenv is already loaded
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from agents.github_agent import list_open_prs

        prs = _run(list_open_prs())
        log.info("github_list_prs: returned %d PRs", len(prs))
        return json.dumps({"pull_requests": prs, "count": len(prs)})
    except Exception as e:
        log.error("github_list_prs error: %s", e)
        return json.dumps({"error": str(e)})


# ── Plugin entry point ────────────────────────────────────────────────────────

def setup(context) -> None:
    """Called by Hermes Agent plugin loader. Register all tools here."""
    context.register_tool(
        name="github_list_prs",
        handler=_list_prs_handler,
        schema=LIST_PRS_SCHEMA,
    )
    log.info("GitHub plugin loaded: 1 tool registered")
```

---

## Step 3 — Register in `hermes.yaml`

Add the plugin path and a new toolset. The toolset name must match what you use in Step 4.

```yaml
# hermes.yaml — diff to add GitHub

plugins:
  - path: plugins/jira_plugin.py
  - path: plugins/story_plugin.py
  - path: plugins/confluence_plugin.py
  - path: plugins/search_plugin.py
  - path: plugins/github_plugin.py    # ← add this

toolsets:
  jira_read:
    - jira_list_tickets
    - jira_get_ticket
  jira_write:
    - jira_create_epic
    - jira_create_story
  story:
    - break_brd_into_stories
  confluence:
    - confluence_search
    - confluence_get_page
    - confluence_find_relevant
  github:                             # ← add this block
    - github_list_prs
```

---

## Step 4 — Map capability in `hermes-service/main.py`

Open [`hermes-service/main.py`](main.py) and add your capability to `CAPABILITY_TOOLSET_MAP`
(currently at line 56):

```python
# main.py — diff

CAPABILITY_TOOLSET_MAP: dict[str, list[str]] = {
    "jira_lister":       ["jira_read"],
    "jira_getter":       ["jira_read"],
    "brd_story_creator": ["jira_read", "jira_write", "story", "confluence"],
    "deploy_pipeline":   [],
    "pr_reviewer":       ["github"],   # ← add this line
}
```

The capability string (`"pr_reviewer"`) is what arrives in the `payload["capabilities"]`
list from the control-plane. See Step 4b to add it there too.

### Step 4b — Add capability to `control-plane/capabilities.py` (optional)

If you want to assign this capability to a role, edit `control-plane/capabilities.py`:

```python
# capabilities.py — diff

CAPABILITY_SETS = {
    "admin":         ["jira_lister", "jira_getter", "brd_story_creator", "deploy_pipeline", "pr_reviewer"],
    "engineer":      ["jira_lister", "jira_getter", "pr_reviewer"],   # ← add pr_reviewer
    "ba":            ["jira_lister", "jira_getter", "brd_story_creator"],
    "product_owner": ["jira_lister", "jira_getter"],
    "viewer":        ["jira_lister"],
}
```

---

## Step 5 — Add env vars

**`.env` (local development)**

```bash
GITHUB_TOKEN=ghp_yourPersonalAccessToken
GITHUB_OWNER=your-org
GITHUB_REPO=your-repo
```

**`infra/params/prod.bicepparam` (Azure deployment)**

Add a new parameter for each secret:

```bicep
param githubToken = readEnvironmentVariable('GITHUB_TOKEN')
```

**`infra/main.bicep` (Azure Container App)**

Add the secret and env var to the `hermes-hermes-service` Container App resource:

```bicep
// under secrets:
{ name: 'github-token', value: githubToken }

// under env:
{ name: 'GITHUB_TOKEN', secretRef: 'github-token' }
```

---

## Key Rules Cheat-Sheet

| Rule | Why |
|------|-----|
| Read env vars inside `_cfg()`, never at module load | `load_dotenv` runs after Python imports — module-level reads see empty strings |
| Handlers return `json.dumps({...})`, never raise | Hermes tool loop expects a string; an unhandled exception crashes the agent |
| Import agent functions inside the handler body | Same dotenv timing issue — top-level imports run before `.env` is loaded |
| Copy `_run()` verbatim from an existing plugin | Handles the nested event loop case (Service Bus consumer is async; plugin handlers are sync) |
| Tool schema `description` must be precise | The agent uses it to decide when to call the tool — vague descriptions cause missed or wrong calls |

---

## Testing Locally (without Slack)

You can send a test JSON payload directly to the Service Bus queue using Azure Portal:

1. Open **Azure Portal → Service Bus → hermes-servicebus → Queues → control-plane-queue**
2. Click **Service Bus Explorer** in the left menu
3. Click **Send messages**
4. Paste the payload below (adjust `user_email` and `message`):

```json
{
  "platform": "slack",
  "user_id": "U999TEST",
  "user_email": "your@email.com",
  "jira_account_id": null,
  "user_role": "engineer",
  "capabilities": ["pr_reviewer"],
  "message": "list open pull requests",
  "channel_id": "C999TEST",
  "thread_ts": "1700000000.000000",
  "correlation_id": "test-local-001"
}
```

5. Click **Send** — `hermes-service` will pick it up, call your new tool, and log the result.
   The Slack reply will go to channel `C999TEST` (which won't exist, so you'll just see the log).

To verify the tool was called, watch the `hermes-service` console:

```
INFO hermes_service — [test-local-001] Routing | user=your@email.com role=engineer toolsets=['github', 'delegation', 'web', 'skills', 'session_search']
INFO plugins.github_plugin — github_list_prs: returned 4 PRs
```

---

## File Reference

| File | Role |
|------|------|
| [`agents/jira_agent.py`](agents/jira_agent.py) | Template for the agent layer |
| [`plugins/jira_plugin.py`](plugins/jira_plugin.py) | Template for the plugin layer |
| [`hermes.yaml`](hermes.yaml) | Plugin and toolset registration |
| [`main.py` lines 56–77](main.py) | `CAPABILITY_TOOLSET_MAP` — RBAC gate |
| [`../control-plane/capabilities.py`](../control-plane/capabilities.py) | Role → capability mapping |
| [`../infra/main.bicep`](../infra/main.bicep) | Where to add env vars for Azure Container Apps |
| [`../infra/params/prod.bicepparam`](../infra/params/prod.bicepparam) | Where to add secret parameter values |
