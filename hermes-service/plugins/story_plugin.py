"""
Story plugin for Nous Research Hermes Agent.
Registers the break_brd_into_stories tool that uses OpenRouter LLM
to decompose a BRD text into Jira epics and user stories.

Rules enforced by Hermes:
- Handlers must return JSON strings (json.dumps), never raw dicts.
- Errors must be returned as {"error": "..."}, never raised.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import os

log = logging.getLogger(__name__)

# ── Tool schema ───────────────────────────────────────────────────────────────

BRD_SCHEMA = {
    "type": "object",
    "properties": {
        "brd_text": {
            "type": "string",
            "description": (
                "Full text content of the Business Requirements Document (BRD). "
                "The tool will extract epics and user stories from this text."
            ),
        }
    },
    "required": ["brd_text"],
    "description": (
        "Analyze a BRD document and extract a structured list of epics and user stories. "
        "Returns JSON with epics, each containing stories with acceptance criteria."
    ),
}


# ── Helper ────────────────────────────────────────────────────────────────────

def _run(coro):
    """Run an async coroutine from synchronous tool handler."""
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


# ── Tool handler ──────────────────────────────────────────────────────────────

def _brd_handler(params: dict, **kwargs) -> str:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from agents.story_agent import break_brd_into_stories

        brd_text = params.get("brd_text", "").strip()
        if not brd_text:
            return json.dumps({"error": "brd_text is required"})

        # story_agent now returns (epics, input_tokens, output_tokens)
        epics, input_tokens, output_tokens = _run(break_brd_into_stories(brd_text))

        epic_count = len(epics)
        story_count = sum(len(e.get("stories", [])) for e in epics)

        log.info(
            "break_brd_into_stories: %d epics, %d stories, tokens in=%d out=%d",
            epic_count, story_count, input_tokens, output_tokens,
        )

        return json.dumps({
            "epics": epics,
            "epic_count": epic_count,
            "story_count": story_count,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        })
    except Exception as e:
        log.error("break_brd_into_stories error: %s", e)
        return json.dumps({"error": str(e)})


# ── Plugin entry point ────────────────────────────────────────────────────────

def setup(context) -> None:
    """Called by Hermes Agent plugin loader. Registers the BRD story tool."""
    context.register_tool(
        name="break_brd_into_stories",
        handler=_brd_handler,
        schema=BRD_SCHEMA,
    )
    log.info("Story plugin loaded: 1 tool registered")
