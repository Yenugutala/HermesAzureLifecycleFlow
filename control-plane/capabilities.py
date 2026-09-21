"""
Authorized Capability Set — defines what each role can do.
Control Plane exposes GET /api/capabilities/{slack_user_id}
Hermes Service calls this before routing any message.
"""
from __future__ import annotations

# ── Role → Capability Set mapping ────────────────────────────────────────────
# Each capability name maps directly to a Hermes Agent registered in the registry

CAPABILITY_SETS: dict[str, list[str]] = {
    "admin": [
        "jira_lister",
        "jira_getter",
        "brd_story_creator",
        "deploy_pipeline",
    ],
    "engineer": [
        "jira_lister",
        "jira_getter",
        "brd_story_creator",
        "deploy_pipeline",
    ],
    "ba": [
        "jira_lister",
        "jira_getter",
        "brd_story_creator",
    ],
    "product_owner": [
        "jira_lister",
        "jira_getter",
    ],
    "viewer": [
        "jira_lister",
    ],
}

# ── User → Role mapping (demo: Slack user_id → role) ─────────────────────────
# In production: load from a database or Azure AD group membership
# For demo: map by email domain or explicit user list

def get_role_for_email(email: str) -> str:
    """
    Resolve user role from email address.
    Demo logic — replace with DB/AAD lookup in production.
    """
    if not email:
        return "viewer"

    email_lower = email.lower()

    # Admin: the Jira account owner
    if email_lower in ("yenugutala.kirankumar@gmail.com", "kyenugutala@outlook.com"):
        return "admin"

    # Engineers: @scjohnson.com tech team
    if email_lower.endswith("@scjohnson.com") and "eng" in email_lower:
        return "engineer"

    # BAs: business analysts
    if email_lower.endswith("@scjohnson.com"):
        return "ba"

    # Everyone else: viewer
    return "viewer"


def get_capabilities(email: str) -> dict:
    """
    Return the full capability response for a user.
    Called by Hermes Service before processing any message.
    """
    role = get_role_for_email(email)
    capabilities = CAPABILITY_SETS.get(role, CAPABILITY_SETS["viewer"])
    return {
        "email": email,
        "role": role,
        "capabilities": capabilities,
    }
