"""
Hermes Service — Azure Service Bus queue consumer.

Uses Nous Research Hermes Agent (AIAgent) for tool orchestration.
Tools are registered as plugins (plugins/jira_plugin.py, plugins/story_plugin.py,
plugins/confluence_plugin.py). The agent decides which tools to call.

Capabilities from the Control Plane payload map to Hermes toolsets,
enforcing role-based access (viewer → jira_read only, BA → full set).

Telemetry → Azure App Insights via opencensus-ext-azure.
Requires Python 3.11+ (hermes-agent dependency).
"""
import os
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv(usecwd=False))

import asyncio
import json
import logging
import time
import uuid
from azure.servicebus.aio import ServiceBusClient
from slack_sdk import WebClient as SlackClient
from run_agent import AIAgent
from telemetry import RequestTracker
from hermes_state import SessionDB
import vector_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger(__name__)

# Silence noisy Azure AMQP connection-state chatter and irrelevant plugin load failures
logging.getLogger("azure.servicebus._pyamqp").setLevel(logging.WARNING)
logging.getLogger("hermes_cli.plugins").setLevel(logging.WARNING)

# ── Load Confluence vector index at startup ───────────────────────────────────
vector_store.load_index()

# ── Hermes SessionDB — durable SQLite conversation history ────────────────────
# Writes to ~/.hermes/state.db; persists across service restarts.
# Set HERMES_HOME to an Azure Files mount path for cross-deployment persistence.
_SESSION_DB = SessionDB()

CONN_STR         = os.environ["AZURE_SERVICEBUS_CONNECTION_STRING"]
QUEUE_NAME       = os.environ.get("AZURE_SERVICEBUS_QUEUE_NAME", "control-plane-queue")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-haiku-4-5")

slack = SlackClient(token=os.environ["SLACK_BOT_TOKEN"])


# ── Capability → Hermes toolset mapping ───────────────────────────────────────
# Toolset names must match keys defined in hermes.yaml

CAPABILITY_TOOLSET_MAP: dict[str, list[str]] = {
    "jira_lister":       ["jira_hermes"],
    "jira_getter":       ["jira_hermes"],
    "brd_story_creator": ["jira_hermes", "story_hermes", "confluence_hermes"],
    "deploy_pipeline":   [],
}


def _toolsets_for_capabilities(capabilities: list[str]) -> list[str]:
    """Map authorized capability names to Hermes toolset names."""
    toolsets: set[str] = set()
    for cap in capabilities:
        toolsets.update(CAPABILITY_TOOLSET_MAP.get(cap, []))
    result = list(toolsets) if toolsets else ["jira_hermes"]
    # Always enable these — overrides global disabled_toolsets in ~/.hermes/config.yaml:
    #   delegation    → delegate_task (parallel multi-agent)
    #   web           → web_search + web_extract (DuckDuckGo, free)
    #   skills        → skill_manage (load/create procedural skills in ~/.hermes/skills/)
    #   session_search → search past conversations in SQLite state.db
    result = list(set(result + ["delegation", "web", "skills", "session_search"]))
    log.info("Capabilities %s → toolsets %s", capabilities, result)
    return result


# ── Route message ──────────────────────────────────────────────────────────────

async def route_message(payload: dict) -> str:
    message        = payload.get("message", "").strip()
    user_email     = payload.get("user_email", "")
    user_role      = payload.get("user_role", "unknown")
    capabilities   = payload.get("capabilities", [])
    correlation_id = payload.get("correlation_id") or str(uuid.uuid4())

    if not message:
        return "Please send a message."

    # ── Conversation history (Hermes SessionDB → SQLite) ──────────────────────
    channel_id    = payload.get("channel_id", "unknown")
    thread_ts     = payload.get("thread_ts") or str(int(time.time()))
    thread_id     = f"{payload.get('platform', 'slack')}-{channel_id}-{thread_ts}"
    prev_messages = _SESSION_DB.get_messages(thread_id, limit=20)

    tracker = RequestTracker(user_email=user_email, user_role=user_role,
                             correlation_id=correlation_id)
    success = True

    try:
        toolsets = _toolsets_for_capabilities(capabilities)
        log.info("[%s] Routing | user=%s role=%s toolsets=%s thread=%s",
                 correlation_id, user_email, user_role, toolsets, thread_id)

        agent = AIAgent(
            model=OPENROUTER_MODEL,
            session_id=thread_id,
            session_db=_SESSION_DB,
            quiet_mode=True,
            enabled_toolsets=toolsets,
            max_iterations=10,
            skip_memory=True,
        )

        system_message = (
            "You are a Jira and Confluence assistant. Act immediately — NEVER ask for clarification.\n\n"
            "RULES:\n"
            "- 'show tickets' / 'list tickets' → call jira_list_tickets immediately\n"
            "- 'open ticket X' / 'get ticket X' → call jira_get_ticket immediately\n"
            "- 'break into epics/stories' / 'create from BRD' → follow BRD STEPS below\n"
            "- General questions unrelated to Jira/Confluence → call web_search and answer from results\n"
            "- Complex BRD breakdowns (many epics + stories) → use delegate_task to parallelize subtasks\n"
            "- 'what did I ask before' / 'find past work' / 'recall previous' → call session_search with relevant keywords\n"
            "- Load the brd-breakdown skill before starting any BRD breakdown, jira-assistant skill before listing/fetching tickets\n\n"
            "BRD BREAKDOWN STEPS — do ALL in order, never skip, never ask:\n"
            "  1. Call confluence_find_relevant with keywords from the user request or Jira ticket\n"
            "  2. Call confluence_get_page with the best matching page_title from step 1 results\n"
            "  3. Call break_brd_into_stories with the full page content\n"
            "  4. Call jira_create_epic WITHOUT parent_key → creates ONE main Epic (use the project name as summary)\n"
            "  5. For EACH functional requirement returned in step 3, call jira_create_epic WITH parent_key = main Epic key from step 4\n"
            "  6. For EACH story/task under each FR, call jira_create_story with epic_key = the FR Task key from step 5\n\n"
            "FINAL SLACK RESPONSE FORMAT for BRD breakdown (use this exact format):\n"
            "*✅ Jira Hierarchy Created*\n"
            "*Main Epic:* SCRUM-XX — <project name>\n\n"
            "*Epics created (X):*\n"
            "• SCRUM-XX — <FR name>\n"
            "• SCRUM-XX — <FR name>\n"
            "_(one line per epic)_\n\n"
            "*Tasks created (X):*\n"
            "• SCRUM-XX — <story title> _(under SCRUM-XX)_\n"
            "_(one line per task)_\n\n"
            "For ticket listings: • *KEY* — Summary `[Status]`\n"
            "NEVER output just 'Done' or 'Completed' — always show the full structured block above."
        )

        # AIAgent.run_conversation() is synchronous — run in executor to avoid blocking async loop
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: agent.run_conversation(message, system_message, conversation_history=prev_messages),
        )

        log.info("[%s] Agent result keys: %s", correlation_id,
                 list(result.keys()) if isinstance(result, dict) else type(result))
        log.info("[%s] Agent final_response: %r", correlation_id,
                 result.get("final_response") if isinstance(result, dict) else result)

        response_text = result.get("final_response") or "I completed the task but had no output to show."
        tracker.set_skill(toolsets[0] if toolsets else "general")
        tracker.add_tokens(
            OPENROUTER_MODEL,
            result.get("prompt_tokens", 0),
            result.get("completion_tokens", 0),
        )

        # Hermes auto-persists this turn to state.db — no manual save needed
        log.info("[%s] Turn complete | thread=%s | Hermes saved to state.db",
                 correlation_id, thread_id)
        return response_text

    except Exception as e:
        success = False
        log.error("[%s] Agent error: %s", correlation_id, e, exc_info=True)
        tracker.set_skill("error")
        return f"Sorry, I encountered an error: {e}"

    finally:
        tracker.finish(success=success)
        tracker.emit()


# ── Reply helper ───────────────────────────────────────────────────────────────

def send_slack_reply(channel_id: str, thread_ts: str, text: str) -> None:
    slack.chat_postMessage(
        channel=channel_id,
        text=text,
        thread_ts=thread_ts or None,
    )


# ── Queue consumer ────────────────────────────────────────────────────────────

_LOCK_RENEWAL_INTERVAL = 30   # renew message lock every 30 s (default lock = 60 s)
_RECONNECT_DELAY       = 5    # seconds to wait before reconnecting after any error


async def _renew_lock_loop(receiver, msg) -> None:
    """Keep renewing the Service Bus message lock every 30 s until cancelled."""
    while True:
        await asyncio.sleep(_LOCK_RENEWAL_INTERVAL)
        try:
            await receiver.renew_message_lock(msg)
            log.debug("Lock renewed for message")
        except Exception as e:
            log.warning("Lock renewal failed: %s", e)
            return


async def _process_one(receiver, msg) -> None:
    """Process a single Service Bus message, renewing its lock in the background."""
    payload = json.loads(str(msg))
    correlation_id = payload.get("correlation_id", str(uuid.uuid4()))
    log.info(
        "[%s] Processing message from user=%s channel=%s",
        correlation_id, payload.get("user_email"), payload.get("channel_id"),
    )

    # Start background lock renewal so the message lock doesn't expire during LLM calls
    renew_task = asyncio.create_task(_renew_lock_loop(receiver, msg))
    try:
        response_text = await route_message(payload)
        send_slack_reply(
            channel_id=payload.get("channel_id", ""),
            thread_ts=payload.get("thread_ts", ""),
            text=response_text,
        )
        log.info("Replied to channel=%s", payload.get("channel_id"))
        await receiver.complete_message(msg)
    except Exception as e:
        log.error("Failed to process message: %s", e, exc_info=True)
        # Abandon so Azure retries, but only if under delivery count limit
        try:
            await receiver.abandon_message(msg)
        except Exception as ab_err:
            log.error("abandon_message failed: %s", ab_err)
    finally:
        renew_task.cancel()
        try:
            await renew_task
        except asyncio.CancelledError:
            pass


async def process_queue() -> None:
    log.info("Hermes Service started | queue=%s | model=%s", QUEUE_NAME, OPENROUTER_MODEL)
    while True:   # outer loop: reconnects on any Service Bus / network error
        try:
            async with ServiceBusClient.from_connection_string(CONN_STR) as client:
                async with client.get_queue_receiver(QUEUE_NAME, max_wait_time=5) as receiver:
                    log.info("Connected to Service Bus queue — listening…")
                    while True:
                        try:
                            messages = await receiver.receive_messages(
                                max_message_count=1, max_wait_time=5
                            )
                        except Exception as recv_err:
                            log.error("receive_messages error (will reconnect): %s", recv_err)
                            break   # break inner loop → re-enter outer → reconnect
                        for msg in messages:
                            await _process_one(receiver, msg)
        except Exception as conn_err:
            log.error("Service Bus connection error (will retry in %ds): %s",
                      _RECONNECT_DELAY, conn_err)
        log.info("Reconnecting to Service Bus in %d s…", _RECONNECT_DELAY)
        await asyncio.sleep(_RECONNECT_DELAY)


if __name__ == "__main__":
    asyncio.run(process_queue())
