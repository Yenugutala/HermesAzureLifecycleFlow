# Adding a New Agent / Capability

This guide shows how to add a new use case to the Hermes platform in 5 steps.
No IDE required in production — push to `main` and CI/CD handles the rest.

## Example: Adding a "GitHub PR Reviewer" capability

### Step 1 — Write the Plugin

Create `hermes-service/plugins/github_plugin.py`:

```python
"""
GitHub plugin — provides tools for listing and reviewing pull requests.
"""
import os
import httpx

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_ORG   = os.environ.get("GITHUB_ORG", "")

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}


def setup(context):
    """Register all GitHub tools with the Hermes agent."""

    @context.tool
    def github_list_prs(repo: str, state: str = "open") -> str:
        """List pull requests in a GitHub repository.

        Args:
            repo: Repository name, e.g. 'my-service'
            state: 'open', 'closed', or 'all'
        """
        url = f"https://api.github.com/repos/{GITHUB_ORG}/{repo}/pulls"
        r = httpx.get(url, headers=HEADERS, params={"state": state})
        r.raise_for_status()
        prs = r.json()
        if not prs:
            return f"No {state} PRs in {repo}."
        lines = [f"• #{p['number']} — {p['title']} (@{p['user']['login']})" for p in prs]
        return "\n".join(lines)

    @context.tool
    def github_get_pr_diff(repo: str, pr_number: int) -> str:
        """Get the diff for a specific pull request.

        Args:
            repo: Repository name
            pr_number: PR number
        """
        url = f"https://api.github.com/repos/{GITHUB_ORG}/{repo}/pulls/{pr_number}"
        r = httpx.get(url, headers={**HEADERS, "Accept": "application/vnd.github.diff"})
        r.raise_for_status()
        return r.text[:8000]   # truncate for LLM context
```

**Rules for plugins:**
- Must have a `setup(context)` function
- Each tool is registered with `@context.tool` decorator
- Tools must have a docstring — Hermes uses it as the LLM tool description
- Tools must have typed parameters — Hermes uses these for the JSON schema

---

### Step 2 — Register Toolset in hermes.yaml

Add to `hermes-service/hermes.yaml`:

```yaml
plugins:
  - path: plugins/jira_plugin.py
  - path: plugins/story_plugin.py
  - path: plugins/confluence_plugin.py
  - path: plugins/search_plugin.py
  - path: plugins/github_plugin.py    # ← ADD THIS

toolsets:
  jira_read:
    - jira_list_tickets
    - jira_get_ticket
  # ... existing toolsets ...
  github:                              # ← ADD THIS
    - github_list_prs
    - github_get_pr_diff
```

And add to `hermes-service/main.py` CAPABILITY_TOOLSET_MAP:

```python
CAPABILITY_TOOLSET_MAP: dict[str, list[str]] = {
    "jira_lister":       ["jira_hermes"],
    "jira_getter":       ["jira_hermes"],
    "brd_story_creator": ["jira_hermes", "story_hermes", "confluence_hermes"],
    "deploy_pipeline":   [],
    "github_reviewer":   ["github"],   # ← ADD THIS
}
```

---

### Step 3 — Create a Skill File

Create `~/.hermes/skills/github-reviewer/SKILL.md`:

```markdown
---
name: github-reviewer
description: "List open PRs and fetch diffs for code review assistance."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, PR, Code Review, Engineering]
    category: engineering
---

# GitHub Reviewer

Quickly lists open pull requests and fetches diffs for AI-assisted code review.

## When to Use

- User says "show open PRs in <repo>"
- User says "review PR #42 in <repo>"
- User says "what's changed in PR #42"

## Quick Reference

| User says | Tool to call |
|---|---|
| "list PRs in my-service" | `github_list_prs(repo="my-service")` |
| "get diff for PR #42" | `github_get_pr_diff(repo="my-service", pr_number=42)` |

## Pitfalls

- Always call tools immediately — never ask for confirmation
- Truncate diffs if too long — add a note that it's truncated
```

---

### Step 4 — Update RBAC

Add the new capability to `control-plane/capabilities.py`:

```python
ROLE_CAPABILITIES: dict[str, list[str]] = {
    "admin":         ["jira_lister", "jira_getter", "brd_story_creator",
                       "deploy_pipeline", "github_reviewer"],   # ← ADD
    "engineer":      ["jira_lister", "jira_getter", "brd_story_creator",
                       "deploy_pipeline", "github_reviewer"],   # ← ADD
    "ba":            ["jira_lister", "jira_getter", "brd_story_creator"],
    "product_owner": ["jira_lister", "jira_getter"],
    "viewer":        ["jira_lister"],
}
```

---

### Step 5 — Add Environment Variable and Deploy

Add `GITHUB_TOKEN` and `GITHUB_ORG` to:
1. `hermes-service/Dockerfile` (or `.env.example`) — document the new var
2. `infra/main.bicep` — add to the hermes-service Container App `env` array
3. GitHub Actions secrets — add `GITHUB_TOKEN` as a secret in repo settings

Then push to main:
```bash
git add .
git commit -m "feat: add GitHub PR reviewer capability"
git push origin main
# CI/CD builds new image → deploys to Container Apps automatically
```

---

## Checklist

- [ ] `plugins/<name>_plugin.py` — `setup(context)` + `@context.tool` functions with docstrings
- [ ] `hermes.yaml` — plugin path added, toolset defined
- [ ] `main.py` — `CAPABILITY_TOOLSET_MAP` updated
- [ ] `~/.hermes/skills/<name>/SKILL.md` — YAML frontmatter + procedure + output format
- [ ] `capabilities.py` — new capability added to correct roles
- [ ] `infra/main.bicep` — new env vars added if needed
- [ ] `.env.example` — new vars documented
- [ ] Pushed to `main` and CI/CD verified
