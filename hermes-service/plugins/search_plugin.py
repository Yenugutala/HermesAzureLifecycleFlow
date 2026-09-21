"""
Search plugin for Nous Research Hermes Agent.
Registers confluence_find_relevant — semantic vector search over indexed Confluence pages.

Handler rules (same as confluence_plugin.py):
- Must return JSON string (json.dumps), never a raw dict.
- Errors returned as {"error": "..."}, never raised.
- Handler is called synchronously by the Hermes tool loop.
"""
from __future__ import annotations

import json
import logging
import os
import sys

log = logging.getLogger(__name__)

# ── Tool schema ────────────────────────────────────────────────────────────────

CONFLUENCE_FIND_RELEVANT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Natural language description of what you are looking for in Confluence. "
                "Examples: 'BRD for securities master data', 'trade promotion requirements', "
                "'securities master specification'. "
                "Extract meaningful keywords from the user request or Jira ticket description."
            ),
        },
    },
    "required": ["query"],
    "description": (
        "Semantically search pre-indexed Confluence pages to automatically find the most "
        "relevant BRD or specification page. Returns the top-3 matching pages with titles, "
        "URLs, and content previews. "
        "Use this FIRST when breaking a BRD into stories — it finds the right page "
        "automatically so you never need to ask the user which Confluence page to use. "
        "After finding the page, call confluence_get_page with the best matching page_title."
    ),
}


# ── Handler ────────────────────────────────────────────────────────────────────

def _confluence_find_relevant_handler(params: dict, **kwargs) -> str:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        import vector_store
        from sentence_transformers import SentenceTransformer

        query = params.get("query", "").strip()
        if not query:
            return json.dumps({"error": "query is required"})

        # SentenceTransformer caches the model after first load — subsequent calls are fast
        model = SentenceTransformer("all-MiniLM-L6-v2")
        embedding = model.encode([query])[0].tolist()

        results = vector_store.search(embedding, top_k=3)

        if not results:
            return json.dumps({
                "pages": [],
                "note": (
                    "No indexed Confluence pages found. "
                    "Run 'python scripts/build_index.py' to index the Confluence space, "
                    "then try confluence_search as a fallback."
                ),
            })

        log.info("confluence_find_relevant: query=%r → %d results (top score=%.3f)",
                 query, len(results), results[0]["score"])
        return json.dumps({"pages": results})

    except Exception as e:
        log.error("confluence_find_relevant error: %s", e)
        return json.dumps({"error": str(e)})


# ── Plugin entry point ─────────────────────────────────────────────────────────

def setup(context) -> None:
    """Called by Hermes Agent plugin loader. Registers semantic search tool."""
    context.register_tool(
        name="confluence_find_relevant",
        handler=_confluence_find_relevant_handler,
        schema=CONFLUENCE_FIND_RELEVANT_SCHEMA,
    )
    log.info("Search plugin loaded: 1 tool registered (confluence_find_relevant)")
