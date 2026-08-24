"""ServiceBusQueue — QueuePort backed by Azure Service Bus (LAW 4).

Requires: pip install azure-servicebus azure-identity
The queues ("parse", "chunkembed", "index") are provisioned by infra/main.bicep.
Messages are JSON (Document/Chunk are serialized via .to_dict() in the runner), so this
is a drop-in for the in-memory queue.
"""
from __future__ import annotations

import json
from typing import Iterable

from dbsearch.ports.base import QueuePort


class ServiceBusQueue(QueuePort):
    def __init__(self, fully_qualified_namespace: str, credential, max_wait_time: int = 5) -> None:
        from azure.servicebus import ServiceBusClient

        self._client = ServiceBusClient(fully_qualified_namespace, credential)
        self._max_wait = max_wait_time

    def publish(self, topic: str, message: dict) -> None:
        from azure.servicebus import ServiceBusMessage

        with self._client.get_queue_sender(topic) as sender:
            sender.send_messages(ServiceBusMessage(json.dumps(message)))

    def consume(self, topic: str) -> Iterable[dict]:
        """Drain currently-available messages. A real worker would loop forever;
        here we receive until the queue is idle for `max_wait` (matches the batch
        semantics the runner expects). Completes each message on success."""
        with self._client.get_queue_receiver(topic, max_wait_time=self._max_wait) as receiver:
            for msg in receiver:
                yield json.loads(str(msg))
                receiver.complete_message(msg)
