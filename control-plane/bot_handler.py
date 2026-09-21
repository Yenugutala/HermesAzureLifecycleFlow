"""
Bot Handler — parses Teams activities, stores conversation references,
sends immediate ACK, and enqueues the message for async processing.
"""
import json
import os
from botbuilder.core import ActivityHandler, TurnContext
from botbuilder.schema import Activity, ActivityTypes, ConversationReference
from identity import resolve_jira_user
from queue_producer import enqueue_message

# In-memory store: user_id → conversation reference (for proactive replies)
CONV_REFS: dict[str, dict] = {}


class HermesBot(ActivityHandler):

    async def on_message_activity(self, turn_context: TurnContext):
        user_id = turn_context.activity.from_property.id
        user_email = turn_context.activity.from_property.name  # Teams uses email as name
        message_text = turn_context.activity.text or ""

        # Store conversation reference for proactive reply later
        conv_ref = TurnContext.get_conversation_reference(turn_context.activity)
        CONV_REFS[user_id] = conv_ref.serialize()

        # Acknowledge immediately so Teams doesn't time out
        await turn_context.send_activity("⏳ Processing your request...")

        # Resolve Jira identity
        jira_account_id = await resolve_jira_user(user_email)

        # Build payload for Service Bus queue
        payload = {
            "user_id": user_id,
            "user_email": user_email,
            "jira_account_id": jira_account_id,
            "message": message_text.strip(),
            "conv_ref": CONV_REFS[user_id],
            "service_url": turn_context.activity.service_url,
        }

        await enqueue_message(payload)

    async def on_members_added_activity(self, members_added, turn_context: TurnContext):
        for member in members_added:
            if member.id != turn_context.activity.recipient.id:
                await turn_context.send_activity(
                    "👋 Hi! I'm the Hermes Agent. Mention me with commands like:\n"
                    "- `list tickets`\n"
                    "- `what is SCRUM-1`\n"
                    "- `create stories from BRD: <filename>`"
                )
