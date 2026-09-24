"""
Developer plugin for Hermes Agent.
Registers 3 tools:
  developer_analyze      — checks if message matches any registered/pending agent
  developer_show_form    — posts Slack Block Kit message with "Configure & Create Agent" button
  developer_create_agent — generates code + pushes PR to GitHub

Rules enforced by Hermes:
- Handlers must return JSON strings (json.dumps), never raw dicts.
- Errors must be returned as {"error": "..."}, never raised.
- Each handler is called synchronously by the Hermes tool loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

log = logging.getLogger(__name__)


# ── Tool schemas ───────────────────────────────────────────────────────────────

ANALYZE_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {
            "type": "string",
            "description": "The user's original message to check against the registry.",
        },
        "active_registry": {
            "type": "string",
            "description": "JSON array of active agents from the registry payload.",
        },
        "pending_agents": {
            "type": "string",
            "description": "JSON array of pending agents (open PRs, not yet merged).",
        },
    },
    "required": ["message", "active_registry", "pending_agents"],
    "description": (
        "Check if the user's message matches any registered or pending agent. "
        "Returns match status, list of available agents, and a suggested service name. "
        "Call this FIRST whenever the request does not clearly match Jira, Confluence, Story, or SharePoint."
    ),
}

SHOW_FORM_SCHEMA = {
    "type": "object",
    "properties": {
        "service_name": {
            "type": "string",
            "description": "snake_case name of the missing service (e.g. servicenow, pagerduty).",
        },
        "channel_id": {
            "type": "string",
            "description": "Slack channel ID to post the form message into.",
        },
        "thread_ts": {
            "type": "string",
            "description": "Slack thread timestamp to reply in.",
        },
        "active_agents": {
            "type": "string",
            "description": "JSON array of currently active agents (for the availability message).",
        },
    },
    "required": ["service_name", "channel_id", "thread_ts", "active_agents"],
    "description": (
        "Post a Slack Block Kit message with a 'Configure & Create Agent' button. "
        "The button opens a modal form where the user fills in all agent details at once. "
        "Call this after developer_analyze confirms no active or pending agent exists."
    ),
}

CREATE_AGENT_SCHEMA = {
    "type": "object",
    "properties": {
        "service_name": {
            "type": "string",
            "description": "snake_case service identifier (e.g. servicenow, pagerduty).",
        },
        "display_name": {
            "type": "string",
            "description": "Human-readable service name (e.g. ServiceNow, PagerDuty).",
        },
        "api_base_url": {
            "type": "string",
            "description": "Base URL of the external API (e.g. https://company.service-now.com).",
        },
        "auth_type": {
            "type": "string",
            "enum": ["bearer", "apikey", "basic", "oauth2"],
            "description": "Authentication mechanism for the external API.",
        },
        "operations": {
            "type": "string",
            "description": "JSON array of operations to support: list, get_by_id, create, update, close.",
        },
        "capability_name": {
            "type": "string",
            "description": "Capability string for RBAC (e.g. servicenow_viewer).",
        },
        "user_email": {
            "type": "string",
            "description": "Email of the user who requested the agent (used in branch name and PR).",
        },
        "channel_id": {
            "type": "string",
            "description": "Slack channel ID (to post confirmation after PR is created).",
        },
        "thread_ts": {
            "type": "string",
            "description": "Slack thread timestamp (to post confirmation in the right thread).",
        },
    },
    "required": ["service_name", "display_name", "api_base_url", "auth_type",
                 "operations", "capability_name", "user_email", "channel_id", "thread_ts"],
    "description": (
        "Generate agent + plugin code from templates, push all files to a GitHub feature branch "
        "(feature_{username}_{service_name}_{date}), and open a PR against develop. "
        "Returns the PR URL. Called after the user submits the agent creation form."
    ),
}


# ── Async → sync bridge (copy verbatim from any existing plugin) ───────────────

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


# ── Tool handlers ──────────────────────────────────────────────────────────────

def _analyze_handler(params: dict, **kwargs) -> str:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from agents.developer_agent import analyze

        message        = params.get("message", "")
        active_registry = json.loads(params.get("active_registry", "[]"))
        pending_agents  = json.loads(params.get("pending_agents", "[]"))

        result = _run(analyze(message, active_registry, pending_agents))
        log.info("developer_analyze: matched_active=%s matched_pending=%s",
                 result.get("matched_active"), result.get("matched_pending"))
        return json.dumps(result)
    except Exception as e:
        log.error("developer_analyze error: %s", e)
        return json.dumps({"error": str(e)})


def _show_form_handler(params: dict, **kwargs) -> str:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from agents.developer_agent import post_form_to_slack

        service_name  = params.get("service_name", "new_service")
        channel_id    = params.get("channel_id", "")
        thread_ts     = params.get("thread_ts", "")
        active_agents = json.loads(params.get("active_agents", "[]"))

        if not channel_id:
            return json.dumps({"error": "channel_id is required to post the form"})

        _run(post_form_to_slack(channel_id, thread_ts, service_name, active_agents))
        log.info("developer_show_form: posted Block Kit form for service=%s", service_name)
        return json.dumps({
            "status": "form_posted",
            "message": (
                f"I've posted a configuration form in the Slack thread. "
                f"Click 'Configure & Create Agent' to fill in the details for the "
                f"{service_name.replace('_', ' ').title()} integration."
            ),
        })
    except Exception as e:
        log.error("developer_show_form error: %s", e)
        return json.dumps({"error": str(e)})


def _create_agent_handler(params: dict, **kwargs) -> str:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from agents.developer_agent import generate_agent_code, push_pr_to_github

        service_name    = params.get("service_name", "").strip().lower().replace(" ", "_")
        display_name    = params.get("display_name", service_name.title())
        api_base_url    = params.get("api_base_url", "").strip().rstrip("/")
        auth_type       = params.get("auth_type", "bearer")
        operations      = json.loads(params.get("operations", '["list"]'))
        capability_name = params.get("capability_name", f"{service_name}_viewer")
        user_email      = params.get("user_email", "unknown@unknown.com")
        channel_id      = params.get("channel_id", "")
        thread_ts       = params.get("thread_ts", "")

        if not service_name:
            return json.dumps({"error": "service_name is required"})
        if not api_base_url:
            return json.dumps({"error": "api_base_url is required"})

        log.info("developer_create_agent: generating %s agent (ops=%s)", service_name, operations)

        # Generate all 5 files
        files = _run(generate_agent_code(
            service_name, display_name, api_base_url,
            auth_type, operations, capability_name,
        ))

        # Push to GitHub and open PR
        pr_url = _run(push_pr_to_github(
            user_email, service_name, display_name, files, capability_name,
        ))

        log.info("developer_create_agent: PR created at %s", pr_url)
        return json.dumps({
            "status": "pr_created",
            "pr_url": pr_url,
            "service_name": service_name,
            "display_name": display_name,
            "files_generated": list(files.keys()),
            "message": (
                f"PR created for the {display_name} agent: {pr_url}\n"
                f"Branch targets `develop`. An engineer will review and merge to `main`.\n"
                f"Until merged, {display_name} requests will show as pending."
            ),
        })
    except Exception as e:
        log.error("developer_create_agent error: %s", e)
        return json.dumps({"error": str(e)})


# ── Plugin entry point ─────────────────────────────────────────────────────────

def setup(context) -> None:
    """Called by Hermes Agent plugin loader. Registers all Developer tools."""
    context.register_tool(
        name="developer_analyze",
        handler=_analyze_handler,
        schema=ANALYZE_SCHEMA,
    )
    context.register_tool(
        name="developer_show_form",
        handler=_show_form_handler,
        schema=SHOW_FORM_SCHEMA,
    )
    context.register_tool(
        name="developer_create_agent",
        handler=_create_agent_handler,
        schema=CREATE_AGENT_SCHEMA,
    )
    log.info("Developer plugin loaded: 3 tools registered")
