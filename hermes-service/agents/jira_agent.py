"""
Jira Agent — mirrors AILedSDLC/agentic/demo/tools/jira_client.py.
Uses sync `requests` (same library that works in AILedSDLC) wrapped in
run_in_executor so the async queue consumer can call it without blocking.
Config is read at call time (not module-import time) so dotenv always wins.
"""
from __future__ import annotations

import asyncio
import logging
import os
from base64 import b64encode
from dataclasses import dataclass, field
from typing import Optional

import requests
from requests.utils import quote

log = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────

def _cfg() -> dict:
    """Read Jira config from env at call time — never cached at module load."""
    cfg = {
        "base_url": os.environ.get("JIRA_BASE_URL", "").rstrip("/"),
        "email":    os.environ.get("JIRA_EMAIL", ""),
        "token":    os.environ.get("JIRA_API_TOKEN", ""),
        "project":  os.environ.get("JIRA_PROJECT_KEY", "SCRUM"),
    }
    log.info(
        "Jira cfg — base_url=%s  email=%s  token_len=%d  project=%s",
        cfg["base_url"], cfg["email"], len(cfg["token"]), cfg["project"],
    )
    return cfg


def _headers(cfg: dict) -> dict:
    creds = b64encode(f"{cfg['email']}:{cfg['token']}".encode()).decode()
    return {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class JiraTicket:
    key: str
    summary: str
    description: str
    status: str
    priority: str
    assignee: str = "Unassigned"
    labels: list = field(default_factory=list)


# ── Sync Jira calls (same pattern as AILedSDLC jira_client.py) ───────────────

def _sync_list_open_tickets() -> list[JiraTicket]:
    cfg = _cfg()
    jql = (
        f"project = {cfg['project']} "
        f"AND issuetype in (Epic, Task) "
        f"AND \"Parent\" is EMPTY "
        f"AND status != Done "
        f"ORDER BY created DESC"
    )
    url = (
        f"{cfg['base_url']}/rest/api/3/search/jql"
        f"?jql={quote(jql)}"
        f"&fields=summary,status,priority,assignee,description,labels"
        f"&maxResults=20"
    )
    r = requests.get(url, headers=_headers(cfg), timeout=15)
    log.info("Jira list_tickets — status=%s  body[:500]=%s", r.status_code, r.text[:500])
    r.raise_for_status()
    data = r.json()
    tickets: list[JiraTicket] = []
    for issue in data.get("issues", []):
        f = issue["fields"]
        desc_raw = f.get("description")
        desc_text = _adf_to_text(desc_raw) if isinstance(desc_raw, dict) else (desc_raw or "")
        tickets.append(JiraTicket(
            key=issue["key"],
            summary=f.get("summary", ""),
            description=desc_text,
            status=(f.get("status") or {}).get("name", ""),
            priority=(f.get("priority") or {}).get("name", "None"),
            assignee=((f.get("assignee") or {}).get("displayName") or "Unassigned"),
            labels=f.get("labels", []),
        ))
    return tickets


def _sync_get_ticket(key: str) -> Optional[JiraTicket]:
    cfg = _cfg()
    url = (
        f"{cfg['base_url']}/rest/api/3/issue/{key}"
        f"?fields=summary,status,priority,assignee,description,labels"
    )
    r = requests.get(url, headers=_headers(cfg), timeout=15)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    f = data["fields"]
    desc_raw = f.get("description")
    desc_text = _adf_to_text(desc_raw) if isinstance(desc_raw, dict) else (desc_raw or "")
    return JiraTicket(
        key=data["key"],
        summary=f.get("summary", ""),
        description=desc_text,
        status=(f.get("status") or {}).get("name", ""),
        priority=(f.get("priority") or {}).get("name", "None"),
        assignee=((f.get("assignee") or {}).get("displayName") or "Unassigned"),
        labels=f.get("labels", []),
    )


def _sync_create_issue(payload: dict) -> str:
    cfg = _cfg()
    r = requests.post(
        f"{cfg['base_url']}/rest/api/3/issue",
        headers=_headers(cfg),
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["key"]


# ── Async wrappers ────────────────────────────────────────────────────────────

async def list_open_tickets() -> list[JiraTicket]:
    """Fetch open tickets from Jira project (async wrapper around sync requests call)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_list_open_tickets)


async def get_ticket(key: str) -> Optional[JiraTicket]:
    """Get full ticket details by key."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_get_ticket, key)


async def create_epic(summary: str, description: str, parent_key: str = "") -> str:
    """Create an Epic (top-level) or a Task under a parent Epic. Returns the new issue key."""
    cfg = _cfg()
    fields: dict = {
        "project": {"key": cfg["project"]},
        "summary": summary,
        "description": {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": description}]}
        ]},
        # If a parent Epic is given, create a Task (child of Epic); otherwise create a top-level Epic
        "issuetype": {"name": "Task" if parent_key else "Epic"},
    }
    if parent_key:
        fields["parent"] = {"key": parent_key}
    payload = {"fields": fields}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_create_issue, payload)


async def create_story(summary: str, description: str, epic_key: str) -> str:
    """Create a Story linked to an Epic. Returns the new issue key."""
    cfg = _cfg()
    payload = {
        "fields": {
            "project": {"key": cfg["project"]},
            "summary": summary,
            "description": {"type": "doc", "version": 1, "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": description}]}
            ]},
            "issuetype": {"name": "Subtask"},
            "parent": {"key": epic_key},
        }
    }
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_create_issue, payload)


# ── ADF helpers (from AILedSDLC jira_client.py) ───────────────────────────────

def _adf_to_text(adf: dict) -> str:
    """Recursively extract plain text from Atlassian Document Format."""
    if not adf or not isinstance(adf, dict):
        return ""
    node_type = adf.get("type", "")
    parts: list[str] = []
    if node_type == "text":
        parts.append(adf.get("text", ""))
    for child in adf.get("content", []):
        parts.append(_adf_to_text(child))
    text = "".join(parts)
    if node_type in ("paragraph", "heading", "bulletList", "orderedList", "listItem", "tableRow"):
        text = text + "\n"
    if node_type in ("tableCell", "tableHeader"):
        text = text.strip() + " | "
    return text


# ── Formatter ──────────────────────────────────────────────────��──────────────

def format_tickets(tickets: list[JiraTicket]) -> str:
    if not tickets:
        return "No open tickets found."
    lines = ["*Open Tickets:*\n"]
    for t in tickets:
        lines.append(f"• *{t.key}* — {t.summary} `[{t.status}]` `{t.priority}`")
    return "\n".join(lines)
