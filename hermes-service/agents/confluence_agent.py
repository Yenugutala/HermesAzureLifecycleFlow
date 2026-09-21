"""
Confluence Agent — fetches page content from Confluence Cloud.

Uses the same Atlassian host and Basic auth as jira_agent.py.
Confluence REST API path: /wiki/rest/api/content
Config read at call time (dotenv-safe, same as jira_agent pattern).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from base64 import b64encode

import requests

log = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────

def _cfg() -> dict:
    """Read Confluence config from env at call time — never cached at module load."""
    return {
        "base_url":  os.environ.get("JIRA_BASE_URL", "").rstrip("/"),  # same Atlassian host
        "email":     os.environ.get("JIRA_EMAIL", ""),
        "token":     os.environ.get("JIRA_API_TOKEN", ""),
        "space_key": os.environ.get("CONFLUENCE_SPACE_KEY", ""),
    }


def _headers(cfg: dict) -> dict:
    creds = b64encode(f"{cfg['email']}:{cfg['token']}".encode()).decode()
    return {
        "Authorization": f"Basic {creds}",
        "Accept": "application/json",
    }


# ── HTML → plain text ─────────────────────────────────────────────────────────

def _html_to_text(html: str) -> str:
    """Strip HTML and Confluence storage-format tags, return readable plain text."""
    # Extract CDATA content first (used inside ac:plain-text-body code blocks)
    cdata_parts = re.findall(r"<!\[CDATA\[(.*?)]]>", html, flags=re.DOTALL)
    if cdata_parts:
        return "\n\n".join(p.strip() for p in cdata_parts if p.strip())
    # Remove Confluence macro tags (ac:*, ri:*)
    text = re.sub(r"<ac:[^>]+>.*?</ac:[^>]+>", " ", html, flags=re.DOTALL)
    text = re.sub(r"<ac:[^>]+/>", " ", text)
    text = re.sub(r"<ri:[^>]+/>", " ", text)
    # Remove remaining HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Normalise whitespace
    text = re.sub(r"\s{2,}", "\n", text)
    return text.strip()


# ── Sync Confluence call ───────────────────────────────────────────────────────

def _sync_get_page(title: str, space_key: str) -> str:
    cfg = _cfg()
    space = space_key or cfg["space_key"]
    log.info("Confluence get_page — title=%r space=%r", title, space)

    r = requests.get(
        f"{cfg['base_url']}/wiki/rest/api/content",
        headers=_headers(cfg),
        params={
            "title":     title,
            "spaceKey":  space,
            "expand":    "body.storage",
            "limit":     1,
        },
        timeout=15,
    )
    log.info("Confluence response — status=%s", r.status_code)
    r.raise_for_status()

    results = r.json().get("results", [])
    if not results:
        return f"Page '{title}' not found in Confluence space '{space}'."

    storage_body = results[0]["body"]["storage"]["value"]
    return _html_to_text(storage_body)


# ── Sync search call ──────────────────────────────────────────────────────────

def _sync_search_pages(query: str, space_key: str, limit: int = 5) -> list:
    cfg = _cfg()
    space = space_key or cfg["space_key"]
    cql = f'type=page AND text~"{query}"'
    if space:
        cql += f' AND space="{space}"'
    log.info("Confluence search — cql=%r", cql)

    r = requests.get(
        f"{cfg['base_url']}/wiki/rest/api/content/search",
        headers=_headers(cfg),
        params={"cql": cql, "limit": limit, "expand": "space"},
        timeout=15,
    )
    log.info("Confluence search response — status=%s", r.status_code)
    r.raise_for_status()

    results = r.json().get("results", [])
    return [
        {
            "title": p["title"],
            "id": p["id"],
            "url": p.get("_links", {}).get("webui", ""),
        }
        for p in results
    ]


# ── Async wrappers ────────────────────────────────────────────────────────────

async def get_page(title: str, space_key: str = "") -> str:
    """Fetch a Confluence page by title and return its content as plain text."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_get_page, title, space_key)


async def search_pages(query: str, space_key: str = "", limit: int = 5) -> list:
    """Search Confluence pages by keyword using CQL. Returns list of {title, id, url}."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_search_pages, query, space_key, limit)
