"""
Developer Agent — detects missing agents in the registry, generates code via LLM,
and creates a GitHub PR to the develop branch.

All generated files are pushed directly to GitHub via the Trees API.
Nothing is written to the local filesystem.

Config is read at call time (inside _cfg()) so dotenv always wins.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import textwrap
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)


# ── Per-service URL patterns + response keys ────────────────────────────────────
# Path suffixes are appended to api_base_url.  {record_id} is filled at call time.
# 'create_wrapper' wraps the payload in a top-level key (e.g. Zendesk "ticket").
_SERVICE_URL_PATTERNS: dict[str, dict] = {
    "servicenow": {
        "resource":       "incident",
        "list_path":      "/incident?sysparm_limit=50&sysparm_display_value=true",
        "get_path":       "/incident/{record_id}",
        "create_path":    "/incident",
        "update_path":    "/incident/{record_id}",
        "close_path":     "/incident/{record_id}",
        "list_key":       "result",
        "get_key":        "result",
        "close_payload":  '{"state": "7", "close_code": "Resolved by Hermes", "close_notes": "Resolved via Hermes"}',
    },
    "databricks": {
        "resource":       "cluster",
        "list_path":      "/clusters/list",
        "get_path":       "/clusters/get",   # uses ?cluster_id= query param
        "list_key":       "clusters",
        "get_key":        "",                # root-level object
        "get_is_param":   True,
    },
    "sap": {
        "resource":       "businesspartner",
        "list_path":      "/A_BusinessPartner?$format=json&$top=50",
        "get_path":       "/A_BusinessPartner('{record_id}')?$format=json",
        "create_path":    "/A_BusinessPartner",
        "update_path":    "/A_BusinessPartner('{record_id}')",
        "list_key":       "d.results",
        "get_key":        "d",
    },
    "salesforce": {
        "resource":       "case",
        "list_path":      "/query?q=SELECT+Id,CaseNumber,Subject,Status,Priority+FROM+Case+LIMIT+50",
        "get_path":       "/sobjects/Case/{record_id}",
        "create_path":    "/sobjects/Case",
        "update_path":    "/sobjects/Case/{record_id}",
        "list_key":       "records",
        "get_key":        "",
    },
    "pagerduty": {
        "resource":       "incident",
        "list_path":      "/incidents",
        "get_path":       "/incidents/{record_id}",
        "create_path":    "/incidents",
        "update_path":    "/incidents/{record_id}",
        "list_key":       "incidents",
        "get_key":        "incident",
        "create_wrapper": "incident",
    },
    "zendesk": {
        "resource":       "ticket",
        "list_path":      "/tickets.json",
        "get_path":       "/tickets/{record_id}.json",
        "create_path":    "/tickets.json",
        "update_path":    "/tickets/{record_id}.json",
        "close_path":     "/tickets/{record_id}.json",
        "list_key":       "tickets",
        "get_key":        "ticket",
        "create_wrapper": "ticket",
        "close_payload":  '{"ticket": {"status": "solved"}}',
    },
}


# ── Config ─────────────────────────────────────────────────────────────────────

def _cfg() -> dict:
    """Read all config from env at call time — never cached at module level."""
    return {
        "github_token":   os.environ.get("GITHUB_TOKEN", ""),
        "github_owner":   os.environ.get("GITHUB_OWNER", ""),
        "github_repo":    os.environ.get("GITHUB_REPO", ""),
        "openrouter_key": os.environ.get("OPENROUTER_API_KEY", ""),
        "openrouter_model": os.environ.get("OPENROUTER_MODEL", "anthropic/claude-haiku-4-5"),
        "slack_token":    os.environ.get("SLACK_BOT_TOKEN", ""),
    }


def _gh_headers(cfg: dict) -> dict:
    return {
        "Authorization": f"Bearer {cfg['github_token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _username_from_email(email: str) -> str:
    """Extract a safe branch-name component from an email address."""
    local = email.split("@")[0] if "@" in email else email
    return re.sub(r"[^a-z0-9]", "", local.lower())[:20]


# ── 1. Analyze registry ─────────────────────────────────────────────────────────

def _sync_analyze(message: str, active: list[dict], pending: list[dict]) -> dict:
    """
    Decide whether the user's message matches any active or pending agent.
    Returns:
      {
        "matched_active": bool,
        "matched_active_name": str | None,
        "matched_pending": bool,
        "matched_pending_name": str | None,
        "pr_url": str | None,
        "pr_branch": str | None,
        "active_agents": [{"name", "display_name", "description"}],
        "pending_agents": [{"name", "display_name", "pr_url"}],
        "suggested_service_name": str,
      }
    """
    message_lower = message.lower()

    matched_active = None
    for agent in active:
        if agent["name"] in message_lower or agent["display_name"].lower() in message_lower:
            matched_active = agent
            break

    matched_pending = None
    for agent in pending:
        if agent["name"] in message_lower or agent["display_name"].lower() in message_lower:
            matched_pending = agent
            break

    # Guess the service name from the message (first unknown noun-like word)
    stop_words = {"show", "list", "get", "create", "find", "fetch", "me", "the", "a", "an",
                  "all", "open", "closed", "my", "our", "team", "please", "can", "you",
                  "incidents", "tickets", "issues", "records", "items", "data"}
    known = {a["name"] for a in active} | {a["name"] for a in pending}
    words = re.findall(r"[a-zA-Z]+", message_lower)
    suggested = next((w for w in words if w not in stop_words and w not in known
                      and len(w) > 3), "new_service")

    return {
        "matched_active":       matched_active is not None,
        "matched_active_name":  matched_active["display_name"] if matched_active else None,
        "matched_pending":      matched_pending is not None,
        "matched_pending_name": matched_pending["display_name"] if matched_pending else None,
        "pr_url":               matched_pending.get("pr_url") if matched_pending else None,
        "pr_branch":            matched_pending.get("pr_branch") if matched_pending else None,
        "active_agents":        active,
        "pending_agents":       pending,
        "suggested_service_name": suggested,
    }


async def analyze(message: str, active: list[dict], pending: list[dict]) -> dict:
    """Async wrapper — checks if message matches any registered or pending agent."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_analyze, message, active, pending)


# ── 2. Generate agent code via LLM ─────────────────────────────────────────────

_AGENT_TEMPLATE = textwrap.dedent("""\
    \"\"\"
    {display_name} Agent — generated by Hermes Developer Agent.
    Connects to {display_name} REST API.
    Config is read at call time so dotenv always wins.
    \"\"\"
    from __future__ import annotations

    import asyncio
    import logging
    import os
    import requests

    log = logging.getLogger(__name__)


    # ── Config ────────────────────────────────────────────────────────────────────

    def _cfg() -> dict:
        \"\"\"Read config from env at call time — never cached at module load.\"\"\"
        return {{
            "api_base_url": os.environ.get("{ENV_PREFIX}_BASE_URL", "{api_base_url}"),
            {auth_env_line}
        }}


    def _headers(cfg: dict) -> dict:
        {auth_header_code}


    # ── Sync HTTP calls ───────────────────────────────────────────────────────────

    {sync_functions}

    # ── Async wrappers ────────────────────────────────────────────────────────────

    {async_functions}
""")

_PLUGIN_TEMPLATE = textwrap.dedent("""\
    \"\"\"
    {display_name} plugin for Hermes Agent — generated by Hermes Developer Agent.
    Registers tools: {tool_list}
    \"\"\"
    from __future__ import annotations

    import asyncio
    import json
    import logging
    import os
    import sys

    log = logging.getLogger(__name__)


    # ── Tool schemas ──────────────────────────────────────────────────────────────

    {schemas}

    # ── Async → sync bridge (copy verbatim from any existing plugin) ──────────────

    def _run(coro):
        \"\"\"Run an async coroutine from a synchronous tool handler.\"\"\"
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


    # ── Tool handlers ─────────────────────────────────────────────────────────────

    {handlers}

    # ── Plugin entry point ────────────────────────────────────────────────────────

    def setup(context) -> None:
        \"\"\"Called by Hermes Agent plugin loader. Registers all {display_name} tools.\"\"\"
        {registrations}
        log.info("{display_name} plugin loaded: {tool_count} tool(s) registered")
""")


def _build_auth_fragments(auth_type: str, env_prefix: str) -> tuple[str, str]:
    """Return (env_line, header_code) for the chosen auth type."""
    if auth_type == "bearer":
        env_line = f'"token": os.environ.get("{env_prefix}_TOKEN", ""),'
        header_code = ('return {{\n'
                       '            "Authorization": f"Bearer {{cfg[\'token\']}}",\n'
                       '            "Content-Type": "application/json",\n'
                       '        }}')
    elif auth_type == "apikey":
        env_line = f'"api_key": os.environ.get("{env_prefix}_API_KEY", ""),'
        header_code = ('return {{\n'
                       '            "X-API-Key": cfg["api_key"],\n'
                       '            "Content-Type": "application/json",\n'
                       '        }}')
    elif auth_type == "basic":
        env_line = (f'"username": os.environ.get("{env_prefix}_USERNAME", ""),\n'
                    f'            "password": os.environ.get("{env_prefix}_PASSWORD", ""),')
        header_code = ('from base64 import b64encode\n'
                       '        creds = b64encode(f"{{cfg[\'username\']}}:{{cfg[\'password\']}}".encode()).decode()\n'
                       '        return {{\n'
                       '            "Authorization": f"Basic {{creds}}",\n'
                       '            "Content-Type": "application/json",\n'
                       '        }}')
    elif auth_type == "pagerduty_token":
        # PagerDuty: Authorization: Token token=<key>  +  PagerDuty Accept header
        env_line = f'"api_key": os.environ.get("{env_prefix}_API_KEY", ""),'
        header_code = ('return {{\n'
                       '            "Authorization": f"Token token={{cfg[\'api_key\']}}",\n'
                       '            "Content-Type": "application/json",\n'
                       '            "Accept": "application/vnd.pagerduty+json;version=2",\n'
                       '        }}')
    elif auth_type == "zendesk_token":
        # Zendesk API token: Basic base64(email/token:api_token)
        env_line = (f'"email": os.environ.get("{env_prefix}_EMAIL", ""),\n'
                    f'            "api_key": os.environ.get("{env_prefix}_API_KEY", ""),')
        header_code = ('from base64 import b64encode\n'
                       '        creds = b64encode(f"{{cfg[\'email\']}}/token:{{cfg[\'api_key\']}}".encode()).decode()\n'
                       '        return {{\n'
                       '            "Authorization": f"Basic {{creds}}",\n'
                       '            "Content-Type": "application/json",\n'
                       '        }}')
    else:  # oauth2
        env_line = (f'"client_id": os.environ.get("{env_prefix}_CLIENT_ID", ""),\n'
                    f'            "client_secret": os.environ.get("{env_prefix}_CLIENT_SECRET", ""),\n'
                    f'            "token_url": os.environ.get("{env_prefix}_TOKEN_URL", ""),')
        header_code = ('# Implement OAuth2 token fetch here\n'
                       '        return {{"Content-Type": "application/json"}}')
    return env_line, header_code


def _build_sync_functions(service_name: str, display_name: str, operations: list[str]) -> str:
    """Generate sync HTTP functions using per-service URL patterns where known, generic paths otherwise."""
    parts = []
    pat         = _SERVICE_URL_PATTERNS.get(service_name, {})
    record_name = pat.get("resource", service_name.rstrip("s"))
    list_key    = pat.get("list_key", "")
    get_key     = pat.get("get_key", "result")
    create_wrap = pat.get("create_wrapper", "")
    close_pl    = pat.get("close_payload", "")
    get_is_param = pat.get("get_is_param", False)

    # Path suffixes — fall back to simple REST convention for unknown services
    list_path   = pat.get("list_path",   f"/{record_name}s")
    get_path    = pat.get("get_path",    f"/{record_name}s/{{record_id}}")
    create_path = pat.get("create_path", f"/{record_name}s")
    update_path = pat.get("update_path", f"/{record_name}s/{{record_id}}")
    close_path  = pat.get("close_path",  f"/{record_name}s/{{record_id}}")

    # Response extraction expressions
    if list_key == "d.results":
        list_extract = 'data.get("d", {}).get("results", [])'
    elif list_key:
        list_extract = f'data.get("{list_key}", data if isinstance(data, list) else [])'
    else:
        list_extract = 'data if isinstance(data, list) else []'

    get_extract = f'data.get("{get_key}", data)' if get_key else 'data'

    if "list" in operations:
        parts.append(f'''\
def _sync_list_{record_name}s() -> list[dict]:
    cfg = _cfg()
    r = requests.get(
        f"{{cfg['api_base_url']}}{list_path}",
        headers=_headers(cfg),
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    return {list_extract}
''')

    if "get_by_id" in operations:
        if get_is_param:
            # Databricks-style: ID goes in a query param, not a path segment
            parts.append(f'''\
def _sync_get_{record_name}(record_id: str) -> dict | None:
    cfg = _cfg()
    r = requests.get(
        f"{{cfg['api_base_url']}}{get_path}",
        headers=_headers(cfg),
        params={{"cluster_id": record_id}},
        timeout=15,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()
''')
        else:
            parts.append(f'''\
def _sync_get_{record_name}(record_id: str) -> dict | None:
    cfg = _cfg()
    r = requests.get(
        f"{{cfg['api_base_url']}}{get_path}",
        headers=_headers(cfg),
        timeout=15,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    return {get_extract}
''')

    if "create" in operations:
        payload_expr = f'{{"{create_wrap}": payload}}' if create_wrap else 'payload'
        parts.append(f'''\
def _sync_create_{record_name}(payload: dict) -> dict:
    cfg = _cfg()
    r = requests.post(
        f"{{cfg['api_base_url']}}{create_path}",
        headers=_headers(cfg),
        json={payload_expr},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    return {get_extract}
''')

    if "update" in operations:
        parts.append(f'''\
def _sync_update_{record_name}(record_id: str, payload: dict) -> dict:
    cfg = _cfg()
    r = requests.patch(
        f"{{cfg['api_base_url']}}{update_path}",
        headers=_headers(cfg),
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    return {get_extract}
''')

    if "close" in operations:
        close_body = close_pl if close_pl else '{{"state": "closed", "resolution": "Resolved via Hermes"}}'
        parts.append(f'''\
def _sync_close_{record_name}(record_id: str) -> dict:
    cfg = _cfg()
    r = requests.patch(
        f"{{cfg['api_base_url']}}{close_path}",
        headers=_headers(cfg),
        json={close_body},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    return {get_extract}
''')

    return "\n".join(parts)


def _build_async_functions(service_name: str, operations: list[str]) -> str:
    """Generate async wrapper functions — uses per-service resource name from _SERVICE_URL_PATTERNS."""
    parts = []
    pat = _SERVICE_URL_PATTERNS.get(service_name, {})
    record_name = pat.get("resource", service_name.rstrip("s"))

    if "list" in operations:
        parts.append(f'''\
async def list_{record_name}s() -> list[dict]:
    """List all {record_name}s from {service_name}."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_list_{record_name}s)
''')
    if "get_by_id" in operations:
        parts.append(f'''\
async def get_{record_name}(record_id: str) -> dict | None:
    """Get a single {record_name} by ID."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_get_{record_name}, record_id)
''')
    if "create" in operations:
        parts.append(f'''\
async def create_{record_name}(payload: dict) -> dict:
    """Create a new {record_name}."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_create_{record_name}, payload)
''')
    if "update" in operations:
        parts.append(f'''\
async def update_{record_name}(record_id: str, payload: dict) -> dict:
    """Update an existing {record_name}."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_update_{record_name}, record_id, payload)
''')
    if "close" in operations:
        parts.append(f'''\
async def close_{record_name}(record_id: str) -> dict:
    """Close/resolve a {record_name}."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_close_{record_name}, record_id)
''')
    return "\n".join(parts)


def _build_plugin_schemas_and_handlers(
        service_name: str, display_name: str, operations: list[str]
) -> tuple[str, str, str, str]:
    """Return (schemas_code, handlers_code, registrations_code, tool_list_str)."""
    pat = _SERVICE_URL_PATTERNS.get(service_name, {})
    record_name = pat.get("resource", service_name.rstrip("s"))
    schemas, handlers, regs, tool_names = [], [], [], []

    if "list" in operations:
        tname = f"{service_name}_list_{record_name}s"
        tool_names.append(tname)
        schemas.append(f'''\
{tname.upper()}_SCHEMA = {{
    "type": "object",
    "properties": {{}},
    "required": [],
    "description": "List all {record_name}s from {display_name}. Returns a list of records.",
}}''')
        handlers.append(f'''\
def _{tname}_handler(params: dict, **kwargs) -> str:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from agents.{service_name}_agent import list_{record_name}s
        records = _run(list_{record_name}s())
        log.info("{tname}: returned %d records", len(records))
        return json.dumps({{"records": records, "count": len(records)}})
    except Exception as e:
        log.error("{tname} error: %s", e)
        return json.dumps({{"error": str(e)}})''')
        regs.append(f'context.register_tool(name="{tname}", handler=_{tname}_handler, schema={tname.upper()}_SCHEMA)')

    if "get_by_id" in operations:
        tname = f"{service_name}_get_{record_name}"
        tool_names.append(tname)
        schemas.append(f'''\
{tname.upper()}_SCHEMA = {{
    "type": "object",
    "properties": {{
        "record_id": {{"type": "string", "description": "The ID of the {record_name} to retrieve."}},
    }},
    "required": ["record_id"],
    "description": "Get a single {display_name} {record_name} by its ID.",
}}''')
        handlers.append(f'''\
def _{tname}_handler(params: dict, **kwargs) -> str:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from agents.{service_name}_agent import get_{record_name}
        record_id = params.get("record_id", "").strip()
        if not record_id:
            return json.dumps({{"error": "record_id is required"}})
        record = _run(get_{record_name}(record_id))
        if record is None:
            return json.dumps({{"error": f"{record_name} {{record_id}} not found"}})
        return json.dumps(record)
    except Exception as e:
        log.error("{tname} error: %s", e)
        return json.dumps({{"error": str(e)}})''')
        regs.append(f'context.register_tool(name="{tname}", handler=_{tname}_handler, schema={tname.upper()}_SCHEMA)')

    if "create" in operations:
        tname = f"{service_name}_create_{record_name}"
        tool_names.append(tname)
        schemas.append(f'''\
{tname.upper()}_SCHEMA = {{
    "type": "object",
    "properties": {{
        "payload": {{"type": "object", "description": "Fields for the new {record_name}."}},
    }},
    "required": ["payload"],
    "description": "Create a new {record_name} in {display_name}.",
}}''')
        handlers.append(f'''\
def _{tname}_handler(params: dict, **kwargs) -> str:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from agents.{service_name}_agent import create_{record_name}
        payload = params.get("payload", {{}})
        result = _run(create_{record_name}(payload))
        return json.dumps(result)
    except Exception as e:
        log.error("{tname} error: %s", e)
        return json.dumps({{"error": str(e)}})''')
        regs.append(f'context.register_tool(name="{tname}", handler=_{tname}_handler, schema={tname.upper()}_SCHEMA)')

    return (
        "\n\n".join(schemas),
        "\n\n".join(handlers),
        "\n    ".join(regs),
        ", ".join(tool_names),
    )


def _sync_generate_agent_code(
        service_name: str, display_name: str, api_base_url: str,
        auth_type: str, operations: list[str], capability_name: str,
) -> dict[str, str]:
    """Generate all 5 files as {filename: content} strings."""
    env_prefix = service_name.upper().replace("-", "_")
    auth_env_line, auth_header_code = _build_auth_fragments(auth_type, env_prefix)

    sync_fns  = _build_sync_functions(service_name, display_name, operations)
    async_fns = _build_async_functions(service_name, operations)

    agent_code = _AGENT_TEMPLATE.format(
        display_name=display_name,
        api_base_url=api_base_url,
        ENV_PREFIX=env_prefix,
        auth_env_line=auth_env_line,
        auth_header_code=auth_header_code,
        sync_functions=sync_fns,
        async_functions=async_fns,
    )

    schemas_code, handlers_code, regs_code, tool_list = _build_plugin_schemas_and_handlers(
        service_name, display_name, operations
    )

    plugin_code = _PLUGIN_TEMPLATE.format(
        display_name=display_name,
        tool_list=tool_list,
        schemas=schemas_code,
        handlers=handlers_code,
        registrations=regs_code,
        tool_count=len(operations),
    )

    pat = _SERVICE_URL_PATTERNS.get(service_name, {})
    record_name = pat.get("resource", service_name.rstrip("s"))
    tool_names = [t.split("_", 1)[1] if "_" in t else t
                  for t in tool_list.split(", ")]

    hermes_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # ── Patch hermes.yaml ──────────────────────────────────────────────────────
    hermes_yaml_path = os.path.join(hermes_dir, "hermes.yaml")
    with open(hermes_yaml_path, "r", encoding="utf-8") as _fh:
        raw_yaml = _fh.read()
    plugin_line = f"  - path: plugins/{service_name}_plugin.py\n"
    if plugin_line not in raw_yaml:
        raw_yaml = raw_yaml.replace(
            "  - path: plugins/developer_plugin.py",
            f"  - path: plugins/developer_plugin.py\n{plugin_line.rstrip()}"
        )
    toolset_block = f"  {service_name}:\n" + "".join(f"    - {t}\n" for t in tool_list.split(", "))
    if f"  {service_name}:" not in raw_yaml:
        raw_yaml = raw_yaml.rstrip("\n") + "\n" + toolset_block
    patched_hermes_yaml = raw_yaml

    # ── Patch main.py ──────────────────────────────────────────────────────────
    main_py_path = os.path.join(hermes_dir, "main.py")
    with open(main_py_path, "r", encoding="utf-8") as _fh:
        raw_main = _fh.read()
    new_cap_entry = f'    "{capability_name}": ["{service_name}"],\n'
    if new_cap_entry not in raw_main and '"agent_factory"' in raw_main:
        raw_main = raw_main.replace(
            '    "agent_factory":',
            f'{new_cap_entry}    "agent_factory":'
        )
    patched_main = raw_main

    # ── Patch agent_registry.json ──────────────────────────────────────────────
    registry_path = os.path.join(os.path.dirname(hermes_dir), "control-plane", "agent_registry.json")
    with open(registry_path, "r", encoding="utf-8") as _fh:
        registry = json.load(_fh)
    new_agent_entry = {
        "name": service_name,
        "display_name": display_name,
        "description": f"Interact with {display_name} — {', '.join(operations)}",
        "capabilities": [capability_name],
        "toolsets": [service_name],
        "status": "active",
    }
    if not any(a["name"] == service_name for a in registry.get("agents", [])):
        registry.setdefault("agents", []).append(new_agent_entry)
    patched_registry = json.dumps(registry, indent=2)

    return {
        f"hermes-service/agents/{service_name}_agent.py": agent_code,
        f"hermes-service/plugins/{service_name}_plugin.py": plugin_code,
        "hermes-service/hermes.yaml": patched_hermes_yaml,
        "hermes-service/main.py": patched_main,
        "control-plane/agent_registry.json": patched_registry,
    }


async def generate_agent_code(
        service_name: str, display_name: str, api_base_url: str,
        auth_type: str, operations: list[str], capability_name: str,
) -> dict[str, str]:
    """Async wrapper — generates all 5 agent files as filename→content dict."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _sync_generate_agent_code,
        service_name, display_name, api_base_url, auth_type, operations, capability_name,
    )


# ── 3. Push files to GitHub and create PR ──────────────────────────────────────

def _sync_push_pr_to_github(
        user_email: str, service_name: str, display_name: str,
        files: dict[str, str], capability_name: str,
) -> str:
    """
    Creates a feature branch from develop, commits all generated files,
    opens a PR targeting develop, and updates pending_agents.json.
    Returns the PR URL.
    """
    cfg = _cfg()
    gh = _gh_headers(cfg)
    owner = cfg["github_owner"]
    repo  = cfg["github_repo"]
    base_url = f"https://api.github.com/repos/{owner}/{repo}"

    username  = _username_from_email(user_email)
    date_str  = datetime.now(timezone.utc).strftime("%Y%m%d")
    branch    = f"feature_{username}_{service_name}_{date_str}"

    # 1. Get base SHA from develop branch (fall back to main)
    r = requests.get(f"{base_url}/git/ref/heads/develop", headers=gh, timeout=15)
    if r.status_code == 404:
        r = requests.get(f"{base_url}/git/ref/heads/main", headers=gh, timeout=15)
    r.raise_for_status()
    base_sha = r.json()["object"]["sha"]

    # 2. Get the tree SHA of the base commit
    r = requests.get(f"{base_url}/git/commits/{base_sha}", headers=gh, timeout=15)
    r.raise_for_status()
    base_tree_sha = r.json()["tree"]["sha"]

    # 3. Create a new tree with all generated + patched files
    tree_items = [
        {"path": path, "mode": "100644", "type": "blob",
         "content": content}
        for path, content in files.items()
    ]
    r = requests.post(f"{base_url}/git/trees", headers=gh, timeout=30,
                      json={"base_tree": base_tree_sha, "tree": tree_items})
    r.raise_for_status()
    new_tree_sha = r.json()["sha"]

    # 4. Create commit
    commit_message = f"feat: add {display_name} agent (generated by Hermes Developer Agent)"
    r = requests.post(f"{base_url}/git/commits", headers=gh, timeout=15,
                      json={"message": commit_message, "tree": new_tree_sha,
                            "parents": [base_sha]})
    r.raise_for_status()
    new_commit_sha = r.json()["sha"]

    # 5. Create branch (or update if it already exists from a previous failed attempt)
    r = requests.post(f"{base_url}/git/refs", headers=gh, timeout=15,
                      json={"ref": f"refs/heads/{branch}", "sha": new_commit_sha})
    if r.status_code in (422, 409):
        # Branch already exists — force-update it
        r = requests.patch(f"{base_url}/git/refs/heads/{branch}", headers=gh, timeout=15,
                           json={"sha": new_commit_sha, "force": True})
    r.raise_for_status()

    # 6. Open PR against develop
    pr_body = _build_pr_body(
        display_name=display_name,
        service_name=service_name,
        user_email=user_email,
        branch=branch,
        capability_name=capability_name,
        files=list(files.keys()),
    )
    r = requests.post(f"{base_url}/pulls", headers=gh, timeout=15,
                      json={"title": f"feat: Add {display_name} agent",
                            "head": branch, "base": "develop", "body": pr_body})
    if r.status_code == 422:
        err_msg = r.json().get("errors", [{}])
        already_exists = any("pull request already exists" in str(e).lower() for e in err_msg)
        if already_exists:
            # PR already open — find it
            search = requests.get(f"{base_url}/pulls", headers=gh, timeout=15,
                                  params={"head": f"{owner}:{branch}", "state": "open"})
            prs = search.json() if search.ok else []
            if prs:
                pr_data = prs[0]
            else:
                # fall back to main
                r = requests.post(f"{base_url}/pulls", headers=gh, timeout=15,
                                  json={"title": f"feat: Add {display_name} agent",
                                        "head": branch, "base": "main", "body": pr_body})
                r.raise_for_status()
                pr_data = r.json()
        else:
            # develop branch doesn't exist — try main
            r = requests.post(f"{base_url}/pulls", headers=gh, timeout=15,
                              json={"title": f"feat: Add {display_name} agent",
                                    "head": branch, "base": "main", "body": pr_body})
            r.raise_for_status()
            pr_data = r.json()
    else:
        r.raise_for_status()
        pr_data = r.json()
    pr_url    = pr_data["html_url"]
    pr_number = pr_data["number"]

    log.info("PR #%d created: %s", pr_number, pr_url)

    # 7. Update pending_agents.json on develop branch
    try:
        import base64
        pending_path = "control-plane/pending_agents.json"
        r = requests.get(f"{base_url}/contents/{pending_path}",
                         headers=gh, params={"ref": "develop"}, timeout=15)
        if r.status_code == 200:
            file_data    = r.json()
            file_sha     = file_data["sha"]
            current_json = json.loads(base64.b64decode(file_data["content"]).decode())
        else:
            file_sha     = None
            current_json = {"pending": []}

        current_json["pending"].append({
            "name":         service_name,
            "display_name": display_name,
            "pr_url":       pr_url,
            "pr_branch":    branch,
            "pr_number":    pr_number,
            "requested_by": user_email,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        })

        put_body = {
            "message": f"chore: mark {service_name} as pending (PR #{pr_number})",
            "content": base64.b64encode(
                json.dumps(current_json, indent=2).encode()
            ).decode(),
            "branch": "develop",
        }
        if file_sha:
            put_body["sha"] = file_sha

        requests.put(f"{base_url}/contents/{pending_path}",
                     headers=gh, json=put_body, timeout=15)
        log.info("pending_agents.json updated with %s", service_name)
    except Exception as e:
        log.warning("Could not update pending_agents.json: %s", e)

    return pr_url


def _build_pr_body(display_name: str, service_name: str, user_email: str,
                    branch: str, capability_name: str, files: list[str]) -> str:
    file_list = "\n".join(f"- `{f}`" for f in files)
    return f"""\
## New Agent: {display_name}

**Requested by**: {user_email} via Slack
**Branch**: `{branch}` → `develop`
**Generated by**: Hermes Developer Agent

### Files added
{file_list}

> `hermes_yaml_patch.txt` contains the lines to manually apply to `hermes.yaml` after review.
> `hermes-service/main.py` has been updated automatically with the new capability map entry.

### Engineer review checklist
- [ ] All env vars read inside `_cfg()` — none hardcoded or at module level
- [ ] `_run()` helper copied verbatim — not rewritten
- [ ] All handlers return `json.dumps({{...}})` — no bare raises
- [ ] Agent functions imported inside handler body, not at top of file
- [ ] Tool schema `description` is precise enough for the agent to call correctly
- [ ] `{capability_name}` added to correct roles in `control-plane/capabilities.py`
- [ ] New env vars documented in `.env.example` and `infra/params/prod.bicepparam`
- [ ] Remove `{service_name}` from `control-plane/pending_agents.json` after merging

### Required environment variables
See `hermes-service/main_py_patch.txt` for the full list of new env vars to add.

### To test after merging to develop
Use Service Bus Explorer to send this payload:
```json
{{
  "platform": "slack",
  "user_email": "{user_email}",
  "user_role": "admin",
  "capabilities": ["{capability_name}"],
  "message": "list {service_name} records",
  "channel_id": "C999TEST",
  "thread_ts": "1700000000.000000",
  "correlation_id": "test-{service_name}-001"
}}
```
"""


async def push_pr_to_github(
        user_email: str, service_name: str, display_name: str,
        files: dict[str, str], capability_name: str,
) -> str:
    """Async wrapper — creates branch, commits files, opens PR. Returns PR URL."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _sync_push_pr_to_github,
        user_email, service_name, display_name, files, capability_name,
    )


# ── 4. Post Block Kit form to Slack ────────────────────────────────────────────

def _sync_post_form_to_slack(
        channel_id: str, thread_ts: str,
        service_name: str, active_agents: list[dict],
) -> None:
    """Post a Slack Block Kit message with a 'Configure & Create Agent' button."""
    cfg = _cfg()
    display_name  = service_name.replace("_", " ").title()
    active_names  = ", ".join(a["display_name"] for a in active_agents) or "none"
    button_value  = json.dumps({
        "service_name": service_name,
        "channel_id":   channel_id,
        "thread_ts":    thread_ts,
    })

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":mag: *No {display_name} agent found in the registry.*\n\n"
                    f"*Available agents:* {active_names}\n\n"
                    f"Would you like me to create a *{display_name}* integration?"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Configure & Create Agent"},
                    "style": "primary",
                    "action_id": "open_agent_form",
                    "value": button_value,
                }
            ],
        },
    ]

    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {cfg['slack_token']}",
                 "Content-Type": "application/json"},
        json={"channel": channel_id, "thread_ts": thread_ts, "blocks": blocks},
        timeout=10,
    )
    data = resp.json()
    if not data.get("ok"):
        log.error("Slack postMessage failed: %s", data.get("error"))


async def post_form_to_slack(
        channel_id: str, thread_ts: str,
        service_name: str, active_agents: list[dict],
) -> None:
    """Async wrapper — posts Block Kit form message to Slack thread."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, _sync_post_form_to_slack,
        channel_id, thread_ts, service_name, active_agents,
    )
