"""
Story Agent — uses OpenRouter LLM to break a BRD into epics and user stories.
"""
import os
import json
import httpx

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-haiku-4-5")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = """You are a Business Analyst. Given a BRD (Business Requirements Document),
extract epics and user stories in strict JSON format.

Return ONLY valid JSON like this:
[
  {
    "epic_title": "User Authentication",
    "epic_description": "Manage user login and access",
    "stories": [
      {
        "title": "As a user, I can log in with email and password",
        "description": "Login form with validation",
        "acceptance_criteria": "Given valid credentials, user is redirected to dashboard"
      }
    ]
  }
]"""


async def break_brd_into_stories(brd_text: str):
    """Call OpenRouter LLM to extract epics and stories from BRD text.

    Returns:
        tuple[list[dict], int, int]: (epics, input_tokens, output_tokens)
    """
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://hermes-agent.dev",
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"BRD Content:\n\n{brd_text}"},
        ],
        "temperature": 0.2,
        "max_tokens": 4096,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)

    # Parse JSON from response
    content = content.strip()
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    epics = json.loads(content.strip())
    return epics, input_tokens, output_tokens
