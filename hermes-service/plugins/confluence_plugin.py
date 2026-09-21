"""
Confluence plugin for Nous Research Hermes Agent.
Registers two tools: confluence_search and confluence_get_page.

Handler rules (same as jira_plugin.py):
- Must return JSON string (json.dumps), never a raw dict.
- Errors returned as {"error": "..."}, never raised.
- Handler is called synchronously by the Hermes tool loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

log = logging.getLogger(__name__)

# ── Tool schemas ───────────────────────────────────────────────────────────────

CONFLUENCE_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Keywords to search for in Confluence "
                "(e.g. 'Trade Promotion SC Johnson BRD'). "
                "Extract meaningful keywords from the Jira ticket description."
            ),
        },
        "space_key": {
            "type": "string",
            "description": "Confluence space key to search within (e.g. 'BA'). Optional.",
        },
    },
    "required": ["query"],
    "description": (
        "Search Confluence pages by keyword using CQL full-text search. "
        "Returns a list of matching page titles and IDs. "
        "Use this FIRST to discover which BRD or requirements page is relevant, "
        "then call confluence_get_page with the best matching title."
    ),
}

CONFLUENCE_GET_PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "page_title": {
            "type": "string",
            "description": "Exact title of the Confluence page to fetch (e.g. 'AI-Powered Internal Developer Portal').",
        },
        "space_key": {
            "type": "string",
            "description": "Confluence space key (e.g. 'BA', 'DEV'). Optional — defaults to CONFLUENCE_SPACE_KEY env var.",
        },
    },
    "required": ["page_title"],
    "description": (
        "Fetch the full text content of a Confluence page by its title. "
        "Use this to read BRD documents, specifications, or any content stored in Confluence "
        "before analysing or breaking them into Jira epics and stories."
    ),
}


# ── Helper ─────────────────────────────────────────────────────────────────────

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

def _confluence_search_handler(params: dict, **kwargs) -> str:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from agents.confluence_agent import search_pages

        query = params.get("query", "").strip()
        space_key = params.get("space_key", "").strip()

        if not query:
            return json.dumps({"error": "query is required"})

        results = _run(search_pages(query, space_key))
        log.info("confluence_search: query=%r found %d results", query, len(results))
        return json.dumps({"results": results, "count": len(results)})

    except Exception as e:
        log.error("confluence_search error: %s", e)
        return json.dumps({"error": str(e)})


def _confluence_get_page_handler(params: dict, **kwargs) -> str:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from agents.confluence_agent import get_page

        title = params.get("page_title", "").strip()
        space_key = params.get("space_key", "").strip()

        if not title:
            return json.dumps({"error": "page_title is required"})

        content = _run(get_page(title, space_key))
        log.info("confluence_get_page: fetched page %r (%d chars)", title, len(content))
        return json.dumps({"content": content, "page_title": title})

    except Exception as e:
        log.error("confluence_get_page error: %s", e)
        return json.dumps({"error": str(e)})


# ── Plugin entry point ─────────────────────────────────────────────────────────

def setup(context) -> None:
    """Called by Hermes Agent plugin loader. Registers Confluence tools."""
    context.register_tool(
        name="confluence_search",
        handler=_confluence_search_handler,
        schema=CONFLUENCE_SEARCH_SCHEMA,
    )
    context.register_tool(
        name="confluence_get_page",
        handler=_confluence_get_page_handler,
        schema=CONFLUENCE_GET_PAGE_SCHEMA,
    )
    log.info("Confluence plugin loaded: 2 tools registered")
