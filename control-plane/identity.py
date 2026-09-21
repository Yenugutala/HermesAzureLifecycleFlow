"""
Identity resolution — maps Teams user email to Jira account ID.
"""
import os
import httpx
from typing import Optional

JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")

# Cache: email → jira_account_id
_cache: dict[str, str] = {}


async def resolve_jira_user(teams_email: str) -> Optional[str]:
    """Look up Jira account ID by email. Returns None if not found."""
    if not teams_email:
        return None

    if teams_email in _cache:
        return _cache[teams_email]

    url = f"{JIRA_BASE_URL}/rest/api/3/user/search"
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params={"query": teams_email}, auth=auth)
        if resp.status_code == 200:
            users = resp.json()
            if users:
                account_id = users[0].get("accountId")
                _cache[teams_email] = account_id
                return account_id

    return None
