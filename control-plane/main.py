"""
Control Plane — handles both Teams (Bot Framework) and Slack (Events API).
Detects platform from the incoming request and routes accordingly.
Both paths resolve Jira identity and enqueue to Service Bus.
"""
import os
from dotenv import load_dotenv, find_dotenv

# Load .env before importing any module that reads os.environ at module level
load_dotenv(find_dotenv(usecwd=False))

import hmac
import hashlib
import time
import uuid
import logging
import httpx
from typing import Optional
from fastapi import FastAPI, Request, Response
from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings
from botbuilder.schema import Activity
from bot_handler import HermesBot
from queue_producer import enqueue_message
from identity import resolve_jira_user
from capabilities import get_capabilities

log = logging.getLogger(__name__)

# Teams / Bot Framework credentials
MICROSOFT_APP_ID = os.environ["MICROSOFT_APP_ID"]
MICROSOFT_APP_SECRET = os.environ["MICROSOFT_APP_SECRET"]

# Slack credentials
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]

# Bot Framework adapter (Teams)
settings = BotFrameworkAdapterSettings(MICROSOFT_APP_ID, MICROSOFT_APP_SECRET)
adapter = BotFrameworkAdapter(settings)
bot = HermesBot()

app = FastAPI(title="Hermes Control Plane")


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── TEAMS: Bot Framework webhook ──────────────────────────────────────────────

@app.post("/api/messages")
async def teams_messages(req: Request):
    """
    Teams Bot Framework webhook.
    Azure Bot Service calls this endpoint when a Teams user sends a message.
    """
    if "application/json" not in req.headers.get("Content-Type", ""):
        return Response(status_code=415)

    body = await req.json()
    activity = Activity().deserialize(body)
    auth_header = req.headers.get("Authorization", "")

    async def call_bot(turn_context):
        await bot.on_turn(turn_context)

    await adapter.process_activity(activity, auth_header, call_bot)
    return Response(status_code=200)


# ── SLACK: Events API webhook ─────────────────────────────────────────────────

def _verify_slack_signature(raw_body: bytes, timestamp: str, signature: str) -> bool:
    """
    Verify the request came from Slack using HMAC-SHA256.
    Slack signs every request with the app's Signing Secret.
    """
    try:
        if abs(time.time() - int(timestamp)) > 300:  # reject if older than 5 minutes
            return False
    except (ValueError, TypeError):
        return False

    sig_base = f"v0:{timestamp}:{raw_body.decode()}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        sig_base.encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _get_slack_user_email(user_id: str) -> Optional[str]:
    """Call Slack users.info API to get the user's email address."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://slack.com/api/users.info",
            params={"user": user_id},
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        )
        data = resp.json()
        if data.get("ok"):
            return data["user"]["profile"].get("email")
    return None


@app.post("/slack/events")
async def slack_events(req: Request):
    """
    Slack Events API webhook.
    Slack POSTs here whenever a message is sent in a channel the bot is in.
    Must respond within 3 seconds — processing is async via Service Bus queue.
    """
    raw_body = await req.body()
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    signature = req.headers.get("X-Slack-Signature", "")

    # Verify request is genuinely from Slack
    if not _verify_slack_signature(raw_body, timestamp, signature):
        return Response(status_code=403, content="Invalid signature")

    import json
    body = json.loads(raw_body)

    # Slack URL verification challenge (one-time when you first set the endpoint)
    if body.get("type") == "url_verification":
        return {"challenge": body["challenge"]}

    # Handle message events
    event = body.get("event", {})
    event_type = event.get("type", "")

    if event_type in ("message", "app_mention") and not event.get("bot_id"):
        # Correlation ID — passed in by APIM, or generated here for local dev
        correlation_id = req.headers.get("X-Correlation-ID") or str(uuid.uuid4())

        user_id = event.get("user", "")
        channel_id = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        message_text = event.get("text", "").strip()

        # Look up user's email → resolve Jira account + capabilities
        user_email = await _get_slack_user_email(user_id)
        jira_account_id = await resolve_jira_user(user_email) if user_email else None
        cap_info = get_capabilities(user_email) if user_email else {"role": "viewer", "capabilities": ["jira_lister"]}

        log.info("[%s] Slack event | user=%s role=%s channel=%s",
                 correlation_id, user_email, cap_info["role"], channel_id)

        payload = {
            "platform": "slack",
            "user_id": user_id,
            "user_email": user_email,
            "jira_account_id": jira_account_id,
            "user_role": cap_info["role"],
            "capabilities": cap_info["capabilities"],
            "message": message_text,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "correlation_id": correlation_id,
        }
        await enqueue_message(payload)

    # Must return 200 immediately — Slack retries if it doesn't get a fast response
    return Response(status_code=200)
