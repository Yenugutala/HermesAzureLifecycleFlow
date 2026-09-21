"""
Jira plugin for Nous Research Hermes Agent.
Registers 4 Jira tools: list_tickets, get_ticket, create_epic, create_story.

Rules enforced by Hermes:
- Handlers must return JSON strings (json.dumps), never raw dicts.
- Errors must be returned as {"error": "..."}, never raised.
- Each handler is called synchronously by the Hermes tool loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import os

log = logging.getLogger(__name__)

# ── Tool schemas ──────────────────────────────────────────────────────────────

LIST_TICKETS_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "description": "List all open Jira tickets in the project. Returns key, summary, status, priority, and assignee.",
}

GET_TICKET_SCHEMA = {
    "type": "object",
    "properties": {
        "ticket_key": {
            "type": "string",
            "description": "Jira ticket key, e.g. SCRUM-5",
        }
    },
    "required": ["ticket_key"],
    "description": "Get full details of a specific Jira ticket by its key.",
}

CREATE_EPIC_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One-line title for the epic.",
        },
        "description": {
            "type": "string",
            "description": "Detailed description of the epic scope.",
        },
    },
    "required": ["summary", "description"],
    "description": "Create a new Epic in Jira. Returns the new ticket key.",
}

CREATE_STORY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One-line title for the user story.",
        },
        "description": {
            "type": "string",
            "description": "Detailed description including acceptance criteria.",
        },
        "epic_key": {
            "type": "string",
            "description": "Parent Epic key this story belongs to, e.g. SCRUM-10.",
        },
    },
    "required": ["summary", "description", "epic_key"],
    "description": "Create a new Story in Jira linked to a parent Epic. Returns the new ticket key.",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(coro):
    """Run an async coroutine from synchronous tool handler."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If already inside an event loop (e.g. asyncio queue consumer),
            # use a new thread-based loop to avoid nesting.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


# ── Tool handlers ─────────────────────────────────────────────────────────────

def _list_tickets_handler(params: dict, **kwargs) -> str:
    try:
        # Import here so dotenv is already loaded when this runs
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from agents.jira_agent import list_open_tickets

        tickets = _run(list_open_tickets())
        result = [
            {
                "key": t.key,
                "summary": t.summary,
                "status": t.status,
                "priority": t.priority,
                "assignee": t.assignee,
            }
            for t in tickets
        ]
        log.info("jira_list_tickets: returned %d tickets", len(result))
        return json.dumps({"tickets": result, "count": len(result)})
    except Exception as e:
        log.error("jira_list_tickets error: %s", e)
        return json.dumps({"error": str(e)})


def _get_ticket_handler(params: dict, **kwargs) -> str:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from agents.jira_agent import get_ticket

        key = params.get("ticket_key", "").strip().upper()
        if not key:
            return json.dumps({"error": "ticket_key is required"})

        ticket = _run(get_ticket(key))
        if ticket is None:
            return json.dumps({"error": f"Ticket {key} not found"})

        log.info("jira_get_ticket: fetched %s", key)
        return json.dumps({
            "key": ticket.key,
            "summary": ticket.summary,
            "description": ticket.description,
            "status": ticket.status,
            "priority": ticket.priority,
            "assignee": ticket.assignee,
            "labels": ticket.labels,
        })
    except Exception as e:
        log.error("jira_get_ticket error: %s", e)
        return json.dumps({"error": str(e)})


def _create_epic_handler(params: dict, **kwargs) -> str:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from agents.jira_agent import create_epic

        summary = params.get("summary", "").strip()
        description = params.get("description", "").strip()
        if not summary:
            return json.dumps({"error": "summary is required"})

        key = _run(create_epic(summary, description))
        log.info("jira_create_epic: created %s", key)
        return json.dumps({"key": key, "summary": summary, "type": "Epic"})
    except Exception as e:
        log.error("jira_create_epic error: %s", e)
        return json.dumps({"error": str(e)})


def _create_story_handler(params: dict, **kwargs) -> str:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from agents.jira_agent import create_story

        summary = params.get("summary", "").strip()
        description = params.get("description", "").strip()
        epic_key = params.get("epic_key", "").strip().upper()
        if not summary or not epic_key:
            return json.dumps({"error": "summary and epic_key are required"})

        key = _run(create_story(summary, description, epic_key))
        log.info("jira_create_story: created %s under %s", key, epic_key)
        return json.dumps({"key": key, "summary": summary, "epic_key": epic_key, "type": "Story"})
    except Exception as e:
        log.error("jira_create_story error: %s", e)
        return json.dumps({"error": str(e)})


# ── Plugin entry point ────────────────────────────────────────────────────────

def setup(context) -> None:
    """Called by Hermes Agent plugin loader. Registers all Jira tools."""
    context.register_tool(
        name="jira_list_tickets",
        handler=_list_tickets_handler,
        schema=LIST_TICKETS_SCHEMA,
    )
    context.register_tool(
        name="jira_get_ticket",
        handler=_get_ticket_handler,
        schema=GET_TICKET_SCHEMA,
    )
    context.register_tool(
        name="jira_create_epic",
        handler=_create_epic_handler,
        schema=CREATE_EPIC_SCHEMA,
    )
    context.register_tool(
        name="jira_create_story",
        handler=_create_story_handler,
        schema=CREATE_STORY_SCHEMA,
    )
    log.info("Jira plugin loaded: 4 tools registered")
