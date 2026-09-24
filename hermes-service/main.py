"""
Hermes Service — Azure Service Bus queue consumer.

Uses Nous Research Hermes Agent (AIAgent) for tool orchestration.
Tools are registered as plugins (plugins/jira_plugin.py, plugins/story_plugin.py,
plugins/confluence_plugin.py, plugins/developer_plugin.py). The agent decides which tools to call.

Capabilities from the Control Plane payload map to Hermes toolsets,
enforcing role-based access (viewer → jira_read only, BA → full set,
admin/engineer → full set + developer toolset for agent creation).

Telemetry → Azure App Insights via opencensus-ext-azure.
Requires Python 3.11+ (hermes-agent dependency).
"""
import os
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv(usecwd=False))

import asyncio
import json
import logging
import time
import uuid
from azure.servicebus.aio import ServiceBusClient
from slack_sdk import WebClient as SlackClient
from run_agent import AIAgent
from telemetry import RequestTracker
from hermes_state import SessionDB
import vector_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger(__name__)

# Silence noisy Azure AMQP connection-state chatter and irrelevant plugin load failures
logging.getLogger("azure.servicebus._pyamqp").setLevel(logging.WARNING)
logging.getLogger("hermes_cli.plugins").setLevel(logging.WARNING)

# ── Load Confluence vector index at startup ───────────────────────────────────
vector_store.load_index()

# ── Hermes SessionDB — durable SQLite conversation history ────────────────────
# Writes to ~/.hermes/state.db; persists across service restarts.
# Set HERMES_HOME to an Azure Files mount path for cross-deployment persistence.
_SESSION_DB = SessionDB()

CONN_STR         = os.environ["AZURE_SERVICEBUS_CONNECTION_STRING"]
QUEUE_NAME       = os.environ.get("AZURE_SERVICEBUS_QUEUE_NAME", "control-plane-queue")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-haiku-4-5")

slack = SlackClient(token=os.environ["SLACK_BOT_TOKEN"])


# ── Capability → Hermes toolset mapping ───────────────────────────────────────
# Toolset names must match keys defined in hermes.yaml

CAPABILITY_TOOLSET_MAP: dict[str, list[str]] = {
    "jira_lister":       ["jira_hermes"],
    "jira_getter":       ["jira_hermes"],
    "brd_story_creator": ["jira_hermes", "story_hermes", "confluence_hermes"],
    "deploy_pipeline":   [],
    "servicenow_viewer": ["servicenow"],
    "agent_factory":     ["developer"],   # developer_analyze, developer_show_form, developer_create_agent
}


def _toolsets_for_capabilities(capabilities: list[str]) -> list[str]:
    """Map authorized capability names to Hermes toolset names."""
    toolsets: set[str] = set()
    for cap in capabilities:
        toolsets.update(CAPABILITY_TOOLSET_MAP.get(cap, []))
    result = list(toolsets) if toolsets else ["jira_hermes"]
    # When agent_factory is the only active capability, keep toolset clean:
    # developer tools only + session_search. No web/delegation/skills to avoid
    # the LLM using web search instead of developer tools in edge-case fallthrough.
    agent_only = set(capabilities) == {"agent_factory"}
    if agent_only:
        result = list(set(result + ["session_search"]))
    else:
        # Always enable these — overrides global disabled_toolsets in ~/.hermes/config.yaml:
        #   delegation    → delegate_task (parallel multi-agent)
        #   web           → web_search + web_extract (DuckDuckGo, free)
        #   skills        → skill_manage (load/create procedural skills in ~/.hermes/skills/)
        #   session_search → search past conversations in SQLite state.db
        result = list(set(result + ["delegation", "web", "skills", "session_search"]))
    log.info("Capabilities %s → toolsets %s", capabilities, result)
    return result


# ── Agent creation handler (message_type: agent_creation) ─────────────────────

async def _handle_agent_creation(payload: dict) -> str:
    """
    Handles agent_creation messages enqueued by the Slack interactions endpoint
    (POST /slack/interactions) after the user submits the Block Kit modal form.
    Calls developer_create_agent directly — no LLM routing needed.
    """
    agent_params   = payload.get("agent_params", {})
    user_email     = payload.get("user_email", "unknown@unknown.com")
    correlation_id = payload.get("correlation_id", str(uuid.uuid4()))

    log.info("[%s] Agent creation job | user=%s service=%s",
             correlation_id, user_email, agent_params.get("service_name"))

    channel_id = payload.get("channel_id", "")
    thread_ts  = payload.get("thread_ts", "")

    try:
        from agents.developer_agent import generate_agent_code, push_pr_to_github
        import re

        # Progress update 1
        send_slack_reply(channel_id, thread_ts, f"⚙️ Generating *{agent_params.get('display_name')}* agent code...")

        files = await generate_agent_code(
            service_name    = agent_params["service_name"],
            display_name    = agent_params["display_name"],
            api_base_url    = agent_params["api_base_url"],
            auth_type       = agent_params["auth_type"],
            operations      = agent_params["operations"],
            capability_name = agent_params["capability_name"],
        )

        # Progress update 2
        send_slack_reply(channel_id, thread_ts, f"📦 Pushing {len(files)} files to GitHub and creating PR...")

        pr_url = await push_pr_to_github(
            user_email      = user_email,
            service_name    = agent_params["service_name"],
            display_name    = agent_params["display_name"],
            files           = files,
            capability_name = agent_params["capability_name"],
        )

        # Build branch name for display
        username = re.sub(r"[^a-z0-9]", "", (user_email.split("@")[0]).lower())[:20]
        from datetime import datetime, timezone
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        branch = f"feature_{username}_{agent_params['service_name']}_{date_str}"

        file_list = "\n".join(f"  • `{f}`" for f in files.keys())
        log.info("[%s] PR created: %s", correlation_id, pr_url)
        return (
            f"✅ *{agent_params['display_name']} Agent PR Created*\n\n"
            f"*Files generated:*\n{file_list}\n\n"
            f"*Branch:* `{branch}` → `develop`\n"
            f"*PR:* <{pr_url}|View on GitHub>\n\n"
            f"_An engineer will review and merge. Until then, {agent_params['display_name']} requests will show as pending._"
        )
    except Exception as e:
        log.error("[%s] Agent creation failed: %s", correlation_id, e, exc_info=True)
        return f"❌ Could not create agent PR: {e}"


# ── Deterministic agent-factory router ────────────────────────────────────────

_KNOWN_KEYWORDS = {
    "jira", "confluence", "story", "sharepoint", "onedrive",
    "ticket", "tickets", "epic", "epics", "brd", "sprint",
}

# ── Well-known service registry ────────────────────────────────────────────────
# api_base_url may contain {instance} — filled from user's answer at Stage 3.
_KNOWN_SERVICES: dict[str, dict] = {
    "servicenow": {
        "display_name":   "ServiceNow",
        "api_base_url":   "https://{instance}.service-now.com/api/now/table",
        "auth_type":      "basic",
        "operations":     ["list", "get_by_id", "create", "update"],
        "capability_name":"servicenow_viewer",
        "instance_hint":  "Your ServiceNow instance name (e.g. `mycompany` → mycompany.service-now.com)",
        "needs_instance": True,
    },
    "databricks": {
        "display_name":   "Databricks",
        "api_base_url":   "https://{instance}.azuredatabricks.net/api/2.0",
        "auth_type":      "bearer",
        "operations":     ["list", "get_by_id"],
        "capability_name":"databricks_viewer",
        "instance_hint":  "Your Azure Databricks workspace prefix (e.g. `adb-12345678.1`)",
        "needs_instance": True,
    },
    "sap": {
        "display_name":   "SAP",
        "api_base_url":   "https://{instance}/sap/opu/odata/sap/API_BUSINESS_PARTNER",
        "auth_type":      "basic",
        "operations":     ["list", "get_by_id"],
        "capability_name":"sap_viewer",
        "instance_hint":  "Your SAP system hostname (e.g. `my-sap.example.com`)",
        "needs_instance": True,
    },
    "salesforce": {
        "display_name":   "Salesforce",
        "api_base_url":   "https://{instance}.my.salesforce.com/services/data/v60.0",
        "auth_type":      "bearer",
        "operations":     ["list", "get_by_id", "create", "update"],
        "capability_name":"salesforce_viewer",
        "instance_hint":  "Your Salesforce My Domain (e.g. `mycompany` → mycompany.my.salesforce.com)",
        "needs_instance": True,
    },
    "pagerduty": {
        "display_name":   "PagerDuty",
        "api_base_url":   "https://api.pagerduty.com",
        "auth_type":      "pagerduty_token",
        "operations":     ["list", "get_by_id", "create"],
        "capability_name":"pagerduty_viewer",
        "instance_hint":  None,   # fixed global URL — no instance placeholder
        "needs_instance": False,
    },
    "zendesk": {
        "display_name":   "Zendesk",
        "api_base_url":   "https://{instance}.zendesk.com/api/v2",
        "auth_type":      "zendesk_token",
        "operations":     ["list", "get_by_id", "create", "update", "close"],
        "capability_name":"zendesk_viewer",
        "instance_hint":  "Your Zendesk subdomain (e.g. `mycompany` → mycompany.zendesk.com)",
        "needs_instance": True,
    },
}

# Multi-word aliases → canonical service key
_SERVICE_ALIASES: dict[str, str] = {
    "service now": "servicenow",
    "service-now": "servicenow",
    "pager duty":  "pagerduty",
    "pager-duty":  "pagerduty",
}


def _lookup_known_service(message: str) -> dict | None:
    """Return the _KNOWN_SERVICES entry if the message mentions a well-known service, else None."""
    msg_lower = message.lower()
    for alias, canonical in _SERVICE_ALIASES.items():
        if alias in msg_lower:
            return _KNOWN_SERVICES[canonical]
    for key, info in _KNOWN_SERVICES.items():
        if key in msg_lower or info["display_name"].lower() in msg_lower:
            return info
    return None


def _create_jira_story_sync(service_name: str, user_email: str) -> str | None:
    """Create a Jira Story for SDLC onboarding of a custom service. Returns story URL or None."""
    import requests as _req
    import base64 as _b64
    jira_base  = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
    jira_email = os.environ.get("JIRA_EMAIL", "")
    jira_token = os.environ.get("JIRA_API_TOKEN", "")
    proj_key   = os.environ.get("JIRA_PROJECT_KEY", "SCRUM")
    issue_type = os.environ.get("JIRA_ISSUE_TYPE", "Task")
    if not (jira_base and jira_email and jira_token):
        return None
    creds   = _b64.b64encode(f"{jira_email}:{jira_token}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}
    body = {
        "fields": {
            "project":     {"key": proj_key},
            "summary":     f"Onboard {service_name} integration to Hermes",
            "description": {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph", "content": [
                    {"type": "text",
                     "text": (f"Requested by {user_email}.\n"
                              f"Evaluate and onboard {service_name} as a Hermes agent integration.\n"
                              f"Define API endpoints, auth strategy, and scaffolding requirements.")}
                ]}]
            },
            "issuetype": {"name": issue_type},
        }
    }
    try:
        r = _req.post(f"{jira_base}/rest/api/3/issue", json=body, headers=headers, timeout=10)
        r.raise_for_status()
        issue_key = r.json()["key"]
        return f"{jira_base}/browse/{issue_key}"
    except Exception as _e:
        log.warning("Jira story creation failed for %s: %s", service_name, _e)
        return None


_AGENT_FACTORY_STATE: dict[str, dict] = {}   # thread_id → {stage, service_name, ...}


_STOP_WORDS = {
    "can", "you", "pull", "show", "get", "list", "fetch", "find", "the", "a", "an",
    "me", "my", "our", "all", "open", "closed", "please", "help", "want", "need",
    "incidents", "tickets", "issues", "records", "items", "data", "tasks", "cases",
    "from", "for", "in", "of", "to", "with", "and", "or", "is", "are", "what",
    "how", "when", "where", "who", "that", "this", "those", "these",
    # prevent extracting words from user's answer text
    "api", "base", "url", "auth", "type", "name", "http", "https", "bearer",
    "apikey", "basic", "oauth", "oauth2", "operations", "display", "capability",
    "readable", "human", "rbac", "access", "control", "viewer", "company",
    "example", "your", "reply", "provide", "answers", "answer",
    # common verbs / nouns that are never service names
    "build", "create", "make", "setup", "agent", "now", "just", "also",
    "would", "could", "should", "will", "have", "like", "use", "using",
}


def _extract_service_name(message: str, active_registry: list) -> str | None:
    """Return the unknown service name mentioned in the message, or None."""
    import re as _re2
    known = {a["name"].lower() for a in active_registry} | _KNOWN_KEYWORDS | _STOP_WORDS
    words = message.lower().split()

    candidates = []
    for word in words:
        # Skip URL-like tokens — https://... or domain.tld.suffix blobs
        if "://" in word or word.count(".") >= 2:
            continue
        clean = _re2.sub(r"[^a-z]", "", word)   # keep only lowercase letters
        # Require min 4 chars to avoid "i", "eg", "now", etc. combining into false names
        if clean and len(clean) >= 4 and clean not in known:
            candidates.append(clean)

    if not candidates:
        return None

    # Combine up to 2 consecutive unknown words (e.g. ["pager", "duty"] → "PagerDuty")
    if len(candidates) >= 2:
        return "".join(w.title() for w in candidates[:2])
    return candidates[0].title()


async def _agent_factory_router(
    message: str,
    payload: dict,
    active_registry: list,
    pending_agents: list,
    channel_id: str,
    thread_ts: str,
    prev_messages: list,
) -> str | None:
    """
    Deterministic pre-router for unknown services.
    Returns a response string if handled, None to fall through to LLM.

    State fields stored per thread_id:
      stage        : "asked_confirmation" | "asked_questions" | "completed"
      service_name : display-friendly name (e.g. "ServiceNow")
      service_info : dict from _KNOWN_SERVICES, or None for custom apps
      is_known     : True if service_info is set
    """
    import re as _re
    import re as _re2
    from datetime import datetime, timezone
    msg_lower  = message.lower().strip()
    thread_id  = f"{payload.get('platform','slack')}-{channel_id}-{thread_ts}"
    state      = _AGENT_FACTORY_STATE.get(thread_id, {})
    stage      = state.get("stage")
    user_email = payload.get("user_email", "unknown@unknown.com")

    # ── Already completed — don't re-trigger for this thread ──────────────────
    if stage == "completed":
        return None

    # ── STAGE 2: confirmation received ────────────────────────────────────────
    if stage == "asked_confirmation":
        service_name = state.get("service_name", "the service")
        service_info = state.get("service_info")        # dict or None
        is_known     = state.get("is_known", False)

        # Jira SDLC path — custom services only
        if not is_known and any(w in msg_lower for w in ("jira", "story", "ticket", "sdlc")):
            _AGENT_FACTORY_STATE[thread_id] = {"stage": "completed", "service_name": service_name}
            send_slack_reply(channel_id, thread_ts, f"📋 Creating Jira Story for *{service_name}* onboarding...")
            story_url = _create_jira_story_sync(service_name, user_email)
            if story_url:
                return (
                    f"✅ *Jira Story Created*\n\n"
                    f"A Story has been raised for the *{service_name}* integration:\n"
                    f"<{story_url}|View Story on Jira>\n\n"
                    f"_An engineer will evaluate and kick off the SDLC onboarding workflow._"
                )
            return (
                f"⚠️ Could not create the Jira Story automatically.\n"
                f"Please raise one manually in your `{os.environ.get('JIRA_PROJECT_KEY','SCRUM')}` project board."
            )

        # Yes / scaffold / proceed path
        if any(w in msg_lower for w in ("yes", "yeah", "sure", "proceed", "confirm", "go ahead", "scaffold")):
            # Check if already pending in registry
            for p in pending_agents:
                if p["name"].lower() in service_name.lower() or service_name.lower() in p["name"].lower():
                    _AGENT_FACTORY_STATE.pop(thread_id, None)
                    return (
                        f"⏳ *{p['display_name']}* agent is already pending engineer review.\n"
                        f"*PR:* <{p['pr_url']}|View on GitHub>"
                    )

            if is_known and service_info:
                needs_instance = service_info.get("needs_instance", True)
                if needs_instance:
                    hint = service_info.get("instance_hint", "your instance hostname")
                    _AGENT_FACTORY_STATE[thread_id] = {
                        "stage": "asked_questions", "service_name": service_name,
                        "service_info": service_info, "is_known": True,
                    }
                    return (
                        f"To set up the *{service_info['display_name']}* agent, I need 2 things:\n\n"
                        f"1. *Instance* — {hint}\n"
                        f"2. *Credential* — API token, bearer token, or `username:password`\n\n"
                        f"Reply with both values (one per line)."
                    )
                else:
                    # PagerDuty — no instance needed, just the API key
                    _AGENT_FACTORY_STATE[thread_id] = {
                        "stage": "asked_questions", "service_name": service_name,
                        "service_info": service_info, "is_known": True,
                    }
                    return (
                        f"To set up the *{service_info['display_name']}* agent, I need 1 thing:\n\n"
                        f"1. *API Key* — your {service_info['display_name']} REST API key\n\n"
                        f"Reply with the value."
                    )
            else:
                # Custom / unknown service — ask 5 questions
                _AGENT_FACTORY_STATE[thread_id] = {
                    "stage": "asked_questions", "service_name": service_name,
                    "service_info": None, "is_known": False,
                }
                cap_default = service_name.lower().replace(" ", "_")
                return (
                    f"To build the *{service_name}* agent, please provide:\n\n"
                    f"1. *API Base URL* — e.g., `https://api.your-company.com`\n"
                    f"2. *Auth type* — `bearer` / `apikey` / `basic` / `oauth2`\n"
                    f"3. *Operations* — any of: `list`, `get_by_id`, `create`, `update`, `close`\n"
                    f"4. *Display name* — human-readable (e.g., {service_name})\n"
                    f"5. *Capability name* — for RBAC (e.g., `{cap_default}_viewer`)\n\n"
                    f"Reply with all 5 answers."
                )

        # No / cancel
        if any(w in msg_lower for w in ("no", "cancel", "nevermind", "nope", "skip")):
            _AGENT_FACTORY_STATE.pop(thread_id, None)
            return f"Got it — no *{service_name}* agent will be created. Feel free to ask me anything else."

    # ── STAGE 3: answers received → generate code + PR ────────────────────────
    if stage == "asked_questions":
        lines        = [ln.strip() for ln in message.strip().splitlines() if ln.strip()]
        service_name = state.get("service_name", "unknown")
        service_info = state.get("service_info")
        is_known     = state.get("is_known", False)

        if is_known and service_info:
            # Known service — need at least 1 line (instance or credential)
            if not lines:
                return None
            needs_instance = service_info.get("needs_instance", True)
            display_name   = service_info["display_name"]
            auth_type      = service_info["auth_type"]
            operations     = service_info["operations"]
            capability_name = service_info["capability_name"]
            svc_snake      = service_info["display_name"].lower().replace(" ", "").replace("-", "")

            if needs_instance:
                # First line → instance, second line → credential (ignored at runtime — goes into env var)
                raw_instance = _re2.sub(r"https?://", "", lines[0]).split(".")[0].split("/")[0].strip()
                instance     = _re2.sub(r"[^a-z0-9\-]", "", raw_instance.lower()) or "myinstance"
                api_base_url = service_info["api_base_url"].replace("{instance}", instance)
            else:
                # PagerDuty — fixed URL, no instance substitution
                api_base_url = service_info["api_base_url"]
        else:
            # Custom service — need at least 3 lines or a URL
            has_url    = any("http" in ln.lower() or "://" in ln for ln in lines)
            if len(lines) < 3 and not has_url:
                return None
            display_name    = next((ln for ln in lines if ln.istitle() or (len(ln.split()) <= 3 and ln[0].isupper())), service_name)
            api_base_url    = next((ln for ln in lines if "http" in ln.lower()), "https://api.example.com")
            auth_type       = next((w for ln in lines for w in ln.lower().split()
                                    if w in ("bearer", "apikey", "basic", "oauth2")), "bearer")
            ops_raw         = next((ln for ln in lines if any(op in ln.lower()
                                    for op in ("list", "get", "create", "update", "close"))), "list")
            operations      = [op for op in ("list", "get_by_id", "create", "update", "close")
                               if op.split("_")[0] in ops_raw.lower()]
            if not operations:
                operations = ["list"]
            capability_name = next((ln for ln in lines if "_viewer" in ln or (ln.startswith(service_name.lower()[:4]) and "_" in ln)),
                                   f"{service_name.lower().replace(' ', '_')}_viewer")
            svc_snake       = service_name.lower().replace(" ", "_")

        # Mark completed BEFORE generation — prevents Service Bus retry → Stage 1 re-trigger
        _AGENT_FACTORY_STATE[thread_id] = {"stage": "completed", "service_name": service_name}

        from agents.developer_agent import generate_agent_code, push_pr_to_github
        try:
            send_slack_reply(channel_id, thread_ts, f"⚙️ Generating *{display_name}* agent code...")
            files  = await generate_agent_code(svc_snake, display_name, api_base_url, auth_type, operations, capability_name)
            send_slack_reply(channel_id, thread_ts, f"📦 Pushing {len(files)} files to GitHub...")
            pr_url = await push_pr_to_github(user_email, svc_snake, display_name, files, capability_name)

            username  = _re2.sub(r"[^a-z0-9]", "", (user_email.split("@")[0]).lower())[:20]
            date_str  = datetime.now(timezone.utc).strftime("%Y%m%d")
            branch    = f"feature_{username}_{svc_snake}_{date_str}"
            file_list = "\n".join(f"• `{f}`" for f in files.keys())
            return (
                f"✅ *{display_name} Agent — PR Created*\n\n"
                f"*Files generated:*\n{file_list}\n\n"
                f"*Branch:* `{branch}` → `develop`\n"
                f"*PR:* <{pr_url}|View on GitHub>\n\n"
                f"_An engineer will review and merge. Until then, {display_name} requests will show as pending._"
            )
        except Exception as _gen_err:
            log.error("Agent creation failed for %s: %s", svc_snake, _gen_err, exc_info=True)
            return f"❌ Failed to create *{display_name}* agent: {_gen_err}"

    # ── STAGE 1: detect service in a fresh message ─────────────────────────────
    # Skip numbered-list messages — they look like user's own answers, not new requests
    if _re.search(r'^\s*\d+[\.\)]', message, _re.MULTILINE):
        return None

    msg_words      = set(msg_lower.split())
    already_active = bool(msg_words & _KNOWN_KEYWORDS) or any(
        a["name"].lower() in msg_lower or a["display_name"].lower() in msg_lower
        for a in active_registry
    )
    if already_active:
        return None   # handled natively — let LLM route it

    active_names = ", ".join(a["display_name"] for a in active_registry) or "Jira, Confluence, Story Builder"

    # Check well-known services first (ServiceNow, Databricks, SAP, Salesforce, PagerDuty, Zendesk)
    known_svc = _lookup_known_service(message)
    if known_svc:
        _AGENT_FACTORY_STATE[thread_id] = {
            "stage": "asked_confirmation",
            "service_name": known_svc["display_name"],
            "service_info": known_svc,
            "is_known": True,
        }
        ops_str = ", ".join(known_svc["operations"])
        return (
            f"🔧 I don't have a *{known_svc['display_name']}* integration yet — but I know how to build one!\n\n"
            f"*Official API:* `{known_svc['api_base_url']}`\n"
            f"*Auth:* `{known_svc['auth_type']}`\n"
            f"*Operations:* {ops_str}\n\n"
            f"*Currently supported:* {active_names}\n\n"
            f"Want me to scaffold a *{known_svc['display_name']}* agent? Reply *yes* to proceed, or *no* to cancel."
        )

    # Unknown / custom service — offer scaffold or Jira Story
    custom_name = _extract_service_name(message, active_registry)
    if custom_name:
        _AGENT_FACTORY_STATE[thread_id] = {
            "stage": "asked_confirmation",
            "service_name": custom_name,
            "service_info": None,
            "is_known": False,
        }
        return (
            f"🤔 *{custom_name}* doesn't look like a standard integration I recognise.\n\n"
            f"*Currently supported:* {active_names}\n\n"
            f"How would you like to proceed?\n"
            f"• Reply *scaffold* — I'll build a custom agent with your API details\n"
            f"• Reply *jira* — I'll create a Jira Story so your team can onboard it via SDLC"
        )

    # Not handled — fall through to LLM
    return None


# ── Route message ──────────────────────────────────────────────────────────────

async def route_message(payload: dict) -> str:
    # ── Short-circuit: agent_creation jobs bypass normal LLM routing ─────────
    if payload.get("message_type") == "agent_creation":
        return await _handle_agent_creation(payload)

    message        = payload.get("message", "").strip()
    user_email     = payload.get("user_email", "")
    user_role      = payload.get("user_role", "unknown")
    capabilities   = payload.get("capabilities", [])
    correlation_id = payload.get("correlation_id") or str(uuid.uuid4())

    # Registry passed from control-plane in every payload
    active_registry  = payload.get("agent_registry_active", [])
    pending_agents   = payload.get("agent_registry_pending", [])
    active_names     = ", ".join(a["display_name"] for a in active_registry) or "Jira, Confluence, Story"

    if not message:
        return "Please send a message."

    # ── Conversation history (Hermes SessionDB → SQLite) ──────────────────────
    channel_id    = payload.get("channel_id", "unknown")
    thread_ts     = payload.get("thread_ts") or str(int(time.time()))
    thread_id     = f"{payload.get('platform', 'slack')}-{channel_id}-{thread_ts}"
    prev_messages = _SESSION_DB.get_messages(thread_id, limit=20)

    # ── Deterministic agent-factory pre-routing (bypasses LLM entirely) ───────
    if "agent_factory" in capabilities:
        result = await _agent_factory_router(
            message, payload, active_registry, pending_agents,
            channel_id, thread_ts, prev_messages,
        )
        if result is not None:
            return result

    tracker = RequestTracker(user_email=user_email, user_role=user_role,
                             correlation_id=correlation_id)
    success = True

    try:
        toolsets = _toolsets_for_capabilities(capabilities)
        log.info("[%s] Routing | user=%s role=%s toolsets=%s thread=%s",
                 correlation_id, user_email, user_role, toolsets, thread_id)

        agent = AIAgent(
            model=OPENROUTER_MODEL,
            session_id=thread_id,
            session_db=_SESSION_DB,
            quiet_mode=True,
            enabled_toolsets=toolsets,
            max_iterations=10,
            skip_memory=True,
        )

        # ── Build registry context for system message ──────────────────────────
        pending_names = ", ".join(p["display_name"] for p in pending_agents) if pending_agents else "none"
        registry_context = (
            f"\n\nCURRENT AGENT REGISTRY:\n"
            f"Active agents: {active_names}\n"
            f"Pending (PR open, not yet deployed): {pending_names}\n"
            f"Active registry JSON: {json.dumps(active_registry)}\n"
            f"Pending agents JSON: {json.dumps(pending_agents)}\n"
            f"User channel_id: {channel_id}\n"
            f"User thread_ts: {thread_ts}\n"
            f"User email: {user_email}"
        )

        system_message = (
            "You are a Jira, Confluence, and multi-service assistant. Act immediately — NEVER ask for clarification.\n\n"
            "══════════════════════════════════════════════════════\n"
            "RULE #1 — UNKNOWN SERVICE (checked FIRST before anything else):\n\n"
            "STEP A — Request mentions service NOT in Jira/Confluence/Story Builder/SharePoint:\n"
            "  Reply with EXACTLY this (replace <ServiceName>):\n"
            "    🔍 I don't have a *<ServiceName>* integration yet.\n"
            "    *Currently supported:* Jira, Confluence, Story Builder, SharePoint\n"
            "    Want me to build a *<ServiceName>* agent? Reply *yes* to proceed.\n"
            "  Do NOT call any tools. Do NOT search the web. Just send that reply.\n\n"
            "STEP B — User replies yes/confirm/proceed after Step A:\n"
            "  First call developer_analyze. If matched_pending=true → reply: ⏳ <Name> pending PR review. PR: <pr_url>\n"
            "  If not pending → reply with ALL questions in ONE message:\n"
            "    To build the <ServiceName> agent, please provide:\n"
            "    1. *API Base URL* — e.g., https://your-company.service-now.com\n"
            "    2. *Auth type* — bearer / apikey / basic / oauth2\n"
            "    3. *Operations* — any of: list, get_by_id, create, update, close\n"
            "    4. *Display name* — human-readable (e.g., ServiceNow)\n"
            "    5. *Capability name* — for RBAC (e.g., servicenow_viewer)\n\n"
            "STEP C — User has answered the 5 questions:\n"
            "  Extract values and call developer_create_agent immediately.\n"
            "  Use user_email, channel_id, thread_ts from the CURRENT AGENT REGISTRY context below.\n"
            "  Do NOT ask for clarification — use sensible defaults for any missing value.\n"
            "══════════════════════════════════════════════════════\n\n"
            "RULE #2 — KNOWN SERVICES:\n"
            "- 'show/list tickets' → call jira_list_tickets immediately\n"
            "- 'open/get ticket X' → call jira_get_ticket immediately\n"
            "- 'break into epics/stories' / 'create from BRD' → follow BRD STEPS below\n"
            "- 'what did I ask before' → call session_search\n"
            "- Load brd-breakdown skill before BRD breakdown, jira-assistant skill before listing tickets\n\n"
            "BRD BREAKDOWN STEPS — do ALL in order, never skip, never ask:\n"
            "  1. Call confluence_find_relevant with keywords from the user request or Jira ticket\n"
            "  2. Call confluence_get_page with the best matching page_title from step 1 results\n"
            "  3. Call break_brd_into_stories with the full page content\n"
            "  4. Call jira_create_epic WITHOUT parent_key → creates ONE main Epic (use the project name as summary)\n"
            "  5. For EACH functional requirement returned in step 3, call jira_create_epic WITH parent_key = main Epic key from step 4\n"
            "  6. For EACH story/task under each FR, call jira_create_story with epic_key = the FR Task key from step 5\n\n"
            "FINAL SLACK RESPONSE FORMAT for BRD breakdown (use this exact format):\n"
            "*✅ Jira Hierarchy Created*\n"
            "*Main Epic:* SCRUM-XX — <project name>\n\n"
            "*Epics created (X):*\n"
            "• SCRUM-XX — <FR name>\n"
            "• SCRUM-XX — <FR name>\n"
            "_(one line per epic)_\n\n"
            "*Tasks created (X):*\n"
            "• SCRUM-XX — <story title> _(under SCRUM-XX)_\n"
            "_(one line per task)_\n\n"
            "For ticket listings: • *KEY* — Summary `[Status]`\n"
            "NEVER output just 'Done' or 'Completed' — always show the full structured block above."
            + registry_context
        )

        # AIAgent.run_conversation() is synchronous — run in executor to avoid blocking async loop
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: agent.run_conversation(message, system_message, conversation_history=prev_messages),
        )

        log.info("[%s] Agent result keys: %s", correlation_id,
                 list(result.keys()) if isinstance(result, dict) else type(result))
        log.info("[%s] Agent final_response: %r", correlation_id,
                 result.get("final_response") if isinstance(result, dict) else result)

        response_text = result.get("final_response") or "I completed the task but had no output to show."
        tracker.set_skill(toolsets[0] if toolsets else "general")
        tracker.add_tokens(
            OPENROUTER_MODEL,
            result.get("prompt_tokens", 0),
            result.get("completion_tokens", 0),
        )

        # Hermes auto-persists this turn to state.db — no manual save needed
        log.info("[%s] Turn complete | thread=%s | Hermes saved to state.db",
                 correlation_id, thread_id)
        return response_text

    except Exception as e:
        success = False
        log.error("[%s] Agent error: %s", correlation_id, e, exc_info=True)
        tracker.set_skill("error")
        return f"Sorry, I encountered an error: {e}"

    finally:
        tracker.finish(success=success)
        tracker.emit()


# ── Reply helper ───────────────────────────────────────────────────────────────

def send_slack_reply(channel_id: str, thread_ts: str, text: str) -> None:
    slack.chat_postMessage(
        channel=channel_id,
        text=text,
        thread_ts=thread_ts or None,
    )


# ── Queue consumer ────────────────────────────────────────────────────────────

_LOCK_RENEWAL_INTERVAL = 30   # renew message lock every 30 s (default lock = 60 s)
_RECONNECT_DELAY       = 5    # seconds to wait before reconnecting after any error


async def _renew_lock_loop(receiver, msg) -> None:
    """Keep renewing the Service Bus message lock every 30 s until cancelled."""
    while True:
        await asyncio.sleep(_LOCK_RENEWAL_INTERVAL)
        try:
            await receiver.renew_message_lock(msg)
            log.debug("Lock renewed for message")
        except Exception as e:
            log.warning("Lock renewal failed: %s", e)
            return


async def _process_one(receiver, msg) -> None:
    """Process a single Service Bus message, renewing its lock in the background."""
    payload = json.loads(str(msg))
    correlation_id = payload.get("correlation_id", str(uuid.uuid4()))
    log.info(
        "[%s] Processing message from user=%s channel=%s",
        correlation_id, payload.get("user_email"), payload.get("channel_id"),
    )

    # Start background lock renewal so the message lock doesn't expire during LLM calls
    renew_task = asyncio.create_task(_renew_lock_loop(receiver, msg))
    try:
        response_text = await route_message(payload)
        send_slack_reply(
            channel_id=payload.get("channel_id", ""),
            thread_ts=payload.get("thread_ts", ""),
            text=response_text,
        )
        log.info("Replied to channel=%s", payload.get("channel_id"))
        await receiver.complete_message(msg)
    except Exception as e:
        log.error("Failed to process message: %s", e, exc_info=True)
        # Abandon so Azure retries, but only if under delivery count limit
        try:
            await receiver.abandon_message(msg)
        except Exception as ab_err:
            log.error("abandon_message failed: %s", ab_err)
    finally:
        renew_task.cancel()
        try:
            await renew_task
        except asyncio.CancelledError:
            pass


async def process_queue() -> None:
    log.info("Hermes Service started | queue=%s | model=%s", QUEUE_NAME, OPENROUTER_MODEL)
    while True:   # outer loop: reconnects on any Service Bus / network error
        try:
            async with ServiceBusClient.from_connection_string(CONN_STR) as client:
                async with client.get_queue_receiver(QUEUE_NAME, max_wait_time=5) as receiver:
                    log.info("Connected to Service Bus queue — listening…")
                    while True:
                        try:
                            messages = await receiver.receive_messages(
                                max_message_count=1, max_wait_time=5
                            )
                        except Exception as recv_err:
                            log.error("receive_messages error (will reconnect): %s", recv_err)
                            break   # break inner loop → re-enter outer → reconnect
                        for msg in messages:
                            await _process_one(receiver, msg)
        except Exception as conn_err:
            log.error("Service Bus connection error (will retry in %ds): %s",
                      _RECONNECT_DELAY, conn_err)
        log.info("Reconnecting to Service Bus in %d s…", _RECONNECT_DELAY)
        await asyncio.sleep(_RECONNECT_DELAY)


if __name__ == "__main__":
    asyncio.run(process_queue())
