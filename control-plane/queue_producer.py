"""
Queue Producer — sends message payload to Azure Service Bus queue.
"""
import json
import os
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus import ServiceBusMessage

async def enqueue_message(payload: dict) -> None:
    """Serialize payload and send to Service Bus queue."""
    conn_str = os.environ["AZURE_SERVICEBUS_CONNECTION_STRING"]
    queue_name = os.environ.get("AZURE_SERVICEBUS_QUEUE_NAME", "control-plane-queue")
    async with ServiceBusClient.from_connection_string(conn_str) as client:
        async with client.get_queue_sender(queue_name) as sender:
            message = ServiceBusMessage(json.dumps(payload))
            await sender.send_messages(message)
